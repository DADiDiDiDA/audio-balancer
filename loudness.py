"""ITU-R BS.1770-4 实时响度测量。

包含：
  * K-weighting 滤波（预滤波 high-shelf + RLB high-pass），双二阶级联，保留滤波状态；
  * 滑动窗口 LUFS 计算（立体声权重 1/1，5.1 权重 1/1/1.41/1/1/1.41）；
  * 两级门控：绝对 -70 LUFS + 相对 -10 LU（v1.8.0），避免长静音把测量拉低。

参考：EBU R128 / ITU-R BS.1770-4 标准系数。
"""
from collections import deque

import numpy as np
from scipy.signal import sosfilt, sosfilt_zi


# ---------------------------------------------------------------- 标准系数
# ITU-R BS.1770-4 的两个 K 加权滤波器参数（Table 1/2 的模拟原型参数）。
# 采样率**必须**参与设计：转折频率是按 fs 算的，直接拿 48k 的系数去处理
# 44.1k/96k 的声音，两个转折点会整体偏移 8%~100%（实测宽带内容读数偏
# 0.09 dB，高频多的内容更多），而且越偏离 48k 越明显。
SHELF_F0 = 1681.974450955533
SHELF_GAIN_DB = 3.999843853973347
SHELF_Q = 0.7071752369554196
HP_F0 = 38.13547087602444
HP_Q = 0.5003270373238773
# 标准里高架滤波器的"中频增益"指数不是 0.5，而是这个值 —— 用它才能在
# 48kHz 下逐位复现 BS.1770-4 公布的系数表（差 <1e-15）。
_VB_EXP = 0.4996667741545416


def _shelf_sos(fs):
    """第一级：高架滤波器（+4 dB @ 高频），双线性变换 + 频率预畸变。"""
    k = np.tan(np.pi * SHELF_F0 / fs)
    vh = 10.0 ** (SHELF_GAIN_DB / 20.0)
    vb = vh ** _VB_EXP
    a0 = 1.0 + k / SHELF_Q + k * k
    return np.array([(vh + vb * k / SHELF_Q + k * k) / a0,
                     2.0 * (k * k - vh) / a0,
                     (vh - vb * k / SHELF_Q + k * k) / a0,
                     1.0,
                     2.0 * (k * k - 1.0) / a0,
                     (1.0 - k / SHELF_Q + k * k) / a0])


def _highpass_sos(fs):
    """第二级：RLB 高通（38 Hz），同样的预畸变双线性设计。"""
    k = np.tan(np.pi * HP_F0 / fs)
    a0 = 1.0 + k / HP_Q + k * k
    return np.array([1.0, -2.0, 1.0,
                     1.0,
                     2.0 * (k * k - 1.0) / a0,
                     (1.0 - k / HP_Q + k * k) / a0])


def k_weighting_sos(fs=48000):
    """返回两段 sos。48kHz 下与 BS.1770-4 公布系数逐位一致。"""
    return np.vstack([_shelf_sos(float(fs)), _highpass_sos(float(fs))])


class KWeighting:
    """K-weighting 滤波器组，逐块流式处理，保持 IIR 状态。

    系数按实际采样率现算（见 k_weighting_sos），不是写死 48k 的表。
    """

    def __init__(self, sr=48000, channels=2):
        self.sr = int(sr)
        self.channels = int(channels)
        self.sos = k_weighting_sos(self.sr)
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
        # 一次调用处理所有声道（axis=0）：scipy 每次调用有固定开销，
        # 两声道各调一次实测 ~90µs/块，合并后 ~50µs。zi 形状正好是
        # (sections, 2, channels)，与按轴过滤的约定一致。
        out, self.zi = sosfilt(self.sos, x, axis=0, zi=self.zi)
        return out

    def reset(self):
        self.zi = np.tile(self._base, (1, 1, self.channels))


class SlidingLUFS:
    """滑动窗口积分响度（LUFS），带 BS.1770-4 的两级门控。

    对滤波后信号按块计算加权均方，在 window_ms 时间窗内累加，按 BS.1770 公式
    -0.691 + 10*log10(加权均方) 得到 LUFS。

    为什么要门控（v1.8.0）：
      不做门控时，窗口里的静音/极弱段会把平均值直接拉低——长静音之后、
      或者音乐里的停顿、片头片尾，测出来的"响度"能低 10 dB 以上，
      均衡器于是拼命往上提，一有声音就过响，随后又被压下来，就是"响度跳变"。
      BS.1770 的做法是两级门控：
        1) 绝对门限 -70 LUFS：低于它的块根本不参与（这是"没节目"而不是"小声"）；
        2) 相对门限：比（过绝对门限后的）平均值低 10 LU 以上的块也剔除，
           这样只有"真正在发声"的段落决定响度。
      本实现把这两级门控放进滑动窗口里做，作为实时短时响度使用
      （标准里的积分响度是 400ms 块 + 75% 重叠，此处窗口可配、默认 1 秒）。
    """

    ABS_GATE_LUFS = -70.0
    REL_GATE_LU = -10.0

    def __init__(self, sr=48000, window_ms=1000):
        self.sr = int(sr)
        self.window_ms = float(window_ms)
        self.window_samples = max(int(self.sr * self.window_ms / 1000.0), 1)
        self.blocks = deque()
        self.sum = 0.0
        self.count = 0
        # 绝对门限对应的加权均方：LUFS = -0.691 + 10log10(z)
        self._abs_z = float(10.0 ** ((self.ABS_GATE_LUFS + 0.691) / 10.0))

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
        # 窗口严格不超过 window_samples：以前的条件会在窗口里多留一块，
        # 50ms 块时等效窗口 1.05s，门控后的"大声记忆"也跟着多留一块。
        while len(self.blocks) > 1 and self.count > self.window_samples:
            n0, v0 = self.blocks.popleft()
            self.sum -= v0 * n0
            self.count -= n0
        return self.value()

    def value(self):
        """门控后的 LUFS；窗口里没有有效节目时返回 -70.0（不是 -120）。"""
        blocks = self.blocks
        if not blocks:
            return self.ABS_GATE_LUFS
        # 第一级：绝对门限
        n_sum = 0.0
        z_sum = 0.0
        for n, z in blocks:
            if z >= self._abs_z:
                n_sum += n
                z_sum += z * n
        if n_sum <= 0.0:
            return self.ABS_GATE_LUFS
        lufs_abs = -0.691 + 10.0 * np.log10(z_sum / n_sum)
        # 第二级：相对门限（比过绝对门限后的平均值低 10 LU 以上剔除）
        rel_z = float(10.0 ** ((lufs_abs + self.REL_GATE_LU + 0.691) / 10.0))
        n2 = 0.0
        z2 = 0.0
        for n, z in blocks:
            if z >= rel_z:
                n2 += n
                z2 += z * n
        if n2 <= 0.0:
            return float(lufs_abs)
        return float(-0.691 + 10.0 * np.log10(z2 / n2))

    def reset(self):
        self.blocks.clear()
        self.sum = 0.0
        self.count = 0
