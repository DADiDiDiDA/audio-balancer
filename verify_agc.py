"""端到端行为验证：门控 + 低电平拐点 + 真峰值限幅 在真实链路里的表现。

三个最容易翻车的场景：
  1) 极弱信号（底噪级）不该被猛提；
  2) 正常内容要能提到目标附近；
  3) 过响内容要被压下来，且输出真峰值不越过天花板。
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import engine as E
import soundcard as sc
from config import Config

SR = 48000
fails = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


cable = [s for s in sc.all_speakers() if "cable in 16ch" in s.name.lower()][0]
out = [s for s in sc.all_speakers() if "realtek" in s.name.lower()][0]

cfg = Config(path=os.path.join(tempfile.mkdtemp(), "t.json"))
cfg.update({"block_ms": 10.0, "downmix": True, "use_windows_volume": False,
            "input_device": cable.name, "output_device": out.name,
            "enabled": True, "level_mode": "auto", "target_lufs": -18.0,
            "target_range_db": 3.0, "max_boost_db": 12.0, "max_att": 20.0,
            "max_attenuation_db": 20.0, "limiter_ceiling_db": -1.0,
            "low_knee_db": 34.0, "low_knee_width_db": 10.0})
eng = E.Engine(cfg)
eng.start()
time.sleep(2.5)


def play(amp, sec=4.0, freq=1000.0):
    sig = (amp * np.sin(2 * np.pi * freq * np.arange(int(SR * 0.1)) / SR)).astype(np.float32)[:, None]

    def run():
        with cable.player(samplerate=SR, channels=1) as p:
            for _ in range(int(sec / 0.1)):
                p.play(sig)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def sample(seconds=2.2):
    """播放稳定后采样状态（跳过前 1 秒的起音）。"""
    time.sleep(1.0)
    lufs, gain, tp, gr = [], [], [], []
    t0 = time.time()
    while time.time() - t0 < seconds:
        st = eng.status
        lufs.append(float(st.get("lufs", -70)))
        gain.append(float(st.get("gain_db", 0)))
        if st.get("out_tp_db") is not None:
            tp.append(float(st["out_tp_db"]))
        gr.append(float(st.get("out_gr_db") or 0.0))
        time.sleep(0.1)
    return (float(np.median(lufs)), float(np.median(gain)),
            float(np.max(tp)) if tp else None, float(np.min(gr)))


print("--- 1) 底噪级弱信号：不该被提升 ---")
th = play(0.002, 5.5)
lufs, gain, tp, gr = sample()
print("  输入约 %.1f LUFS → 增益 %+.1f dB（真峰值 %s）"
      % (lufs, gain, ("%.1f dBTP" % tp) if tp is not None else "—"))
check(gain < 1.0, "弱信号的提升接近 0（护底噪拐点生效）")
th.join(timeout=8)
time.sleep(1.5)

print("--- 2) 正常偏轻内容：提到目标附近 ---")
th = play(0.05, 5.5)
lufs, gain, tp, gr = sample()
print("  输入约 %.1f LUFS → 增益 %+.1f dB，输出约 %.1f LUFS"
      % (lufs, gain, lufs + gain))
check(gain > 2.0, "偏轻内容被提上来")
check(abs((lufs + gain) - (-18.0)) < 6.0, "输出响度落在目标附近（±6 dB 内）")
th.join(timeout=8)
time.sleep(1.5)

print("--- 3) 过响内容：压下来且不越过真峰值天花板 ---")
th = play(0.9, 5.5)
lufs, gain, tp, gr = sample()
print("  输入约 %.1f LUFS → 增益 %+.1f dB，限幅衰减 %.1f dB，输出真峰值 %s"
      % (lufs, gain, gr, ("%.2f dBTP" % tp) if tp is not None else "—"))
check(gain < 0.0, "过响内容被衰减")
if tp is not None:
    check(tp <= -1.0 + 0.2, "输出真峰值不越过 −1 dBTP（实测 %.2f）" % tp)
th.join(timeout=8)

eng.stop()
print("\n%s（%d 项失败）" % ("全部通过" if not fails else "有失败", len(fails)))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
