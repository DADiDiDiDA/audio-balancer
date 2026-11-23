"""音频引擎：CABLE 回环 -> 响度均衡 -> 真实输出设备。

链路：
    播放器 -> CABLE Input（系统默认输出）
    -> 本引擎从 CABLE 的渲染端回环采集
    -> 去直流 + ITU-R BS.1770 测响度 + 只压不升的增益 + 软限幅
    -> 送到耳机/扬声器

为什么不会变调：
  * 采集与播放都用 48kHz / 480 帧（10ms，与 WASAPI 设备周期对齐）；
  * 采集线程与播放线程之间用有界队列解耦，时钟漂移由队列吸收
    （满则丢最旧、空则补静音），不会出现"同一块播两次"的电音；
  * 测量发生在增益之前，所以增益不会反过来影响测量，控制天然收敛。

为什么不碰系统音量：
  音量只在本引擎内部乘增益，绝不修改端点主音量或会话音量。
  自动静音是另一模块，只动被判定为"后台"的会话。
"""
import queue
import threading
import time
import traceback

import ctypes

import numpy as np

# comtypes(pycaw) 必须先于 soundcard 初始化，否则两个库的 COM 单元模式会打架
from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
from pycaw.pycaw import AudioSession, AudioUtilities  # noqa: F401
import soundcard as sc

import session
from dsp import ClickGuard, DcBlocker, TruePeakLimiter, downmix_to_stereo
from loudness import KWeighting, SlidingLUFS
from wasapi_raw import RawPlayer

SR = 48000
# 采集/播放块长。原来固定 480 帧（10ms）= 设备周期 100%，零余量 —— GUI、
# 静音枚举等线程只要占住 GIL 几十毫秒，播放线程就来不及填缓冲，设备插静音，
# 听感就是断续/电音；后来改成 4800（100ms）倒是稳，但端到端延迟 300ms+，
# 看视频/打游戏口型对不上。
# 折中：默认 33ms（1600 帧），再把 WASAPI 播放缓冲压到 ~50ms，
# 端到端约 120-150ms；觉得偶尔有咔哒声可以在「音频设备」里把缓冲调回 60-100ms。
BLOCK = 1600
CHANNELS = 2


def ensure_com():
    """每个用 soundcard 的线程都必须自己初始化 COM（单元是线程级的）。"""
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0)   # MTA
    return (hr + 2 ** 32) not in (0, 0x80010106)


def boost_current_thread():
    """把当前音频线程提到最高优先级。

    WASAPI 的块周期只有 10~20ms，线程被普通优先级调度拖一下（几 ms）就会
    在采集端丢样本、在输出端欠载——这正是「块长调小就咔哒」的主要来源。
    提到 TIME_CRITICAL 后，同样的小块长就稳得多了。
    """
    try:
        k = ctypes.windll.kernel32
        h = k.GetCurrentThread()
        k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k.SetThreadPriority(h, 15)          # THREAD_PRIORITY_TIME_CRITICAL
    except Exception:
        pass


def set_timer_resolution():
    """timeBeginPeriod(1)：让 sleep/等待的精度从 15.6ms 提到 1ms。"""
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def restore_timer_resolution():
    try:
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def open_recorder(mic, samplerate, channels, blocksize):
    """打开 soundcard 回环采集，并给采集器打两个补丁，专治「静音时白烧 CPU」。

    soundcard 的 `_record_chunk` 在设备暂时没有数据时会：
        while 可用帧数 == 0:
            deviceperiod()          # 每次都查 WASAPI（COM 调用，贵）
            sleep(最小周期/4 ≈ 2.5ms)
    静音/无播放时这段就是每 2.5ms 两次 COM 轮询，实测一个线程吃掉 11% CPU。
    补丁：
      1) 缓存 deviceperiod（省掉一半 COM 调用）；
      2) 空闲越久 sleep 间隔越大（最多 8 倍），一旦有数据立刻恢复原节奏。
    最坏情况只是「静音→有声」多等约 20ms，听不出来。
    """
    # 优先按更多声道打开（5.1/7.1 配置的 CABLE 才有意义），失败逐级降级，
    # 最后一定回落到 2 声道——立体声用户的行为与以前完全一致。
    rec = None
    last_err = None
    tried = []
    for ch in (channels, 8, 6, 2):
        if ch in tried:
            continue
        tried.append(ch)
        try:
            rec = mic.recorder(samplerate=samplerate, channels=ch,
                               blocksize=blocksize).__enter__()
            rec._actual_channels = ch
            break
        except Exception as exc:
            last_err = exc
            rec = None
    if rec is None:
        raise last_err if last_err else RuntimeError("无法打开采集设备")
    try:
        import soundcard.mediafoundation as _mf
        import numpy as _np

        class _IdleFriendlyRecorder(_mf._Recorder):
            _dp_cache = None
            _idle_spin = 0

            @property
            def deviceperiod(self):
                dp = self.__dict__.get("_dp_cache")
                if dp is None:
                    dp = _mf._Recorder.deviceperiod.fget(self)
                    self.__dict__["_dp_cache"] = dp
                return dp

            def _record_chunk(self):
                spin = 0
                while self._capture_available_frames() == 0:
                    if self._idle_start_time is None:
                        self._idle_start_time = time.perf_counter_ns()
                    dflt, minbl = self.deviceperiod
                    if spin == 0:
                        # 刚读完一块，下一包还在路上。按设备周期睡一次就够了：
                        # 原来每 2.5ms 醒一次，一块要醒 4~7 次，而每次 sleep 本身
                        # 就要 40µs 左右 CPU（线程切换），实测这一项占了空闲/播放
                        # 时 CPU 的 2~3%。这里只在「确实没数据」时才睡，睡够了再问，
                        # 有积压的包第一次轮询就直接拿到，不会累积延迟。
                        time.sleep(min(max(dflt * 0.95, 0.001), 0.05))
                    else:
                        # 还没到：短睡重试，1→8 倍退避，久等才逐步降频
                        scale = min(8, 1 + spin // 2)
                        time.sleep(min(minbl / 4.0 * scale, 0.05))
                    spin += 1
                    elapsed = time.perf_counter_ns() - self._idle_start_time
                    if elapsed / 1e9 > dflt * 4:
                        nf = int(self.samplerate * elapsed / 1e9)
                        nch = len(set(self.channelmap))
                        self._idle_start_time += elapsed
                        return _np.zeros([nf * nch], dtype="float32")
                self._idle_start_time = None
                data_ptr, nframes, flags = self._capture_buffer()
                if data_ptr != _mf._ffi.NULL:
                    chunk = _np.frombuffer(
                        _mf._ffi.buffer(data_ptr,
                                        nframes * 4 * len(set(self.channelmap))),
                        dtype="float32").copy()
                else:
                    raise RuntimeError("Could not create capture buffer")
                if flags & _mf._ole32.AUDCLNT_BUFFERFLAGS_SILENT:
                    chunk[:] = 0
                if nframes > 0:
                    self._capture_release(nframes)
                return chunk

        rec.__class__ = _IdleFriendlyRecorder
    except Exception:
        pass          # soundcard 改版也不影响功能，只是 CPU 高一点
    return rec

VIRTUAL_HINTS = ("cable", "vb-audio", "voice", "虚拟", "stereo mix")


def is_virtual(name):
    low = (name or "").lower()
    return any(k in low for k in VIRTUAL_HINTS)


def find_loopback(device):
    for m in sc.all_microphones(include_loopback=True):
        if getattr(m, "isloopback", False) and m.name == device.name:
            return m
    return None


def endpoint_has_active_session(pycaw_dev):
    """该渲染端点上有没有正在发声的音频会话。

    背景（大坑）：soundcard 的回环采集如果**在端点空闲时**打开，之后永远只
    采到静音 —— 实测：先开采集后播音=静音；播放中打开=正常；播放中打开后
    经历停顿=仍正常。所以必须等端点开始发声后再打开采集。

    检测不到会话（枚举失败 / 设备找不到）时返回 True 兑底：宁可多开，
    绝不能因为门控本身故障而不出声。
    """
    if pycaw_dev is None:
        return True
    try:
        mgr = pycaw_dev._dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
        mgr = mgr.QueryInterface(IAudioSessionManager2)
        enum = mgr.GetSessionEnumerator()
        for i in range(enum.GetCount()):
            try:
                s = AudioSession(enum.GetSession(i).QueryInterface(IAudioSessionControl2))
                if s.ProcessId and int(getattr(s.State, "value", s.State)) == 1:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return True


def soft_limit(x, ceiling_db):
    """软膝限幅：峰值接近上限时平滑压缩，避免硬削波的刺耳失真。"""
    ceiling = 10.0 ** (ceiling_db / 20.0)
    knee = ceiling * 0.8
    a = np.abs(x)
    if not np.any(a > knee):
        return x
    span = max(ceiling - knee, 1e-6)
    over = (a - knee) / span
    limited = knee + span * np.tanh(over)
    return np.where(a > knee, np.sign(x) * limited, x)


def apply_gain_ramp(x, total, state):
    """输出增益按样本线性过渡，避免「块间增益台阶」造成的拉链噪声。

    原来每个块用一个新算出的常量增益：块长 15ms 时，增益每块一跳就等于
    波形上出现一个阶跃，每秒 66 次 —— 听感是很轻但持续的「刺啦」。
    这里让增益在块内从上一块的值线性走到本块的值，块与块就接上了。
    """
    prev = state.get("g_total")
    if prev is None:
        prev = total
    state["g_total"] = total
    if abs(total - prev) <= 1e-6 or x.shape[0] < 2:
        return x * total
    ramp = np.linspace(prev, total, x.shape[0], dtype=np.float64)[:, None]
    return x * ramp


def volume_curve(v, ref=0.20, max_db=10.0):
    """Windows 音量滑块 0..1 → 输出增益。

    线性映射（直接 y *= v）在低段变化太陡、高段又到顶就没声了：
    20% 只有 0.2 倍（-14dB），几乎听不见。改用对数刻度：
        v = 0      → 0（静音）
        v = ref    → 1.0（正常音量，默认 20%）
        v = 1.0    → 10^(max_db/20)（默认 +10dB ≈ 3.16 倍）
    这样 20% 是舒服的正常音量，往上到 100% 还能一路增大。
    """
    v = float(min(max(v, 0.0), 1.0))
    if v <= 0.0:
        return 0.0
    if v >= 1.0:
        return float(10.0 ** (max_db / 20.0))
    ref = float(min(max(ref, 0.02), 0.8))
    if v <= ref:
        # 0..ref：从静音平滑爬到 1.0（平方曲线，低段更细腻）
        return (v / ref) ** 2
    # ref..1：1.0 起按 dB 线性升到 max_db
    frac = (v - ref) / (1.0 - ref)
    return float(10.0 ** (frac * max_db / 20.0))


def desired_gain(mode, target, rng, lufs, max_att, max_boost,
                 low_knee=None, knee_width=10.0):
    """输入响度 → 需要的增益（dB）。

    auto（自动电平）：目标是**区间** target ± rng。
        落在区间里 → 往区间中部拉，但只拉一半（温和不抽吸）；
        偏低 → 提到中部（target）；偏高 → 压到中部；上限 max_boost。
    limit（仅限制）：只把超过 target 的部分压下来，永不放大。

    低电平拐点（v1.8.0）：
        只要 lufs 低于 low_knee，就**不给任何提升**；从 low_knee 到
        low_knee + knee_width 线性恢复。原因：无差别放大弱信号会把底噪、
        呼吸声、环境噪声一起抬起来，听感"脏"。用拐点而不是噪声门：
        噪声门有检测延迟和开合过渡，反而更容易被听出来。
    """
    if mode == "auto":
        err = target - lufs
        if abs(err) <= rng:
            # 区间内也往中部走，但力度减半：小声片段会被托起来，又不至于一直在动
            gain = float(np.clip(err * 0.5, -max_att, max_boost))
        else:
            gain = float(np.clip(err, -max_att, max_boost))
    else:
        gain = float(np.clip(target - lufs, -max_att, 0.0))
    if gain > 0.0 and low_knee is not None:
        k = (float(lufs) - float(low_knee)) / max(float(knee_width), 1e-6)
        k = 0.0 if k <= 0.0 else (1.0 if k >= 1.0 else k)
        gain *= k
    return gain


class _Out:
    """输出设备包装：优先 RAW 模式（绕过 Nahimic/Voice Clarity 等 APO），
    不支持时回退到 soundcard 的普通共享模式。"""

    def __init__(self, out_dev, buffer_ms=60):
        self.out_dev = out_dev
        self.buffer_ms = int(buffer_ms)
        self.raw = None
        self.sc = None
        self.mode = "unknown"

    def __enter__(self):
        try:
            self.raw = RawPlayer(self.out_dev.name, SR, CHANNELS,
                                 buffer_ms=self.buffer_ms).open()
            self.mode = "raw" if self.raw.used_raw else "shared"
            return self
        except Exception:
            self.raw = None
            self.sc = self.out_dev.player(samplerate=SR, channels=CHANNELS, blocksize=BLOCK)
            self.sc.__enter__()
            self.mode = "shared-fallback"
            return self

    def play(self, data):
        if self.raw is not None:
            self.raw.play(data)
        else:
            self.sc.play(data)

    @property
    def starves(self):
        """输出缓冲被写空的累计次数（RAW 模式才有；回退模式给 0）。"""
        return self.raw.starves if self.raw is not None else 0

    @property
    def min_pad_ms(self):
        """观察到的最小剩余缓冲（ms）。贴着 0 跑就说明没有余量。"""
        return self.raw.min_pad_ms if self.raw is not None else 1e9

    def __exit__(self, *exc):
        try:
            if self.raw is not None:
                self.raw.close()
        except Exception:
            pass
        try:
            if self.sc is not None:
                self.sc.__exit__(None, None, None)
        except Exception:
            pass
        return False


class Engine:
    def __init__(self, config, on_status=None):
        self.config = config
        self.on_status = on_status
        self._run = False
        self._stop = threading.Event()
        self._thread = None
        self._q = None
        # 诊断用：设置为 callable(x, y) 可拿到「采集到的输入」与「即将送出的输出」
        self.tap = None
        self.status = {
            "running": False, "error": None, "paused": None,
            "lufs": -70.0, "gain_db": 0.0, "pre_db": 0.0,
            "input": "-", "output": "-", "out_mode": "-",
            "underrun": 0, "buffered": 0, "waiting": False, "win_vol": 1.0,
            "out_tp_db": None, "out_gr_db": 0.0,
        }

    # ---------- 设备 ----------
    def list_devices(self):
        """一次枚举同时得到「监听输入（虚拟声卡）」与「真实输出」。

        原来 list_inputs()/list_outputs() 各自调一次 sc.all_speakers()，
        等于把同一份 WASAPI 端点枚举跑两遍；这里合并成一次。
        """
        ins, outs = [], []
        try:
            for s in sc.all_speakers():
                (ins if is_virtual(s.name) else outs).append(s.name)
        except Exception:
            pass
        return ins, outs

    def list_outputs(self):
        return self.list_devices()[1]

    def list_inputs(self):
        return self.list_devices()[0]

    def _pick_input(self, cfg):
        want = (cfg.get("input_device") or "").strip().lower()
        speakers = sc.all_speakers()
        if want:
            for s in speakers:
                if s.name.lower() == want:
                    return s
            for s in speakers:
                if want in s.name.lower():
                    return s
        for pref in ("cable input", "cable in"):
            for s in speakers:
                if pref in s.name.lower():
                    return s
        for s in speakers:
            if is_virtual(s.name):
                return s
        raise RuntimeError("没有找到 CABLE 虚拟声卡。请确认 VB-CABLE 已安装，"
                           "或到「设备」页手动选择监听输入。")

    def _healthy_names(self):
        """当前可用（未静音、音量不为 0）的端点名，用于自动挑输出设备。"""
        names = set()
        try:
            from pycaw.pycaw import AudioUtilities
            for d in AudioUtilities.GetAllDevices():
                try:
                    if int(getattr(d.state, "value", d.state)) != 1:
                        continue
                    ev = d.EndpointVolume
                    if not ev.GetMute() and ev.GetMasterVolumeLevelScalar() > 0.01:
                        names.add(d.FriendlyName)
                except Exception:
                    continue
        except Exception:
            pass
        return names

    def _pick_output(self, cfg):
        want = (cfg.get("output_device") or "").strip().lower()
        speakers = sc.all_speakers()
        if want:
            for s in speakers:
                if s.name.lower() == want:
                    return s
            for s in speakers:
                if want in s.name.lower():
                    return s
        healthy = self._healthy_names()
        real = [s for s in speakers if not is_virtual(s.name)]
        for s in real:
            if s.name in healthy:
                return s
        # 兜底：优先耳机。未接线的后置「扬声器」常被静音/音量 0，
        # 选到它用户就完全听不到声音。
        prefer = ("耳机", "headphone", "headset", "earphone")
        real.sort(key=lambda s: (0 if any(k in s.name.lower() for k in prefer) else 1))
        if real:
            return real[0]
        raise RuntimeError("没有找到可用的耳机/扬声器。")

    # ---------- 生命周期 ----------
    def start(self):
        if self.is_running():
            return
        self._stop.clear()
        self._run = True
        self._thread = threading.Thread(target=self._supervise, name="Engine", daemon=True)
        self._thread.start()

    def stop(self):
        self._run = False
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        self._publish(running=False)

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def request_reload(self):
        self._stop.set()

    def _publish(self, **kw):
        self.status.update(kw)
        if self.on_status:
            try:
                self.on_status(self.status.copy())
            except Exception:
                pass

    def _rdp_blocked(self, cfg=None):
        """远程桌面场景 + 用户没开「在远程桌面中接管」→ 不碰声音。

        「不碰」是字面意思：不打开采集/播放设备、不处理、也不改系统默认输出。
        这样 RDP 里的声音会原样走对面设备；用户把开关打开后才像本机一样接管。
        cfg 可以传配置快照，也可以不传（直接读当前配置，省一次拷贝）。
        """
        cfg = cfg if cfg is not None else self.config
        return session.blocked(bool(cfg.get("rdp_takeover", False)))

    def _set_pause(self, reason):
        """暂停状态只发布一次，避免每秒刷一遍状态文件。"""
        if reason:
            if self.status.get("paused") == reason and not self.status.get("running"):
                return
            self._publish(running=False, error=None, paused=reason, waiting=False)
        elif self.status.get("paused"):
            self._publish(paused=None)

    def _supervise(self):
        ensure_com()
        backoff = 1.0
        while self._run:
            if self._rdp_blocked():
                self._set_pause("rdp")
                # 0.5 秒看一次配置/会话状态：用户在界面上打开接管就立刻恢复
                if self._stop.wait(0.5):
                    self._stop.clear()
                continue
            self._set_pause(None)
            started = time.time()
            try:
                self._session()
                if not self._run:
                    break
                if time.time() - started > 30:
                    backoff = 1.0
                # 正常结束（reload）直接重连；如果刚连上就断，稍等一下再试，
                # 避免设备插拔时打成紧密重连循环
                if time.time() - started < 1.0:
                    self._stop.wait(0.5)
                continue
            except Exception as exc:
                if not self._run:
                    break
                self._publish(running=False,
                              error="音频链路中断：%s（%.0f 秒后重连）"
                              % (str(exc).splitlines()[0][:70], backoff))
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)

    # ---------- 一次会话 ----------
    def _num(self, cfg, key, default, lo, hi):
        try:
            v = float(cfg.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        if not np.isfinite(v):
            return float(default)
        return float(min(max(v, lo), hi))

    def _session(self):
        # 关键：上一次 request_reload() 会把 _stop 置位，新会话必须清掉它，
        # 否则新起的采集/播放线程一进循环就退出 —— 表现就是「切换设备后没声音」。
        self._stop.clear()
        cfg = self.config.snapshot()
        in_dev = self._pick_input(cfg)
        out_dev = self._pick_output(cfg)
        mic = find_loopback(in_dev)
        if mic is None:
            raise RuntimeError("监听输入「%s」不支持回环采集。" % in_dev.name)
        if out_dev.name == in_dev.name:
            raise RuntimeError("输入和输出不能是同一个设备，否则会自激。")

        kw = KWeighting(SR, CHANNELS)
        meter = SlidingLUFS(SR, window_ms=self._num(cfg, "lufs_window_ms", 1000, 200, 5000))
        dc = DcBlocker(SR, fc=self._num(cfg, "dc_blocker_hz", 10, 1, 120), channels=CHANNELS)

        state = {"gain": 0.0, "lufs": -70.0, "underrun": 0, "warm": 0.0,
                 "win_vol": 1.0,
                 # Windows 音量：每块都去读端点音量是 200 次/秒 COM 调用，
                 # 改成每 100ms 读一次，人耳完全感觉不到延迟，CPU 省一大截
                 "vol_at": 0.0, "vol_scalar": 1.0, "vol_gain": 1.0}
        self._thread_error = None      # 工作线程的现场：不许再悄悄吞掉
        # 门控要按「监听输入这个端点」查会话；找不到就 None（函数里兑底为 True）
        try:
            sess_dev = [d for d in AudioUtilities.GetAllDevices()
                        if (d.FriendlyName or "") == in_dev.name][0]
        except Exception:
            sess_dev = None
        block_ms = self._num(cfg, "block_ms", 20, 10, 100)
        blk = int(round(SR * block_ms / 1000.0))
        # WASAPI 输出缓冲 = 两块 + 6ms 抖动余量。为什么不是「一块 + 8ms」：
        # 采集端按设备周期成块交付，块间会有几毫秒到十几毫秒的抖动；余量只有
        # 8~9ms 时缓冲会被写空，WASAPI 补静音 —— 听感就是持续的轻微「刺啦」。
        # 实测 11ms 块 + 20ms 缓冲：播放时每秒约 9 次写空；改成两块+6ms 后归零。
        # 想更跟手可以在高级设置里用 out_buffer_ms 手动压回去。
        auto_buf = max(24, int(block_ms) * 2 + 6)
        user_buf = int(self._num(cfg, "out_buffer_ms", 0, 0, 400))
        # 用户显式指定时才允许压小（低于两块就容易刺啦，这里仍留一点底线）
        deadband_ms = max(int(block_ms) + 4, user_buf) if user_buf > 0 else auto_buf
        self.status["block_ms"] = int(block_ms)
        self.status["buffer_ms"] = int(deadband_ms)
        # 队列只留一块：抖动的吸收交给设备缓冲（那里已经是两块），
        # 队列再堆两块等于白加 2×块长 的延迟。
        q = queue.Queue(maxsize=1)

        # Windows 音量滑块（CABLE Input 的端点音量）对回环采集不起作用，
        # 必须在引擎里主动读出来当输出增益用，用户才能用系统音量控大小。
        win_vol = None
        if sess_dev is not None and bool(cfg.get("use_windows_volume", True)):
            try:
                win_vol = sess_dev.EndpointVolume
                # 绝不修改用户的 Windows 音量滑块——只读它当输出增益。
                # 曲线用「滑块值 / 正常位置」计算，用户把滑块停在哪都行。
                if getattr(self, "_win_vol_init", False) is False:
                    self._win_vol_init = True
            except Exception:
                win_vol = None
        self._q = q
        self._publish(running=True, error=None, paused=None,
                      input=in_dev.name, output=out_dev.name)

        def capture():
            ensure_com()
            boost_current_thread()
            rec = None
            in_ch = CHANNELS
            silent_for = 0.0
            last_sess_check = 0.0
            sess_active = True
            try:
                while self._run and not self._stop.is_set():
                    if rec is None:
                        # 端点空闲时打开回环会永远采到静音（soundcard 的坑），
                        # 等有活动会话再开；每 0.5 秒查一次会话状态
                        now = time.time()
                        if now - last_sess_check > 0.5:
                            last_sess_check = now
                            sess_active = endpoint_has_active_session(sess_dev)
                        if not sess_active:
                            self._publish(waiting=True)
                            self._stop.wait(0.15)
                            continue
                        want_ch = 8 if cfg.get("downmix", True) else CHANNELS
                        rec = open_recorder(mic, SR, want_ch, blk)
                        in_ch = int(getattr(rec, "_actual_channels", CHANNELS))
                        self._publish(in_channels=in_ch)
                        silent_for = 0.0
                        self._publish(waiting=False)
                    d = rec.record(numframes=blk)
                    if d is None or d.size == 0:
                        continue
                    try:
                        peak = float(np.abs(d).max())     # 用原始声道算峰值更准
                    except Exception:
                        peak = 1.0
                    if in_ch > 2:
                        # 5.1/7.1 内容折成左右：中置（人声）和环绕不再丢失
                        d = downmix_to_stereo(d)
                    if peak < 1e-6:
                        # 纯静音：可能只是真的没声音；但如果端点上明明有会话，
                        # 说明采集已经断链（老会话里开出来的采集）——重开一次
                        silent_for += blk / float(SR)
                        if silent_for > 3.0:
                            now = time.time()
                            if now - last_sess_check > 0.5:
                                last_sess_check = now
                                sess_active = endpoint_has_active_session(sess_dev)
                            if sess_active:
                                try:
                                    rec.__exit__(None, None, None)
                                except Exception:
                                    pass
                                rec = None
                                silent_for = 0.0
                                continue
                    else:
                        silent_for = 0.0
                    # 长时间静音：把采集节流到 ~20 块/秒。soundcard 每次 record 都要
                    # 轮询 WASAPI，满速跑等于白烧一个核；有声音时 silent_for 立刻归零，
                    # 下一块就回到正常节奏（最多多等一次 20ms，听不出）。
                    # 阈值 2 秒：正常音乐/视频里的停顿不会误触发。
                    if silent_for > 2.0:
                        time.sleep(0.02)
                    # 队列只有一格：满了就等（正常节流），真的等超时才丢旧的换新的，
                    # 并给下一块打上 gap 标记 —— 播放端据此做 2ms 淡化，不会咔哒。
                    try:
                        q.put((d, False), timeout=0.2)
                    except queue.Full:
                        try:
                            q.get_nowait()
                            state["underrun"] += 1
                        except queue.Empty:
                            pass
                        try:
                            q.put((d, True), timeout=0.05)
                        except queue.Full:
                            pass
            except Exception:
                self._thread_error = traceback.format_exc()
                self._last_trace = self._thread_error
                self._stop.set()
            finally:
                if rec is not None:
                    try:
                        rec.__exit__(None, None, None)
                    except Exception:
                        pass

        # 块边界平滑：欠载/丢块/增益突变都不再产生阶跃咔哒，代价只是每块开头 2ms 交叉
        guard = ClickGuard(SR, fade_ms=2.0, channels=CHANNELS)
        # 前瞻式真峰值限幅：按"采样间峰值"限制，而不是对波形做软膝整形。
        # 前者只降增益、波形形状不变；后者会把峰顶压扁，长期工作时听感发毛。
        # 代价是 lookahead_ms（2ms）的额外延迟。
        limiter = TruePeakLimiter(
            SR, CHANNELS,
            ceiling_db=self._num(cfg, "limiter_ceiling_db", -1.0, -12, 0),
            lookahead_ms=2.0, release_ms=120.0)

        def playback():
            ensure_com()
            boost_current_thread()
            last = time.time()
            # ---- 静音快路径 ----
            # 连续静音超过 ~0.3 秒后，整条链路对零输入毫无意义（下混、去直流、
            # K 加权、测响度、限幅全是白算），直接把预置零块送给声卡。
            # 进来之前已经连续送了 0.3 秒的真零块，滤波器状态基本归零，
            # 所以离开时要 reset 一次，绝不能把静音期的陈旧状态带进下一段音乐。
            zeros = np.zeros((blk, CHANNELS), dtype=np.float32)
            fast = False
            quiet = 0
            quiet_need = max(8, int(300.0 / max(float(block_ms), 1.0)))
            # 配置快照按版本号缓存：每块 deepcopy 一份配置在 100 块/秒时纯属浪费
            # （第一次循环发现版本号变了就会装进来，所以这里先放空）
            hot = {"cfg": None, "ver": -1}
            try:
                with _Out(out_dev, buffer_ms=deadband_ms) as pl:
                    self._publish(out_mode=pl.mode)
                    while self._run and not self._stop.is_set():
                        # 配置变了（改「启用/直通/目标响度」等）立刻跟着变，
                        # 没变就复用上一份，避免每块 deepcopy
                        ver = self.config.version()
                        if ver != hot["ver"]:
                            hot["ver"] = ver
                            hot["cfg"] = self.config.view()
                        cfg = hot["cfg"]
                        try:
                            item = q.get(timeout=0.05)
                        except queue.Empty:
                            state["underrun"] += 1
                            # 不能硬切静音：从当前电平淡出，否则就是一声咔哒
                            pl.play(guard.silence(blk).astype(np.float32))
                            continue
                        # 采集端会显式标出「这块之前丢过块」→ 接缝必然不连续
                        if isinstance(item, tuple):
                            block, gap = item
                        else:
                            block, gap = item, False

                        x = np.asarray(block, dtype=np.float64)
                        if x.ndim == 1:
                            x = x[:, None]
                        if x.shape[1] != CHANNELS:
                            x = x[:, :CHANNELS] if x.shape[1] > CHANNELS else np.pad(
                                x, ((0, 0), (0, CHANNELS - x.shape[1])))
                        if x.shape[0] != blk:
                            pad = blk - x.shape[0]
                            x = np.pad(x, ((0, max(pad, 0)), (0, 0)))[:blk]
                        if not np.all(np.isfinite(x)):
                            x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)

                        # 数字静音（一块全是 0）才是快路径的目标；-0.000001 这种
                        # 底噪块不算，老老实实走完整链路，绝不会漏掉真实声音
                        silent_block = not x.any()
                        quiet = quiet + 1 if silent_block else 0
                        if fast and not silent_block:
                            # 有声音了：清掉静音期留下的滤波器状态与增益，
                            # 否则第一个块会带着陈旧状态出现台阶（咔哒/泵一下）
                            fast = False
                            dc.reset()
                            kw.reset()
                            meter.reset()
                            limiter.reset()
                            state["warm"] = 0.0
                            state["gain"] = 0.0
                        if silent_block and (fast or quiet >= quiet_need):
                            if not fast:
                                fast = True
                                dc.reset()
                                kw.reset()
                                meter.reset()
                                # 清掉前瞻缓冲里的旧音频，别在恢复时吐出一段 2ms 的残响
                                limiter.reset()
                                state["warm"] = 0.0
                                state["gain"] = 0.0
                                state["lufs"] = -70.0
                            pl.play(zeros)
                            now = time.time()
                            if now - last > 0.25:
                                last = now
                                self._publish(lufs=-70.0, gain_db=0.0,
                                              pre_db=state.get("pre_db", 0.0),
                                              win_vol=state.get("win_vol", 1.0),
                                              underrun=state["underrun"],
                                              buffered=q.qsize(),
                                              out_tp_db=None, out_gr_db=0.0,
                                              out_starves=pl.starves,
                                              out_pad_ms=round(pl.min_pad_ms, 1))
                            continue

                        x = np.clip(x, -4.0, 4.0)
                        x = dc.process(x)

                        # 测量在增益之前：闭环天然稳定
                        state["lufs"] = float(meter.update(kw.process(x)))
                        state["warm"] += blk / float(SR)

                        # 处理前衰减：测量之后、增益/限幅之前把信号降下来。
                        # 效果 = 整体音量下降这么多，同时给限幅器留余量。
                        # 测量不看它，所以「目标响度」的含义不会被它带偏。
                        pre_db = self._num(cfg, "pre_gain_db", 0.0, -40.0, 0.0)
                        pre = float(10.0 ** (pre_db / 20.0)) if pre_db else 1.0
                        state["pre_db"] = pre_db

                        # Windows 音量滑块：先读出增益倍数，放进限幅之前一起处理，
                        # 这样 100%（+10dB）时被限幅器兜住，不会硬削波。
                        # 测量仍是增益前的 x，所以调音量不影响响度判定。
                        now = time.time()
                        if win_vol is not None and (now - state["vol_at"]) >= 0.1:
                            state["vol_at"] = now
                            try:
                                if win_vol.GetMute():
                                    state["vol_scalar"] = 0.0
                                    state["vol_gain"] = 0.0
                                else:
                                    scalar = float(win_vol.GetMasterVolumeLevelScalar())
                                    state["vol_scalar"] = scalar
                                    state["vol_gain"] = volume_curve(
                                        scalar,
                                        self._num(cfg, "winvol_ref", 0.20, 0.02, 0.8),
                                        self._num(cfg, "winvol_max_db", 10.0, 0, 20))
                            except Exception:
                                state["vol_scalar"], state["vol_gain"] = 1.0, 1.0
                        vol_scalar = state["vol_scalar"]
                        vol_gain = state["vol_gain"]
                        state["win_vol"] = vol_scalar

                        if bool(cfg.get("passthrough", False)):
                            # 直通：不做电平调整，原样搬运（排查 / 只要自动静音时用）。
                            # 但音量与前级仍可能把信号推过满刻度，所以仍过一遍 0 dBFS
                            # 软膝：低于 0.8 FS 一个采样都不动，超过才平滑压住，
                            # 比让驱动硬削波干净得多。
                            state["gain"] = 0.0
                            y = apply_gain_ramp(x, pre * vol_gain, state)
                            y = soft_limit(y, 0.0)
                            # 直通不过限幅器，峰值读数置空（别把上一段的旧值留着）
                            limiter.last_tp = 0.0
                            limiter.last_gr_db = 0.0
                        else:
                            gate = self._num(cfg, "silence_gate_lufs", -60, -90, -20)
                            enabled = bool(cfg.get("enabled", True))
                            auto = str(cfg.get("level_mode", "auto")).lower() != "limit"
                            target = self._num(cfg, "target_lufs", -18, -40, -8)
                            max_att = self._num(cfg, "max_attenuation_db", 20, 0, 40)
                            if not enabled or state["lufs"] < gate or state["warm"] < 0.4:
                                desired = 0.0
                            else:
                                # 低电平拐点：比目标低 34dB 以上（默认）完全不提升，
                                # 往上 10dB 内线性恢复——护住底噪，又不至于切掉弱对白
                                knee_db = self._num(cfg, "low_knee_db", 34.0, 0.0, 60.0)
                                knee = (target - knee_db) if knee_db > 0.0 else None
                                desired = desired_gain(
                                    "auto" if auto else "limit", target,
                                    self._num(cfg, "target_range_db", 3, 0, 10),
                                    state["lufs"], max_att,
                                    self._num(cfg, "max_boost_db", 12, 0, 30),
                                    low_knee=knee,
                                    knee_width=self._num(cfg, "low_knee_width_db",
                                                         10.0, 1.0, 40.0))

                            attack = self._num(cfg, "gain_attack_ms", 120, 5, 3000) / 1000.0
                            release = self._num(cfg, "gain_release_ms", 900, 50, 8000) / 1000.0
                            dt = blk / float(SR)
                            if desired < state["gain"]:
                                tau = attack                      # 压下去要快
                            else:
                                tau = release
                                if auto:
                                    tau = max(tau, 1.2)           # 提上来要慢
                            coeff = 1.0 - float(np.exp(-dt / max(tau, 1e-3)))
                            state["gain"] += (desired - state["gain"]) * coeff

                            # 增益按样本线性过渡：块间没有台阶，就不会有拉链噪声
                            y = apply_gain_ramp(
                                x, pre * float(10.0 ** (state["gain"] / 20.0)) * vol_gain,
                                state)
                            # 真峰值限幅（只降增益，不改波形）：天花板按界面上设的
                            # dBTP 走，改了配置立刻生效，不需要重连
                            ceil_db = self._num(cfg, "limiter_ceiling_db", -1.0, -12, 0)
                            if abs(ceil_db - limiter.ceiling_db) > 1e-9:
                                limiter.set_ceiling(ceil_db)
                            y = limiter.process(y)

                        # 只在「真的不连续」时才在块首淡化；连续音频原样通过
                        y = guard.smooth(y, discontinuity=gap)
                        if self.tap is not None:
                            try:
                                self.tap(x, y)   # 抓的是真正送出的（已平滑）
                            except Exception:
                                pass
                        pl.play(np.ascontiguousarray(y, dtype=np.float32))

                        now = time.time()
                        if now - last > 0.25:
                            last = now
                            tp = limiter.last_tp
                            self._publish(lufs=state["lufs"], gain_db=state["gain"],
                                          pre_db=state.get("pre_db", 0.0),
                                          win_vol=state.get("win_vol", 1.0),
                                          out_tp_db=(round(20.0 * np.log10(tp), 1)
                                                     if tp > 1e-6 else None),
                                          out_gr_db=round(limiter.last_gr_db, 1),
                                          underrun=state["underrun"], buffered=q.qsize(),
                                          out_starves=pl.starves,
                                          out_pad_ms=round(pl.min_pad_ms, 1))
            except Exception:
                self._thread_error = traceback.format_exc()
                self._last_trace = self._thread_error
                self._stop.set()

        set_timer_resolution()     # 1ms 定时精度：小块长才稳
        t1 = threading.Thread(target=capture, name="Cap", daemon=True)
        t2 = threading.Thread(target=playback, name="Play", daemon=True)
        t1.start()
        t2.start()
        while self._run and not self._stop.is_set():
            if not (t1.is_alive() and t2.is_alive()):
                break
            # 运行途中被远程桌面挤掉（或用户打开/关闭「接管远程桌面」）：
            # 立刻收工回到暂停态，别继续占着 CABLE 和默认输出
            if self._rdp_blocked():
                self._stop.set()
                break
            time.sleep(0.25)
        self._stop.set()
        t1.join(timeout=1.5)
        t2.join(timeout=1.5)
        restore_timer_resolution()
        self._q = None
        # 工作线程挂了必须把现场抛出去：否则界面永远显示 running=True、
        # 响度永远是初值，而日志里什么都没有（这次排查就是被这个坑的）
        if self._thread_error:
            err = self._thread_error.splitlines()[-1][:160]
            self._publish(running=False, error="音频链路中断：%s" % err)
            raise RuntimeError(err)

    def diagnostics(self):
        return dict(self.status)
