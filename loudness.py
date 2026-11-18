"""ITU-R BS.1770-4 实时响度测量。

包含：
  * K-weighting 滤波（预滤波 high-shelf + RLB high-pass），双二阶级联，保留滤波状态；
  * 滑动窗口 LUFS 计算（立体声权重 1/1，5.1 权重 1/1/1.41/1/1/1.41）。

参考：EBU R128 / ITU-R BS.1770-4 标准系数。
"""
from collections import deque

import numpy as np
from scipy.signal import sosfilt, sosfilt_zi


def _rbj_sos(kind, fs, f0, q=None, gain_db=None):
    """RBJ Audio EQ Cookbook 双二阶滤波器，返回 sos 行 [b0,b1,b2,a0,a1,a2]。"""
    w0 = 2.0 * np.pi * f0 / fs
    cw, sw = np.cos(w0), np.sin(w0)
    if kind == "highpass":
        alpha = sw / (2.0 * q)
        b0, b1, b2 = (1.0 + cw) / 2.0, -(1.0 + cw), (1.0 + cw) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cw, 1.0 - alpha
    elif kind == "highshelf":
        a = 10.0 ** (gain_db / 40.0)
        alpha = sw / (2.0 * q)
        sq = 2.0 * np.sqrt(a) * alpha
        b0 = a * ((a + 1.0) + (a - 1.0) * cw + sq)
        b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cw)
        b2 = a * ((a + 1.0) + (a - 1.0) * cw - sq)
        a0 = (a + 1.0) - (a - 1.0) * cw + sq
        a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cw)
        a2 = (a + 1.0) - (a - 1.0) * cw - sq
    else:
        raise ValueError("未知的滤波器类型: %s" % kind)
    return np.array([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0])


class KWeighting:
    """K-weighting 滤波器组，逐块流式处理，保持 IIR 状态。"""

    def __init__(self, sr=48000, channels=2):
        self.sr = int(sr)
        self.channels = int(channels)
        shelf = _rbj_sos("highshelf", self.sr,
                         f0=1681.974450955533,
                         q=0.7071752369554196,
                         gain_db=3.999843853973347)
        highpass = _rbj_sos("highpass", self.sr,
                            f0=38.13547087602444,
                            q=0.5003270373238773)
        self.sos = np.vstack([shelf, highpass])
        # 关键：初始状态必须为零。若用 sosfilt_zi（单位阶跃稳态）初始化，
        # 零输入也会产生衰减瞬态，表现为启动/切换时的一声"噗"爆音（实测峰值 1.5）。
        self.zi0 = np.zeros((self.sos.shape[0], 2, 1))
        self.zi = np.tile(self.zi0, (1, 1, self.channels))
        self._base = self.zi0  # 用于 reset

    def process(self, block):
        """block: (n, ch) -> 滤波后的 (n, ch)，float64。"""
        x = np.asarray(block, dtype=float)
        if x.ndim == 1:
            x = x[:, None]
        n, ch = x.shape
        if ch != self.channels:
            raise ValueError("声道数不匹配: 期望 %d, 实际 %d" % (self.channels, ch))
        out = np.empty_like(x)
        for c in range(ch):
            out[:, c], self.zi[:, :, c] = sosfilt(
                self.sos, x[:, c], zi=self.zi[:, :, c]
            )
        return out

    def reset(self):
        self.zi = np.tile(self._base, (1, 1, self.channels))


class SlidingLUFS:
    """滑动窗口积分响度（LUFS）。

    对滤波后信号按块计算加权均方，在 window_ms 时间窗内累加，
    按 BS.1770 公式 -0.691 + 10*log10(加权均方) 得到 LUFS。
    """

    def __init__(self, sr=48000, window_ms=1000):
        self.sr = int(sr)
        self.window_ms = float(window_ms)
        self.window_samples = max(int(self.sr * self.window_ms / 1000.0), 1)
        self.blocks = deque()
        self.sum = 0.0
        self.count = 0

    def update(self, filtered_block):
        x = np.asarray(filtered_block, dtype=float)
        if x.ndim == 1:
            x = x[:, None]
        if x.size == 0:
            return self.value()
        ch = x.shape[1]
        # BS.1770-4 的声道权重（WAV 声道顺序）：
        #   5.1 = FL FR FC LFE BL BR  -> 1 1 1 0 1.41 1.41
        #   7.1 = FL FR FC LFE BL BR SL SR -> 1 1 1 0 1.41 1.41 1.41 1.41
        # LFE 必须权重 0（不计入响度），否则低音炮能量会被当成整体响度，
        # 导致低频重的影片/音乐被过度衰减，人声反而变小。
        # 早期版本权重整体错位（把 LFE 计入 1.0、中置算 1.41、环绕算 1.0），
        # 会让 5.1 内容的响度判断偏差数 dB，这里按标准修正。
        if ch == 6:
            weights = np.array([1.0, 1.0, 1.0, 0.0, 1.41, 1.41])
        elif ch == 8:
            weights = np.array([1.0, 1.0, 1.0, 0.0, 1.41, 1.41, 1.41, 1.41])
        else:
            weights = np.ones(ch)
        ms = np.mean(x * x, axis=0)
        weighted = float(np.dot(weights, ms))
        n = x.shape[0]
        self.blocks.append((n, weighted))
        self.sum += weighted * n
        self.count += n
        while len(self.blocks) > 1 and self.count - self.blocks[0][0] > self.window_samples:
            n0, v0 = self.blocks.popleft()
            self.sum -= v0 * n0
            self.count -= n0
        return self.value()

    def value(self):
        mean = self.sum / max(self.count, 1)
        return -0.691 + 10.0 * np.log10(mean + 1e-12)

    def reset(self):
        self.blocks.clear()
        self.sum = 0.0
        self.count = 0
