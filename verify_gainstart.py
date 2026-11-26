"""验证「刚打开 / 刚有声音时音量偏大一下」已修（v1.8.8）。

不连设备、不出声：用真实的 K 加权 + 滑动 LUFS + desired_gain + agc_step，
按 10ms 块跑一段 -12 LUFS 的噪声，对比旧策略（升温期强制 0 dB）与新策略的
增益包络。断言：
  1) 旧策略开头确实有 ~0.4 秒的 0 dB（复现问题）；
  2) 新策略在第一块就把衰减落到位，之后不再有「先大后小」；
  3) 静音恢复时保留衰减、不跳回 0 dB；
  4) 升温期不会因为测量没填满而猛提（正增益不落位）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import engine as E
from dsp import DcBlocker
from loudness import KWeighting, SlidingLUFS

SR = 48000
BLK = 480            # 10ms
SECONDS = 2.0
TARGET = -18.0
MAX_ATT, MAX_BOOST = 20.0, 12.0
ATTACK, RELEASE = 0.120, 0.900

fails = []
total = 0


def check(cond, msg, extra=""):
    global total
    total += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(msg)


def make_noise(lufs_db=-12.0):
    """按目标响度生成噪声（先归一化再按 LUFS 反算幅度）。"""
    rng = np.random.default_rng(7)
    w = rng.standard_normal((SR * 3, 2))
    kw = KWeighting(SR, 2)
    m = SlidingLUFS(SR, window_ms=400)
    m.update(kw.process(w))
    v = m.value()
    amp = 10.0 ** ((lufs_db - v) / 20.0)
    return (w * amp).astype(np.float64)


def envelope(x, policy):
    """跑一遍增益包络；policy = "old" | "new"。"""
    dc = DcBlocker(SR, fc=10.0, channels=2)
    kw = KWeighting(SR, 2)
    meter = SlidingLUFS(SR, window_ms=1000)
    state = {"gain": 0.0, "fresh": policy == "new", "lufs": -70.0, "warm": 0.0}
    gains = []
    for i in range(0, len(x) - BLK + 1, BLK):
        blk = dc.process(x[i:i + BLK])
        state["lufs"] = float(meter.update(kw.process(blk)))
        state["warm"] += BLK / float(SR)
        if state["lufs"] < -60:
            desired = 0.0
        else:
            desired = E.desired_gain(
                "auto", TARGET, 3.0, state["lufs"], MAX_ATT, MAX_BOOST,
                low_knee=TARGET - 34.0, knee_width=10.0)
        if policy == "old":
            if state["warm"] < 0.4:
                desired = 0.0
            if desired < state["gain"]:
                tau = ATTACK
            else:
                tau = max(RELEASE, 1.2)
            coeff = 1.0 - float(np.exp(-(BLK / SR) / tau))
            state["gain"] += (desired - state["gain"]) * coeff
        else:
            if state["warm"] < 0.4:
                desired = min(0.0, desired)
            E.agc_step(state, desired, state["warm"], BLK / SR,
                       ATTACK, RELEASE, True)
        gains.append(state["gain"])
    return np.array(gains)


x = make_noise()
old = envelope(x, "old")
new = envelope(x, "new")

print("== 启动包络（前 1 秒，单位 dB） ==")
print("  旧: 第1块 %.1f  0.1s %.1f  0.3s %.1f  0.5s %.1f  1.0s %.1f"
      % (old[0], old[10], old[30], old[50], old[99]))
print("  新: 第1块 %.1f  0.1s %.1f  0.3s %.1f  0.5s %.1f  1.0s %.1f"
      % (new[0], new[10], new[30], new[50], new[99]))

tail = float(np.median(new[len(new) // 2:]))
check(abs(old[0]) < 0.05 and abs(old[10]) < 0.05,
      "旧策略开头确实按 0 dB 放（复现「偏大一下」）", "第1块 %.2f dB" % old[0])
check(new[0] <= tail + 1.5,
      "新策略第一块就落在稳态增益 1.5dB 内", "第1块 %.1f / 稳态 %.1f dB" % (new[0], tail))
check(float(new[:100].max()) - tail <= 1.0,
      "新策略前 1 秒过冲不到 1dB（人耳不可闻）", "最大 %.1f dB" % float(new[:100].max()))
check(old[:50].max() - tail > 3.0,
      "旧策略前 0.5 秒比稳态高 3dB 以上（问题确实存在）",
      "高 %.1f dB" % (old[:50].max() - tail))

print("\n== 静音恢复 ==")
st = {"gain": -8.0, "fresh": True, "warm": 0.0}
g = E.agc_step(st, -8.0, 0.0, 0.01, ATTACK, RELEASE, True)
check(abs(g + 8.0) < 1e-6, "恢复时保留衰减值，不跳回 0 dB", "%.2f dB" % g)
st = {"gain": 0.0, "fresh": True, "warm": 0.0}
g = E.agc_step(st, 6.0, 0.01, 0.01, ATTACK, RELEASE, True)
check(g < 0.2, "升温期不会因为测量没填满就猛提", "%.2f dB" % g)
st = {"gain": -8.0, "fresh": True, "warm": 0.01}
g = E.agc_step(st, -2.0, 0.01, 0.01, ATTACK, RELEASE, True)
check(g <= -7.9, "恢复后要变响也只走慢释放", "%.2f dB" % g)

print("\n结果：PASS=%d  FAIL=%d" % (total - len(fails), len(fails)))
sys.exit(1 if fails else 0)
