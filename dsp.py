"""通用 DSP 小工具：分贝换算、包络跟随、增益平滑、峰值限幅、人声突显。

所有函数按块（block）处理，适合实时音频线程使用。
时间常数按"块"更新：dt = blocksize / sr（秒），避免把采样率误当成调用频率。
"""
import numpy as np
from scipy.signal import lfilter



def db_to_lin(db):
    """分贝 -> 线性幅度。"""
    return 10.0 ** (np.asarray(db, dtype=float) / 20.0)


def lin_to_db(lin):
    """线性幅度 -> 分贝。"""
    return 20.0 * np.log10(np.maximum(np.asarray(lin, dtype=float), 1e-12))


class DcBlocker:
    """一阶高通（默认 20 Hz），用于消除直流偏移与次声。

    直流偏移会让扬声器音圈持续偏置、浪费功率，并可能在大音量下产生失真。
    该滤波器对 20 Hz 以上音频影响可忽略（1 kHz 处损耗 < 0.01 dB）。
    """

    def __init__(self, sr=48000, fc=20.0, channels=2):
        self.b = np.array([1.0, -1.0])
        self.a = np.array([1.0, -(1.0 - 2.0 * np.pi * fc / sr)])
        self.zi = np.zeros((1, int(channels)))

    def process(self, block):
        x = np.asarray(block, dtype=float)
        if x.ndim == 1:
            x = x[:, None]
        out = np.empty_like(x)
        for c in range(x.shape[1]):
            out[:, c], self.zi[:, c] = lfilter(self.b, self.a, x[:, c], zi=self.zi[:, c])
        return out

    def reset(self):
        self.zi = np.zeros_like(self.zi)


class EnvelopeFollower:
    """单极点包络跟随器，用于压缩器检测信号电平。

    mode = "peak" 取块内绝对峰值；mode = "rms" 取块内均方根。
    上升沿使用 attack 时间常数，下降沿使用 release 时间常数。
    """

    def __init__(self, mode="peak", attack_ms=10.0, release_ms=150.0,
                 sr=48000, blocksize=512):
        if mode not in ("peak", "rms"):
            raise ValueError("mode 必须是 'peak' 或 'rms'")
        self.mode = mode
        self.sr = int(sr)
        self.blocksize = int(blocksize)
        self.attack_tau = max(attack_ms, 0.1) * 1e-3
        self.release_tau = max(release_ms, 0.1) * 1e-3
        self.state = 0.0

    def _coeff(self, tau, n):
        dt = max(n, 1) / float(self.sr)
        return float(np.exp(-dt / tau))

    def process(self, block):
        block = np.asarray(block, dtype=float)
        if block.size == 0:
            return self.state
        if self.mode == "peak":
            target = float(np.max(np.abs(block)))
        else:
            target = float(np.sqrt(np.mean(block * block)))
        n = len(block) if block.ndim == 1 else block.shape[0]
        coeff = self._coeff(self.attack_tau if target > self.state else self.release_tau, n)
        self.state = coeff * self.state + (1.0 - coeff) * target
        return self.state

    def set_times(self, attack_ms, release_ms):
        """热更新时间常数（保留当前包络状态，避免切换预设时跳变）。"""
        self.attack_tau = max(float(attack_ms), 0.1) * 1e-3
        self.release_tau = max(float(release_ms), 0.1) * 1e-3

    def reset(self):
        self.state = 0.0


class Smoother:
    """标量增益平滑器（一阶低通），方向相关的时间常数。

    默认 attack_on="decrease"：增益下降（加大衰减）快，增益回升（恢复音量）慢。
    这正是压缩器/响度归一化避免"抽吸"效应所需的特性。
    """

    def __init__(self, attack_ms=100.0, release_ms=800.0, sr=48000,
                 blocksize=512, attack_on="decrease"):
        if attack_on not in ("increase", "decrease"):
            raise ValueError("attack_on 必须是 'increase' 或 'decrease'")
        self.sr = int(sr)
        self.blocksize = int(blocksize)
        self.attack_tau = max(attack_ms, 0.1) * 1e-3
        self.release_tau = max(release_ms, 0.1) * 1e-3
        self.attack_on = attack_on
        self.value = None

    def _coeff(self, tau, n):
        dt = max(n, 1) / float(self.sr)
        return float(np.exp(-dt / tau))

    def process(self, target, n=None):
        target = float(target)
        n = n if n is not None else self.blocksize
        if self.value is None:
            self.value = target
            return self.value
        fast = (target < self.value) if self.attack_on == "decrease" else (target > self.value)
        coeff = self._coeff(self.attack_tau if fast else self.release_tau, n)
        self.value = coeff * self.value + (1.0 - coeff) * target
        return self.value

    def set_times(self, attack_ms, release_ms):
        """热更新时间常数（保留当前增益值，避免切换预设时音量跳变）。"""
        self.attack_tau = max(float(attack_ms), 0.1) * 1e-3
        self.release_tau = max(float(release_ms), 0.1) * 1e-3

    def reset(self):
        self.value = None


class Limiter:
    """峰值限幅器：瞬时压低超过天花板的峰值，缓慢释放。

    ceiling_db 默认 -1 dBFS，给数模转换留出余量，防止削波。
    """

    def __init__(self, ceiling_db=-1.0, release_ms=100.0, sr=48000, blocksize=512):
        self.ceiling_db = float(ceiling_db)
        self.ceiling = float(db_to_lin(ceiling_db))
        self.sr = int(sr)
        self.blocksize = int(blocksize)
        self.release_tau = max(release_ms, 0.1) * 1e-3
        self.gain = 1.0

    def process(self, block):
        block = np.asarray(block, dtype=float)
        if block.size == 0:
            return block
        n = len(block) if block.ndim == 1 else block.shape[0]
        dt = max(n, 1) / float(self.sr)
        release = float(np.exp(-dt / self.release_tau))
        peak = float(np.max(np.abs(block)))
        target = min(1.0, self.ceiling / peak) if peak > 1e-12 else 1.0
        # 需要更强衰减时立即动作，恢复时缓慢释放
        coeff = 0.0 if target < self.gain else release
        self.gain = coeff * self.gain + (1.0 - coeff) * target
        return block * self.gain

    def set_ceiling(self, ceiling_db):
        """热更新限幅天花板（保留当前增益状态）。"""
        self.ceiling_db = float(ceiling_db)
        self.ceiling = float(db_to_lin(self.ceiling_db))

    def reset(self):
        self.gain = 1.0


class Biquad:
    """二阶 IIR 滤波器（RBJ Audio EQ Cookbook 系数），多声道一次滤波。

    kind: "peaking" 钟形均衡 / "lowshelf" 低架 / "highshelf" 高架 / "highpass" 高通。
    用 lfilter 的 axis 参数一次处理所有声道，避免逐声道 Python 循环。

    稳定性保护：极点必须严格在单位圆内。极端采样率（8kHz 下把 5kHz 架式滤波器
    放到奈奎斯特附近）会让极点跑到圆外，滤波器输出指数发散——那听起来是持续的
    巨大噪声，比不做处理糟糕得多。因此构造时校验，失败就退化为直通。
    """

    KINDS = ("peaking", "lowshelf", "highshelf", "highpass")

    def __init__(self, kind, sr, fc, q=0.7071, gain_db=0.0, channels=1):
        if kind not in self.KINDS:
            raise ValueError("不支持的滤波器类型：%s" % kind)
        self.kind = kind
        self.sr = max(float(sr), 8000.0)
        self.channels = max(int(channels), 1)
        # 截止/中心频率必须留在安全区间内，否则 tan/alpha 失去意义
        self.fc = float(min(max(fc, 20.0), self.sr * 0.45))
        self.q = float(min(max(q, 0.1), 18.0))
        self.gain_db = float(gain_db)
        self.b, self.a = self._design()
        self.zi = np.zeros((max(len(self.a), len(self.b)) - 1, self.channels))

    def _design(self):
        """按 RBJ 公式计算系数，并做稳定性校验。"""
        sr = self.sr
        w0 = 2.0 * np.pi * self.fc / sr
        cos_w = float(np.cos(w0))
        sin_w = float(np.sin(w0))
        A = float(10.0 ** (self.gain_db / 40.0))

        if self.kind == "peaking":
            alpha = sin_w / (2.0 * self.q)
            b0 = 1.0 + alpha * A
            b1 = -2.0 * cos_w
            b2 = 1.0 - alpha * A
            a0 = 1.0 + alpha / A
            a1 = -2.0 * cos_w
            a2 = 1.0 - alpha / A
        elif self.kind == "highpass":
            alpha = sin_w / (2.0 * self.q)
            b0 = (1.0 + cos_w) / 2.0
            b1 = -(1.0 + cos_w)
            b2 = (1.0 + cos_w) / 2.0
            a0 = 1.0 + alpha
            a1 = -2.0 * cos_w
            a2 = 1.0 - alpha
        elif self.kind == "lowshelf":
            alpha = sin_w / 2.0 * np.sqrt(2.0)   # S = 1（临界阻尼斜率）
            two_sqrt_a_alpha = 2.0 * np.sqrt(A) * alpha
            b0 = A * ((A + 1.0) - (A - 1.0) * cos_w + two_sqrt_a_alpha)
            b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w)
            b2 = A * ((A + 1.0) - (A - 1.0) * cos_w - two_sqrt_a_alpha)
            a0 = (A + 1.0) + (A - 1.0) * cos_w + two_sqrt_a_alpha
            a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cos_w)
            a2 = (A + 1.0) + (A - 1.0) * cos_w - two_sqrt_a_alpha
        else:  # highshelf
            alpha = sin_w / 2.0 * np.sqrt(2.0)
            two_sqrt_a_alpha = 2.0 * np.sqrt(A) * alpha
            b0 = A * ((A + 1.0) + (A - 1.0) * cos_w + two_sqrt_a_alpha)
            b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w)
            b2 = A * ((A + 1.0) + (A - 1.0) * cos_w - two_sqrt_a_alpha)
            a0 = (A + 1.0) - (A - 1.0) * cos_w + two_sqrt_a_alpha
            a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cos_w)
            a2 = (A + 1.0) - (A - 1.0) * cos_w - two_sqrt_a_alpha

        if not np.isfinite([b0, b1, b2, a0, a1, a2]).all() or abs(a0) < 1e-12:
            return np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0])
        b = np.array([b0, b1, b2]) / a0
        a = np.array([a0, a1, a2]) / a0
        # 极点校验：|z| 必须 < 1，否则滤波器发散
        poles = np.roots(a)
        if poles.size and float(np.max(np.abs(poles))) >= 0.9999:
            return np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0])
        return b, a

    def process(self, block):
        """block: (n, ch) -> 同形状滤波结果。"""
        x = np.asarray(block, dtype=float)
        if x.size == 0:
            return x
        if x.ndim == 1:
            x = x[:, None]
        out, self.zi = lfilter(self.b, self.a, x, axis=0, zi=self.zi)
        return out

    def reset(self):
        self.zi = np.zeros_like(self.zi)

    def gain_at(self, freq):
        """某频率处的幅度响应（dB），仅用于自检/测试。"""
        w = 2.0 * np.pi * float(freq) / self.sr
        z = np.exp(-1j * w)
        num = self.b[0] + self.b[1] * z + self.b[2] * z ** 2
        den = self.a[0] + self.a[1] * z + self.a[2] * z ** 2
        return float(20.0 * np.log10(max(abs(num / den), 1e-12)))


# 人声突显强度档位：presence（2.6kHz 咬字清晰度）、low（180Hz 以下收敛）、
# air（5kHz 齿音/气息）、width（立体声宽度系数，越小越"贴脸"）。
VOICE_LEVELS = {
    1: {"presence_db": 3.0, "low_db": -1.5, "air_db": 1.0, "width": 0.85},
    2: {"presence_db": 4.5, "low_db": -2.5, "air_db": 1.5, "width": 0.72},
    3: {"presence_db": 6.0, "low_db": -3.5, "air_db": 2.0, "width": 0.60},
}


class VoiceEnhancer:
    """人声突显：中频咬字提升 + 低频收敛 + 立体声宽度收敛。

    为什么是这三件事：
      * 人声的能量集中在 200Hz~4kHz，2~4kHz 决定"咬字是否清楚"，提升这里最直接；
      * 电影/游戏的爆炸声、低频轰鸣占据大量能量，收敛 180Hz 以下能给中频让位；
      * 人声在混音里基本居中，收窄立体声宽度等于把居中成分相对抬高，
        这是"对白更靠前"最省事、也最不容易失真的做法（M/S 重构是精确的）。

    切换保护：改开关或改档位时不做硬切，而是先把增强量线性淡到 0（干声），
    在 0 处替换滤波器系数，再淡回来。整个过程约 128ms，只有音色渐变，没有咔哒声。
    """

    FADE_BLOCKS = 6   # 与引擎的淡入淡出块数一致
    DEFAULT_HZ = {"presence": 2600.0, "low": 180.0, "air": 5000.0}

    def __init__(self, sr=48000, channels=2, blocksize=512):
        self.sr = int(sr) if int(sr) >= 8000 else 48000
        self.channels = max(int(channels), 1)
        self.blocksize = max(int(blocksize), 1)
        self.enabled = False
        self.strength = 2
        self.mix = 0.0
        self._pending = None
        self._spec = self._spec_for(2)
        self._build(self._spec)

    # ---------- 参数 ----------
    def _spec_for(self, strength, presence_db=None, low_db=None,
                  air_db=None, width=None,
                  presence_hz=None, low_hz=None, air_hz=None):
        """把档位 + 手工覆盖值整理成一份完整参数（含频率夹取）。"""
        level = VOICE_LEVELS.get(int(strength), VOICE_LEVELS[2])
        spec = {
            "strength": int(strength) if int(strength) in VOICE_LEVELS else 2,
            "presence_hz": float(presence_hz or self.DEFAULT_HZ["presence"]),
            "presence_db": float(level["presence_db"] if presence_db is None else presence_db),
            "low_hz": float(low_hz or self.DEFAULT_HZ["low"]),
            "low_db": float(level["low_db"] if low_db is None else low_db),
            "air_hz": float(air_hz or self.DEFAULT_HZ["air"]),
            "air_db": float(level["air_db"] if air_db is None else air_db),
            "width": float(level["width"] if width is None else width),
        }
        # 频率夹取：必须低于奈奎斯特，且留出余量
        nyq = self.sr * 0.45
        spec["presence_hz"] = min(max(spec["presence_hz"], 800.0), nyq)
        spec["low_hz"] = min(max(spec["low_hz"], 40.0), min(600.0, nyq))
        spec["air_hz"] = min(max(spec["air_hz"], 2000.0), nyq)
        # 增益夹取：过度提升会明显失真，也不符合"人声突显"的定位
        spec["presence_db"] = min(max(spec["presence_db"], 0.0), 12.0)
        spec["low_db"] = min(max(spec["low_db"], -12.0), 0.0)
        spec["air_db"] = min(max(spec["air_db"], -6.0), 6.0)
        spec["width"] = min(max(spec["width"], 0.0), 1.0)
        return spec

    def _build(self, spec):
        # 5.1/7.1 的第 4 声道是 LFE，单独绕开；滤波器按"实际处理声道数"构造，
        # 否则 zi 形状与输入不匹配会直接抛异常。
        if self.channels >= 6:
            self._idx = [c for c in range(self.channels) if c != 3]
        else:
            self._idx = None
        proc_ch = len(self._idx) if self._idx is not None else self.channels
        self.presence = Biquad("peaking", self.sr, spec["presence_hz"], q=1.0,
                               gain_db=spec["presence_db"], channels=proc_ch)
        self.low = Biquad("lowshelf", self.sr, spec["low_hz"],
                          gain_db=spec["low_db"], channels=proc_ch)
        self.air = Biquad("highshelf", self.sr, spec["air_hz"],
                          gain_db=spec["air_db"], channels=proc_ch)
        self.width = spec["width"]

    @staticmethod
    def _coerce_strength(value):
        try:
            number = int(round(float(value)))
        except (TypeError, ValueError):
            return 2
        return number if number in VOICE_LEVELS else min(max(number, 1), 3)

    def configure(self, enabled, strength=2, **overrides):
        """由配置驱动：开关立即生效，参数变化走"淡出-替换-淡入"。"""
        strength = self._coerce_strength(strength)
        spec = self._spec_for(strength, **overrides)
        self.enabled = bool(enabled)
        if spec != self._spec:
            # 不在本块直接换系数：先记录待切换参数，等增强量淡到 0 再换
            self._pending = spec

    # ---------- 处理 ----------
    def _process_wet(self, x):
        """湿声：三段 EQ + 立体声宽度收敛（LFE 声道不参与）。"""
        ch = x.shape[1]
        if self._idx is not None and ch == self.channels and ch >= 6:
            sub = self.presence.process(x[:, self._idx])
            sub = self.low.process(sub)
            sub = self.air.process(sub)
            out = x.copy()
            out[:, self._idx] = sub
        else:
            out = self.presence.process(x)
            out = self.low.process(out)
            out = self.air.process(out)
        if ch == 2 and self.width < 0.999:
            # M/S 精确重构：宽度收窄 = 居中(人声)成分相对抬高
            mid = (out[:, 0] + out[:, 1]) * 0.5
            side = (out[:, 0] - out[:, 1]) * 0.5 * self.width
            out = np.empty_like(out)
            out[:, 0] = mid + side
            out[:, 1] = mid - side
        return out

    def process(self, block):
        x = np.asarray(block, dtype=float)
        if x.size == 0:
            return block
        ndim = x.ndim
        if ndim == 1:
            x = x[:, None]
        # 始终计算湿声：停用期间也保持滤波器状态温热，否则重新启用时的
        # 陈旧状态（甚至全零状态）会产生一段可听的过渡噪声。
        wet = self._process_wet(x)

        target = 1.0 if (self.enabled and self._pending is None) else 0.0
        step = 1.0 / (self.FADE_BLOCKS * 2.0)
        if self.mix < target:
            self.mix = min(target, self.mix + step)
        elif self.mix > target:
            self.mix = max(target, self.mix - step)
        if self._pending is not None and self.mix <= 0.0:
            self._spec = self._pending
            self._pending = None
            self._build(self._spec)
            wet = self._process_wet(x)   # 用新系数重算本块，避免输出旧参数结果

        if self.mix <= 0.0:
            out = x            # 位精确直通（未启用/参数切换间隙）
        elif self.mix >= 1.0:
            out = wet
        else:
            out = x * (1.0 - self.mix) + wet * self.mix
        if ndim == 1:
            out = out[:, 0]
        return out

    def reset(self):
        self.presence.reset()
        self.low.reset()
        self.air.reset()
        self.mix = 0.0

    def status(self):
        return {
            "enabled": bool(self.enabled),
            "strength": int(self._spec.get("strength", 2)),
            "mix": float(self.mix),
            "switching": self._pending is not None,
        }

