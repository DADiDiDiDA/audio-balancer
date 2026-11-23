"""真峰值限幅器自检：天花板、无过冲、增益平滑、块边界连续、直通不改样。

用手工构造的最坏情况（采样间峰值、方波、脉冲、满幅正弦）压测。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

from dsp import TruePeakLimiter

SR = 48000
fails = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def run(lim, x, block=480):
    """按块喂进去，返回拼接后的输出。"""
    outs = []
    for i in range(0, x.shape[0], block):
        outs.append(lim.process(x[i:i + block]))
    return np.concatenate(outs, axis=0)


def tp_of(x):
    """独立算真峰值（4× 过采样，用 numpy poly 重采样），用于验证。"""
    from scipy.signal import resample_poly
    y = resample_poly(x, 4, 1, axis=0)
    return float(np.abs(y).max())


def sine(amp, sec, freq=997.0, phase=0.0):
    t = np.arange(int(SR * sec)) / SR
    return np.repeat((amp * np.sin(2 * np.pi * freq * t + phase))[:, None], 2, axis=1)


print("--- 1) 未超限时：只延迟，不改样 ---")
x = sine(0.3, 0.5)
lim = TruePeakLimiter(SR, 2, ceiling_db=-1.0, lookahead_ms=2.0)
y = run(lim, x)
n = lim.n
check(y.shape == x.shape, "输出长度与输入一致（块内延迟 %d 采样）" % n)
check(np.allclose(y[n:], x[:-n], atol=1e-12), "输出 = 输入延迟 n 个采样（零改动）")
check(lim.engaged == 0, "没有触发限幅")
print("  真峰值 %.2f dBFS" % (20 * np.log10(max(tp_of(x), 1e-12))))

print("--- 2) 大幅过载：真峰值不越过天花板 ---")
for amp in (1.0, 1.4, 2.0, 4.0):
    lim = TruePeakLimiter(SR, 2, ceiling_db=-1.0, lookahead_ms=2.0)
    y = run(lim, sine(amp, 0.6))
    tp = tp_of(y[lim.n:])
    print("  输入 %.1fx（%.1f dBTP）→ 输出 %.3f（%.2f dBTP），最大衰减 %.1f dB"
          % (amp, 20 * np.log10(amp), tp, 20 * np.log10(max(tp, 1e-12)), lim.last_gr_db))
    check(tp <= 10 ** (-1.0 / 20.0) + 0.02, "%.1fx 输入时真峰值 ≤ 天花板（含 2%% 容差）" % amp)
    check(np.abs(y).max() <= 1.0, "%.1fx 输入时样本峰值 ≤ 0 dBFS" % amp)

print("--- 3) 采样间峰值（fs/4、45° 相位）：样本看不出来，真峰值超了 ---")
t = np.arange(int(SR * 0.5)) / SR
x = (0.95 * np.sin(2 * np.pi * (SR / 4) * t + np.pi / 4))[:, None]
print("  输入样本峰值 %.3f，真峰值 %.3f" % (float(np.abs(x).max()), tp_of(x)))
lim = TruePeakLimiter(SR, 2, ceiling_db=-0.5, lookahead_ms=2.0)
y = run(lim, x)
tp = tp_of(y[lim.n:])
check(tp <= 10 ** (-0.5 / 20.0) + 0.02, "采样间峰值也被压到天花板以内（%.3f）" % tp)

print("--- 4) 脉冲与方波：不过冲、不产生阶跃 ---")
imp = np.zeros((int(SR * 0.2), 2))
imp[1000] = 5.0
lim = TruePeakLimiter(SR, 2, ceiling_db=-1.0, lookahead_ms=2.0)
y = run(lim, imp)
print("  5.0 脉冲 → 输出峰值 %.3f" % float(np.abs(y).max()))
check(float(np.abs(y).max()) <= 10 ** (-1.0 / 20.0) + 0.02, "脉冲也不过冲")

sq = (np.sign(np.sin(2 * np.pi * 200 * t)) * 0.9)[:, None]
sq = np.repeat(sq, 2, axis=1)
lim = TruePeakLimiter(SR, 2, ceiling_db=-1.0, lookahead_ms=2.0)
y = run(lim, sq)
check(float(np.abs(y).max()) <= 10 ** (-1.0 / 20.0) + 0.02, "方波被压到天花板以内")

# 增益平滑性要看"波形本身的步进有没有被放大"：用满幅正弦比最大相邻样本差
loud = sine(1.8, 0.4, freq=440.0)
lim = TruePeakLimiter(SR, 2, ceiling_db=-1.0, lookahead_ms=2.0)
y = run(lim, loud)
step_in = float(np.max(np.abs(np.diff(loud[:, 0]))))
step_out = float(np.max(np.abs(np.diff(y[:, 0]))))
print("  1.8x 正弦：输入最大步进 %.4f，输出 %.4f" % (step_in, step_out))
check(step_out <= step_in * 1.02 + 1e-6, "限幅没有在波形上造出额外阶跃")

print("--- 5) 块边界连续：分块处理与整段处理一致 ---")
x = sine(1.6, 0.4, freq=440.0)
a = TruePeakLimiter(SR, 2, ceiling_db=-1.0, lookahead_ms=2.0)
y_blocks = run(a, x, block=480)
b = TruePeakLimiter(SR, 2, ceiling_db=-1.0, lookahead_ms=2.0)
y_whole = b.process(x)
check(np.allclose(y_blocks, y_whole, atol=1e-9), "480 帧分块与整段结果一致（无接缝）")

print("--- 6) 长时低频大信号：不爆、不漂 ---")
t2 = np.arange(int(SR * 2.0)) / SR
low = (1.2 * np.sin(2 * np.pi * 45 * t2))[:, None]
lim = TruePeakLimiter(SR, 2, ceiling_db=-1.0, lookahead_ms=2.0)
y = run(lim, low)
check(np.isfinite(y).all(), "输出全部有限")
check(float(np.abs(y).max()) <= 10 ** (-1.0 / 20.0) + 0.02, "低频大信号也被限住")

print("\n%s（%d 项失败）" % ("全部通过" if not fails else "有失败", len(fails)))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
