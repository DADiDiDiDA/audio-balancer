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
        # 一次调用处理所有声道（axis=0），省掉按声道循环的固定开销
        out, self.zi = lfilter(self.b, self.a, x, axis=0, zi=self.zi)
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



class ClickGuard:
    """块边界去咔哒，只在**真正不连续**时才动手。

    咔哒的本质是波形出现阶跃（相邻采样跳变很大）。三个来源：
      1. 队列欠载时直接输出整块 0 —— 从有声瞬间跳到 0；
      2. 采集端队列满时整块丢弃 —— 剩下的块与上一块接缝不连续；
      3. 增益/限幅参数突变 —— 块首样本与上块尾样本对不上。

    做法：
      · smooth()：先比较「块首样本」和「上块末样本」。连续时**原样通过、一个采样都不改**；
        只有跳变明显超出这段音频自身的正常步进（上块块内最大步进 ×2.5）时才淡化，
        或者调用方明确告知「这块前面被丢过 / 刚从静音恢复」（discontinuity=True / 内部静音标记）。
      · silence()：欠载时输出「从上次电平淡出到 0」的块，而不是硬切。

    ⚠️ 历史教训（v1.6.3）：最早的版本对**每一块**都无条件交叉淡化，等于每 15 ms
    把波形朝「上块最后一个采样值」拉 2 ms —— 对恒定电平无所谓，对真实波形就是
    周期性挖洞：1 kHz 正弦实测失真 -13 dB，听感是持续的轻微「刺啦」。
    所以「是否连续」必须真的判断，不能靠淡化碰运气。
    """

    def __init__(self, samplerate, fade_ms=2.0, channels=2,
                 disc_factor=2.5, disc_floor=0.05):
        self.channels = int(channels)
        self.n = max(2, int(float(samplerate) * float(fade_ms) / 1000.0))
        self._up = np.linspace(0.0, 1.0, self.n, dtype=np.float64)[:, None]
        self._down = self._up[::-1].copy()
        self.last = None          # 上一块最后一个样本 (channels,)
        self.disc_factor = float(disc_factor)
        self.disc_floor = float(disc_floor)
        self._prev_step = 0.0     # 上块块内最大相邻采样步进（衡量"正常步进"）
        self._limit = self.disc_floor
        self._muted = False       # 上一块是欠载静音 → 恢复时必然不连续
        self.fades = 0            # 实际触发淡化的次数（诊断用）

    def reset(self):
        self.last = None
        self._prev_step = 0.0
        self._limit = self.disc_floor
        self._muted = False

    def _prepare(self, block):
        b = np.asarray(block, dtype=np.float64)
        if b.ndim == 1:
            b = b[:, None]
        if b.shape[1] != self.channels:
            if b.shape[1] > self.channels:
                b = b[:, :self.channels]
            else:
                b = np.pad(b, ((0, 0), (0, self.channels - b.shape[1])))
        return b

    def smooth(self, block, discontinuity=False):
        """连续时原样返回（零改动）；检测到/被告知不连续才在块首交叉淡化。"""
        b = self._prepare(block)
        frames = b.shape[0]
        out = b                    # 绝大多数块是连续的：不做任何拷贝
        if frames > 0:
            fade = bool(discontinuity or self._muted)
            if not fade and self.last is not None:
                step = float(np.max(np.abs(b[0] - self.last)))
                fade = step > self._limit
            if fade and self.last is not None:
                out = b.copy()     # 只有真要淡化时才复制
                n = min(self.n, frames)
                ramp = self._up[:n]
                out[:n] = out[:n] * ramp + self.last[None, :] * (1.0 - ramp)
                self.fades += 1
            self.last = out[-1].copy()
        # 记录本块"正常步进"，作为下一块的连续性判据
        if frames > 1:
            d = np.diff(b, axis=0)
            m = float(np.max(np.abs(d)))
            self._prev_step = m if np.isfinite(m) else 0.0
            self._limit = max(self.disc_floor, self.disc_factor * self._prev_step)
        self._muted = False
        return out

    def silence(self, frames):
        """欠载块：从上次电平短淡出到 0，避免阶跃。"""
        frames = int(frames)
        out = np.zeros((frames, self.channels), dtype=np.float64)
        if self.last is not None and frames > 0:
            n = min(self.n, frames)
            out[:n] = self.last[None, :] * self._down[:n]
        self.last = np.zeros(self.channels, dtype=np.float64)
        self._prev_step = 0.0
        self._limit = self.disc_floor
        self._muted = True
        return out

    def fade_to_zero(self, block):
        """块尾淡出到 0（停止/切设备前用）。"""
        b = self._prepare(block).copy()
        if b.shape[0] > 0:
            n = min(self.n, b.shape[0])
            b[-n:] = b[-n:] * self._down[self.n - n:]
            self.last = np.zeros(b.shape[1], dtype=np.float64)
        return b


_DOWNMIX_CACHE = {}


# ---------------------------------------------------------------- 真峰值限幅
def _lagrange_coeffs(npoints=8, ts=(0.25, 0.5, 0.75)):
    """n 点拉格朗日插值在 t=0.25/0.5/0.75 处的系数（t 位于中间两个采样之间）。

    用于估算"采样点之间"的峰值：正弦被采样时，样本峰值可能比真实峰值低
    0.5~3 dB（取决于频率/相位）。若只按样本峰值限幅，转成 AAC/MP3 后
    容易出现"看着没削波、解码后削波"——这正是真峰值（True Peak）要解决的问题。

    为什么用 8 点而不是 4 点：4 点三次插值在 fs/4（12kHz）附近最坏会低估
    1 dB 以上，等于天花板形同虚设；8 点（7 阶）实测最坏 0.2 dB，配合
    tp_margin_db 的余量足够安全。代价是每块多几十微秒。
    """
    pos = np.arange(npoints) - (npoints // 2 - 1)   # 8 点：-3..4
    out = []
    for t in ts:
        coeffs = []
        for i, xi in enumerate(pos):
            num, den = 1.0, 1.0
            for j, xj in enumerate(pos):
                if i == j:
                    continue
                num *= (t - xj)
                den *= (xi - xj)
            coeffs.append(num / den)
        out.append(tuple(coeffs))
    return out


_TP_NPTS = 8
_TP_COEFFS = _lagrange_coeffs(_TP_NPTS)
_TP_MATRIX = np.array(_TP_COEFFS).T          # (8, 3)：8 个抽头 × 3 个插值位置


class TruePeakLimiter:
    """前瞻式真峰值限幅器（立体声联动，块间连续）。

    为什么不用"对每个采样做 tanh/软膝"的波形整形：
      波形整形会把峰顶压扁，等于对信号做非线性失真；一旦增益提得多、
      限幅器长期在工作，听感就是"发毛、动态被压扁"。正确做法是**降增益**，
      让波形形状保持原样，只是整体变小一点。

    结构：
      1. 真峰值检测：4 点三次插值估出采样间的峰值（4× 过采样的廉价近似）；
      2. 前瞻：内部延迟 lookahead_ms（默认 2ms），用"未来"的峰值提前开始压，
         这样峰值本身不会被削掉；
      3. 瞬时下压 + 慢释放（release_ms）：增益绝不阶跃；
      4. 立体声联动：用两个声道的最大值算一个增益，避免声像被拉扯。
    代价只有 lookahead_ms 的延迟。
    """

    def __init__(self, samplerate=48000, channels=2, ceiling_db=-1.0,
                 lookahead_ms=2.0, release_ms=120.0, tp_margin_db=0.3):
        self.sr = int(samplerate)
        self.channels = max(int(channels), 1)
        self.ceiling_db = float(ceiling_db)
        # 8 点插值在 fs/4 这类"采样点正好错过峰顶"的频率上仍可能低估约 0.2 dB，
        # 所以内部按天花板再低 tp_margin_db 来限，保证外部量到的真峰值不越线。
        self.tp_margin_db = float(tp_margin_db)
        self.ceiling = float(10.0 ** ((self.ceiling_db - self.tp_margin_db) / 20.0))
        self.n = max(8, int(round(self.sr * max(float(lookahead_ms), 0.5) / 1000.0)))
        self.rel = float(np.exp(-1.0 / max(1.0, self.sr * float(release_ms) / 1000.0)))
        self.buf = np.zeros((self.n, self.channels), dtype=np.float64)
        self.g = 1.0
        self.last_tp = 0.0        # 本块输出的真峰值（线性）
        self.last_gr_db = 0.0     # 本块最大增益衰减（dB，≤ 0）
        self.engaged = 0          # 触发限幅的块数（诊断）

    # ---------- 参数 ----------
    def set_ceiling(self, ceiling_db):
        self.ceiling_db = float(ceiling_db)
        self.ceiling = float(10.0 ** ((self.ceiling_db - self.tp_margin_db) / 20.0))

    def reset(self):
        self.buf[:] = 0.0
        self.g = 1.0
        self.last_tp = 0.0
        self.last_gr_db = 0.0

    # ---------- 处理 ----------
    def _peaks(self, x):
        """每个采样位置的"局部真峰值"：样本绝对值与三处插值结果取最大。

        优化：样本峰值离天花板还差 4 dB 以上时直接跳过插值——插值最多把峰值
        抬高约 3 dB（fs/2 附近的极端情况），不可能越线，没必要白算。
        正常音量下这一支省掉整块 100µs 的插值开销。
        """
        p = np.abs(x).max(axis=1)
        if float(p.max()) < self.ceiling * 0.6:
            return p
        size = x.shape[0]
        half = _TP_NPTS // 2 - 1          # 8 点：中间两个样本前有 3 个历史样本
        span = size - _TP_NPTS + 1
        if span <= 0:
            return p
        # 一次矩阵乘算出 3 个相位的插值值：(span, ch, 8) @ (8, 3) → (span, ch, 3)
        win = np.lib.stride_tricks.sliding_window_view(x, _TP_NPTS, axis=0)
        vals = np.abs(win @ _TP_MATRIX).max(axis=(1, 2))
        p[half:half + span] = np.maximum(p[half:half + span], vals)
        return p

    def process(self, block):
        x = np.asarray(block, dtype=np.float64)
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[0] == 0:
            return x
        if x.shape[1] != self.channels:
            x = x[:, :self.channels] if x.shape[1] > self.channels else np.pad(
                x, ((0, 0), (0, self.channels - x.shape[1])))
        frames = x.shape[0]
        ext = np.concatenate((self.buf, x), axis=0)
        peaks = self._peaks(ext)
        # 需要多小的增益才不越过天花板（峰值低于天花板时是 1.0）
        req = np.minimum(1.0, self.ceiling / np.maximum(peaks, 1e-12))
        # 前瞻：输出第 i 个采样时，看的是 ext[i .. i+n] 这段的峰值
        win = np.lib.stride_tricks.sliding_window_view(req, self.n + 1)
        need = win.min(axis=-1)[:frames]

        if float(need.min()) >= 1.0 and self.g >= 0.999999:
            out = ext[:frames]
            self.last_gr_db = 0.0
            self.last_tp = float(np.max(peaks[:frames]))
        else:
            # 增益包络：瞬时下压（need 变小立刻跟随）、慢释放（往 1.0 指数回升）。
            # 令 u = 1 - g（衰减量），递推就是 u[i] = max(u0[i], u[i-1] * rel)，
            # 闭式解 u[i] = rel^i * max_{j<=i}(u0[j] / rel^j)，可用累加最大值整块算，
            # 省掉逐个采样的 Python 循环（10ms 块下约 70µs）。
            u0 = 1.0 - need
            rk = self._rel_pow(frames)
            u = np.maximum.accumulate(u0 / rk) * rk
            # 上一块的残留衰减也要参与释放（否则每块开头都会突然恢复满增益）
            u = np.maximum(u, (1.0 - self.g) * self.rel * rk)
            g = 1.0 - u
            self.g = float(g[-1])
            out = ext[:frames] * g[:, None]
            self.last_gr_db = float(20.0 * np.log10(max(float(g.min()), 1e-6)))
            self.last_tp = float(np.max(peaks[:frames] * g))
            self.engaged += 1

        self.buf = ext[frames:].copy()
        return out

    def _rel_pow(self, n):
        """rel^0..rel^(n-1)，按块长缓存（块长固定时每块省一次 pow）。"""
        cache = self.__dict__.get("_rp")
        if cache is not None and cache[0] == n:
            return cache[1]
        arr = self.rel ** np.arange(n, dtype=np.float64)
        self.__dict__["_rp"] = (n, arr)
        return arr

    def status(self):
        return {"tp": self.last_tp, "gr_db": self.last_gr_db,
                "engaged": self.engaged, "lookahead_ms": self.n * 1000.0 / self.sr}


def _downmix_matrix(n):
    """按声道数生成 n×2 的下混矩阵（缓存，块间复用）。"""
    m = _DOWNMIX_CACHE.get(n)
    if m is not None:
        return m
    m = np.zeros((n, 2))
    if n >= 1:
        m[0, 0] = 1.0
    if n >= 2:
        m[1, 1] = 1.0
    if n >= 3:                      # FC 中置
        m[2, 0] = m[2, 1] = 0.70710678
    if n >= 6:                      # BL BR 后环绕
        m[4, 0] = 0.70710678
        m[5, 1] = 0.70710678
    if n >= 8:                      # SL SR 侧环绕
        m[6, 0] = 0.70710678
        m[7, 1] = 0.70710678
    _DOWNMIX_CACHE[n] = m
    return m


def downmix_to_stereo(block, assume=2):
    """多声道按 ITU-R BS.775 下混到立体声。

    为什么需要：CABLE 若在 Windows 里配成 5.1/7.1，回环采集拿到的是多声道，
    只取前两个声道会**丢掉中置（人声）和环绕**——表现为「背景音有、人声很小」。

    系数设计（关键）：
      · 前左/前右 保持 1.0 —— 立体声内容原封不动直通，听感和不处理完全一样；
      · 中置、后环绕、侧环绕 取 0.707（-3dB），按 BS.775 折进左右；
      · LFE 不计（和响度测量的权重一致，避免低频过载）。
    声道顺序按 WAVEFORMATEXTENSIBLE：FL FR FC LFE BL BR SL SR ...

    实现：预生成 n×2 矩阵后一次矩阵乘。原来是逐个声道切片相加
    （实测 480×8 的块 17µs），矩阵乘走 BLAS 只要 2.6µs，结果完全一致
    （最大差异 3e-17，只是浮点求和顺序不同）。
    """
    b = np.asarray(block, dtype=np.float64)
    if b.ndim == 1:
        b = b[:, None]
    n = b.shape[1]
    if n <= 0:
        return np.zeros((b.shape[0], 2), dtype=np.float64)
    if n == 1:
        return np.repeat(b, 2, axis=1)
    return np.dot(b, _downmix_matrix(n))
