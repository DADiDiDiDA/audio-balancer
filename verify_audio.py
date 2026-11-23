"""音频链路自检：RAW 格式校验、限幅、端到端失真与增益。

不需要界面，直接跑引擎；播放一个 1kHz 正弦到 CABLE，抓引擎的输出 tap。
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
from wasapi_raw import RawPlayer, describe_mix_format

SR = 48000
fails = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def speakers():
    return {s.name.lower(): s for s in sc.all_speakers()}


def find(keyword):
    for name, s in speakers().items():
        if keyword in name:
            return s
    return None


print("--- 1) RAW 混音格式校验 ---")
out = find("realtek") or find("耳机")
try:
    p = RawPlayer(out.name, SR, 2, buffer_ms=26).open()
    print("  设备: %s  混音格式=%s  raw=%s" % (out.name, p.mix_format, p.used_raw))
    check(p.mix_format is not None and p.mix_format[1] == 2,
          "能读出混音格式的声道数")
    check(p.mix_format[3] == 32 and p.mix_format[0], "混音格式是 float32")
    p.close()
except Exception as exc:
    print("  打不开 RAW（会回退共享模式）:", exc)

print("--- 2) 直通时的软膝限幅（0 dBFS） ---")
x = np.linspace(0, 2 * np.pi * 3, 4800)
loud = (1.6 * np.sin(x))[:, None].astype(np.float64)
y = E.soft_limit(loud, 0.0)
check(float(np.abs(y).max()) <= 1.0001, "峰值被压到满刻度以内（%.4f）" % float(np.abs(y).max()))
check(float(np.max(np.abs(np.diff(y, axis=0)))) < 0.2,
      "限幅后没有阶跃（最大步进 %.3f）" % float(np.max(np.abs(np.diff(y, axis=0)))))
quiet = (0.3 * np.sin(x))[:, None]
check(np.array_equal(E.soft_limit(quiet, 0.0), quiet), "低于膝点的信号一个采样都不改")

print("--- 3) 端到端：响度/增益/失真 ---")
cfg = Config(path=os.path.join(tempfile.mkdtemp(), "t.json"))
cfg.update({"block_ms": 10.0, "downmix": True, "use_windows_volume": True,
            "input_device": "CABLE In 16ch (VB-Audio Virtual Cable)",
            "output_device": out.name, "enabled": True, "passthrough": False})
taps = []
eng = E.Engine(cfg)
eng.tap = lambda a, b: taps.append((np.asarray(a, float).copy(), np.asarray(b, float).copy()))
eng.start()
time.sleep(2.5)
print("  状态:", {k: eng.status.get(k) for k in
                ("running", "paused", "error", "in_channels", "out_mode",
                 "buffer_ms", "block_ms")})

cable = find("cable in 16ch") or find("cable")
sig = (0.08 * np.sin(2 * np.pi * 1000 * np.arange(SR) / SR)).astype(np.float32)[:, None]


def play_for(sec):
    def run():
        try:
            with cable.player(samplerate=SR, channels=1) as pl:
                for _ in range(int(sec * 10) + 1):
                    pl.play(sig)
        except Exception as exc:
            print("  播放异常:", exc)
    threading.Thread(target=run, daemon=True).start()


def thd(seg):
    w = np.hanning(seg.size)
    spec = np.abs(np.fft.rfft(seg * w)) ** 2
    k = int(round(1000 * seg.size / SR))
    band = spec[max(0, k - 3):k + 4].sum()
    total = spec[1:].sum()
    return 10 * np.log10(max(total - band, 1e-30) / max(total, 1e-30))


play_for(5.0)
time.sleep(3.6)
taps.clear()
s0 = int(eng.status.get("out_starves") or 0)
time.sleep(1.2)
s1 = int(eng.status.get("out_starves") or 0)
print("  持续有声 1.2 秒内输出写空次数：%d" % (s1 - s0))
check(s1 - s0 <= 2, "持续有声时输出缓冲没有被写空（%d 次）" % (s1 - s0))
if taps:
    ys = np.concatenate([t[1][:, 0] for t in taps])[:SR // 2]
    print("  输出 RMS %.4f  THD+N %.1f dB  块数 %d"
          % (float(np.sqrt(np.mean(ys ** 2))), thd(ys), len(taps)))
    check(thd(ys) < -80, "输出失真低于 −80 dB")
    check(np.isfinite(ys).all(), "输出没有 NaN/Inf")
else:
    check(False, "抓到了输出 tap")
time.sleep(1.0)
st = eng.status
print("  LUFS %.1f  增益 %+.1f dB  欠载 %s  输出写空 %s"
      % (st.get("lufs", -99), st.get("gain_db", 0), st.get("underrun"),
         st.get("out_starves")))
check(st.get("running") is True and not st.get("error"), "引擎在跑且无错误")
eng.stop()

# 恢复默认输出（引擎会话结束时不会动设备，这里只是确认）
print("\n%s（%d 项失败）" % ("全部通过" if not fails else "有失败", len(fails)))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
