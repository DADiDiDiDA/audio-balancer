"""门控测量自检：对照一个独立的参考实现，并验证"长静音不拉低响度"。

为什么单独测：门控是这次改动的核心，算错不会报错、只会让均衡器
"该提的不提、不该提的猛提"——必须用数值对得上。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from scipy.signal import sosfilt

from loudness import KWeighting, SlidingLUFS, k_weighting_sos

SR = 48000
fails = []

# BS.1770-4 公布的 48kHz K 加权系数（独立于产品代码，作为对照）
OFFICIAL = np.array([[1.53512485958697, -2.69169618940638, 1.19839281085285,
                      1.0, -1.69065929318241, 0.73248077421585],
                     [1.0, -2.0, 1.0,
                      1.0, -1.99004745483398, 0.99007225036621]])


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def ref_lufs_from_z(zs, ns, abs_gate=-70.0, rel_gate=-10.0):
    """参考实现：直接按 BS.1770 两级门控算，独立于产品代码。"""
    z_abs = 10 ** ((abs_gate + 0.691) / 10.0)
    kept = [(z, n) for z, n in zip(zs, ns) if z >= z_abs]
    if not kept:
        return None
    mean1 = sum(z * n for z, n in kept) / sum(n for _z, n in kept)
    l1 = -0.691 + 10 * np.log10(mean1)
    z_rel = 10 ** ((l1 + rel_gate + 0.691) / 10.0)
    kept2 = [(z, n) for z, n in kept if z >= z_rel]
    mean2 = sum(z * n for z, n in kept2) / sum(n for _z, n in kept2)
    return -0.691 + 10 * np.log10(mean2)


def kweight(x):
    """用标准公布的系数滤波（参考实现，不调用产品代码的设计函数）。"""
    return sosfilt(OFFICIAL, x, axis=0)


def tone(amp, sec, freq=1000.0):
    t = np.arange(int(SR * sec)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


print("--- 0) 系数与采样率 ---")
got = k_weighting_sos(SR)
check(float(np.max(np.abs(got - OFFICIAL))) < 1e-9,
      "48kHz 系数与 BS.1770-4 公布表一致（最大差 %.1e）"
      % float(np.max(np.abs(got - OFFICIAL))))
# 同一信号在 44.1/48/96k 下必须测出同一个数（系数没跟着采样率改就会漂）
vals = {}
for fs in (44100, 48000, 96000):
    m = SlidingLUFS(fs, 1000)
    k = KWeighting(fs, 2)
    t = np.arange(int(fs * 1.2)) / fs
    x = np.repeat((0.1 * np.sin(2 * np.pi * 1000 * t))[:, None], 2, axis=1)
    step = int(fs * 0.02)
    for i in range(0, x.shape[0], step):
        v = float(m.update(k.process(x[i:i + step])))
    vals[fs] = v
check(max(vals.values()) - min(vals.values()) < 0.1,
      "同一信号在 44.1/48/96kHz 下读数一致（极差 %.3f dB）"
      % (max(vals.values()) - min(vals.values())))
# 校准：双声道 -20 dBFS 的 1kHz 正弦 ≈ -20 LUFS（BS.1770 的参考点）
check(abs(vals[48000] + 20.0) < 0.15,
      "双声道 -20dBFS / 1kHz 正弦读作 %.2f LUFS（参考点 -20）" % vals[48000])


print("--- 1) 与参考实现逐块对照 ---")
meter = SlidingLUFS(SR, window_ms=1000)
rng = np.random.default_rng(0)
zs, ns = [], []
worst = 0.0
for i in range(60):                       # 3 秒、每块 50ms
    if i < 10:
        amp = 0.3
    elif i < 20:
        amp = 0.0                          # 静音段
    elif i < 34:
        amp = 0.02                         # 弱段（比主段低 24 dB）
    else:
        amp = 0.15
    block = tone(amp, 0.05) if amp else np.zeros(int(SR * 0.05))
    y = kweight(block).reshape(-1, 1)
    got = meter.update(y)
    z = float(np.mean(y ** 2))
    zs.append(z)
    ns.append(len(y))
    # 滑动窗口 = 最近 1 秒（20 块）
    keep = slice(max(0, len(zs) - 20), len(zs))
    want = ref_lufs_from_z(zs[keep], ns[keep])
    if want is not None and np.isfinite(got):
        worst = max(worst, abs(got - want))
check(worst < 0.02, "门控值与参考实现一致（最大差 %.4f dB）" % worst)

print("--- 2) 长静音不把响度拉低 ---")
gated = SlidingLUFS(SR, window_ms=2000)
ungated_sum = 0.0
ungated_n = 0
for i in range(40):                       # 先 1 秒有声，再 1 秒静音
    block = tone(0.1, 0.05) if i < 20 else np.zeros(int(SR * 0.05))
    y = kweight(block).reshape(-1, 1)
    gated.update(y)
    ungated_sum += float(np.mean(y ** 2)) * len(y)
    ungated_n += len(y)
g_val = gated.value()
u_val = -0.691 + 10 * np.log10(ungated_sum / ungated_n + 1e-30)
print("  门控 %.2f LUFS   不门控 %.2f LUFS（差 %.2f dB）" % (g_val, u_val, g_val - u_val))
check(g_val > u_val + 2.0, "门控后比不门控高 2 dB 以上（静音被剔除）")

print("--- 3) 极弱信号按绝对门限剔除 ---")
m2 = SlidingLUFS(SR, window_ms=1000)
for _ in range(20):
    y = kweight(tone(0.0002, 0.05)).reshape(-1, 1)   # 约 -80 LUFS
    m2.update(y)
v = m2.value()
print("  0.0002 幅度（≈ −80 LUFS）测出 %.1f" % v)
check(v <= -69.0, "低于绝对门限时返回 −70 而不是一个虚假的读数")

print("--- 4) 窗口里的弱段被相对门限剔除 ---")
m3 = SlidingLUFS(SR, window_ms=1000)
vals = []
for _ in range(10):
    vals.append(m3.update(kweight(tone(0.2, 0.05)).reshape(-1, 1)))
loud_only = vals[-1]
for _ in range(8):
    vals.append(m3.update(kweight(tone(0.006, 0.05)).reshape(-1, 1)))  # 低 30 dB
mixed = vals[-1]
print("  只有大声段 %.1f  →  混入 −30dB 弱段后 %.1f（差 %.2f dB）" % (loud_only, mixed, mixed - loud_only))
check(abs(mixed - loud_only) < 1.5, "弱段几乎不影响测量（差 < 1.5 dB）")

print("\n%s（%d 项失败）" % ("全部通过" if not fails else "有失败", len(fails)))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
