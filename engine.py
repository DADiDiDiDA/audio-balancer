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

from dsp import DcBlocker
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


def desired_gain(mode, target, rng, lufs, max_att, max_boost):
    """输入响度 → 需要的增益（dB）。

    auto（自动电平）：目标是**区间** target ± rng。
        落在区间里 → 0（完全不动）；
        偏低 → 提上来，但只提到区间下沿（不会一路追到目标值），上限 max_boost；
        偏高 → 压下去，同样只压到区间上沿。
    limit（仅限制）：只把超过 target 的部分压下来，永不放大。
    """
    if mode == "auto":
        err = target - lufs
        if abs(err) <= rng:
            return 0.0
        return float(np.clip(err - float(np.sign(err)) * rng, -max_att, max_boost))
    return float(np.clip(target - lufs, -max_att, 0.0))


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
            "running": False, "error": None,
            "lufs": -70.0, "gain_db": 0.0, "pre_db": 0.0,
            "input": "-", "output": "-", "out_mode": "-",
            "underrun": 0, "buffered": 0, "waiting": False,
        }

    # ---------- 设备 ----------
    def list_outputs(self):
        out = []
        try:
            for s in sc.all_speakers():
                if not is_virtual(s.name):
                    out.append(s.name)
        except Exception:
            pass
        return out

    def list_inputs(self):
        out = []
        try:
            for s in sc.all_speakers():
                if is_virtual(s.name):
                    out.append(s.name)
        except Exception:
            pass
        return out

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

    def _supervise(self):
        ensure_com()
        backoff = 1.0
        while self._run:
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

        state = {"gain": 0.0, "lufs": -70.0, "underrun": 0, "warm": 0.0}
        self._thread_error = None      # 工作线程的现场：不许再悄悄吞掉
        # 门控要按「监听输入这个端点」查会话；找不到就 None（函数里兑底为 True）
        try:
            sess_dev = [d for d in AudioUtilities.GetAllDevices()
                        if (d.FriendlyName or "") == in_dev.name][0]
        except Exception:
            sess_dev = None
        block_ms = self._num(cfg, "block_ms", 33, 20, 120)
        blk = int(round(SR * block_ms / 1000.0))
        deadband_ms = max(30, int(block_ms) + 18)      # WASAPI 缓冲 ≈ 一块 + 抖动余量
        self.status["block_ms"] = int(block_ms)
        q = queue.Queue(maxsize=4)
        self._q = q
        self._publish(running=True, error=None,
                      input=in_dev.name, output=out_dev.name)

        def capture():
            ensure_com()
            rec = None
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
                        rec = mic.recorder(samplerate=SR, channels=CHANNELS,
                                           blocksize=blk).__enter__()
                        silent_for = 0.0
                        self._publish(waiting=False)
                    d = rec.record(numframes=blk)
                    if d is None or d.size == 0:
                        continue
                    try:
                        peak = float(np.abs(d).max())
                    except Exception:
                        peak = 1.0
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
                    while q.full():
                        try:
                            q.get_nowait()
                            state["underrun"] += 1
                        except queue.Empty:
                            break
                    try:
                        q.put(d, timeout=0.2)
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

        silence = np.zeros((blk, CHANNELS), dtype=np.float32)

        def playback():
            ensure_com()
            last = time.time()
            try:
                with _Out(out_dev, buffer_ms=deadband_ms) as pl:
                    self._publish(out_mode=pl.mode)
                    while self._run and not self._stop.is_set():
                        # 每块重读配置：否则界面上改「启用/直通/目标响度」都要重启才生效
                        # （原来只在会话开始时读一次，这就是"勾了直通却没变化"的原因）
                        cfg = self.config.snapshot()
                        try:
                            block = q.get(timeout=0.05)
                        except queue.Empty:
                            state["underrun"] += 1
                            pl.play(silence)
                            continue

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

                        if bool(cfg.get("passthrough", False)):
                            # 直通：完全不处理，原样搬运（排查 / 只要自动静音时用）
                            state["gain"] = 0.0
                            y = x * pre
                        else:
                            gate = self._num(cfg, "silence_gate_lufs", -60, -90, -20)
                            enabled = bool(cfg.get("enabled", True))
                            auto = str(cfg.get("level_mode", "auto")).lower() != "limit"
                            target = self._num(cfg, "target_lufs", -18, -40, -8)
                            max_att = self._num(cfg, "max_attenuation_db", 20, 0, 40)
                            if not enabled or state["lufs"] < gate or state["warm"] < 0.4:
                                desired = 0.0
                            else:
                                desired = desired_gain(
                                    "auto" if auto else "limit", target,
                                    self._num(cfg, "target_range_db", 3, 0, 10),
                                    state["lufs"], max_att,
                                    self._num(cfg, "max_boost_db", 12, 0, 30))

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

                            y = x * pre * float(10.0 ** (state["gain"] / 20.0))
                            y = soft_limit(y, self._num(cfg, "limiter_ceiling_db", -1.0, -6, 0))
                        if self.tap is not None:
                            try:
                                self.tap(x, y)
                            except Exception:
                                pass
                        pl.play(np.ascontiguousarray(y, dtype=np.float32))

                        now = time.time()
                        if now - last > 0.25:
                            last = now
                            self._publish(lufs=state["lufs"], gain_db=state["gain"],
                                          pre_db=state.get("pre_db", 0.0),
                                          underrun=state["underrun"], buffered=q.qsize())
            except Exception:
                self._thread_error = traceback.format_exc()
                self._last_trace = self._thread_error
                self._stop.set()

        t1 = threading.Thread(target=capture, name="Cap", daemon=True)
        t2 = threading.Thread(target=playback, name="Play", daemon=True)
        t1.start()
        t2.start()
        while self._run and not self._stop.is_set():
            if not (t1.is_alive() and t2.is_alive()):
                break
            time.sleep(0.25)
        self._stop.set()
        t1.join(timeout=1.5)
        t2.join(timeout=1.5)
        self._q = None
        # 工作线程挂了必须把现场抛出去：否则界面永远显示 running=True、
        # 响度永远是初值，而日志里什么都没有（这次排查就是被这个坑的）
        if self._thread_error:
            err = self._thread_error.splitlines()[-1][:160]
            self._publish(running=False, error="音频链路中断：%s" % err)
            raise RuntimeError(err)

    def diagnostics(self):
        return dict(self.status)
