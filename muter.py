"""自动静音：按"列表里的程序"决定谁的会话该压低。

两条规则（各自可在界面上开关）：
  A. 挂机静音（默认开）：列表里的程序**不在前台**时，把**它自己**压到「压到」
     —— 游戏最小化/挂机时不再出声。
  B. 游戏时压制其它程序（默认关）：列表里的程序在前台时，把**其它程序**压到「压到」
     —— 打游戏时音乐自动让路。

只改会话音量（ISimpleAudioVolume），绝不碰：
  * 系统提示音（pid 0）
  * 本程序与 audiodg
  * 用户配置的忽略列表
退出或关闭开关时恢复成用户自己原来的音量。
"""
import ctypes
import ctypes.wintypes  # noqa: F401  （显式导入，别依赖其它库顺手加载）
import math
import os

import psutil
from comtypes import CLSCTX_ALL
from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
from pycaw.pycaw import AudioSession, AudioUtilities

from device_guard import default_render

# 必须忽略的会话：系统提示音、音频引擎、本程序自己
# 注意：本程序 exe 名是「音频均衡器.exe」，光靠名字容易漏，
# 所以额外用 pid 判断（见 _is_self）。
ALWAYS_IGNORE = {"audiobalancer.exe", "音频均衡器.exe", "audiodg.exe"}
STATE_EXPIRED = 2


def ensure_com():
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
    return (hr + 2 ** 32) not in (0, 0x80010106)


def _num(cfg, key, default, lo, hi):
    """取数值配置：非数字/NaN/越界都退回默认值。

    以前 tick 里直接 float(cfg.get(...))，配置里一个 null 就抛异常，
    整个自动静音停摆，而且每 0.3 秒往日志里写一遍完整 traceback。
    """
    try:
        v = float(cfg.get(key, default))
    except (TypeError, ValueError):
        v = float(default)
    if not math.isfinite(v):
        v = float(default)
    return max(float(lo), min(float(hi), v))


def foreground_info():
    """返回 (前台进程名小写, 前台 PID)。"""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", 0
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return "", 0
    try:
        return (psutil.Process(pid.value).name() or "").lower(), int(pid.value)
    except Exception:
        return "", int(pid.value)


def foreground_process_name():
    return foreground_info()[0]


class Muter:
    def __init__(self, config):
        self.config = config
        self._vol = {}
        self._orig = {}          # key -> 接管前用户自己的音量
        self._sessions = []
        self._names = {}
        self._fg_pid = 0
        self._stopping = False
        self._last_enum = 0.0
        self._self_pid = os.getpid()
        self.foreground = ""
        self.in_game = False
        self.controlled = 0
        self.muted = []          # 正在被压低的程序名（界面显示用）
        self.error = None

    def _enum(self):
        out = []
        try:
            # 直接在默认端点上 Activate 会话管理器：pycaw 的
            # GetAudioSessionManager() 走的是 GetSpeakers()→CreateDevice，
            # 每次都要把端点属性表读一遍（~10ms），3 秒一轮纯属浪费。
            try:
                ptr = default_render()
                mgr = ptr.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
                mgr = mgr.QueryInterface(IAudioSessionManager2)
            except Exception:
                mgr = AudioUtilities.GetAudioSessionManager()   # 兜底
            if mgr is None:
                return out
            enum = mgr.GetSessionEnumerator()
            for i in range(enum.GetCount()):
                ctl = enum.GetSession(i)
                if ctl is None:
                    continue
                c2 = ctl.QueryInterface(IAudioSessionControl2)
                if c2 is None:
                    continue
                s = AudioSession(c2)
                if s.SimpleAudioVolume is None or not s.ProcessId:
                    continue
                if getattr(s, "State", 1) == STATE_EXPIRED:
                    continue
                out.append(s)
        except Exception as exc:
            self.error = str(exc)
            return []
        return out

    @staticmethod
    def _key(s):
        try:
            v = s.InstanceIdentifier or ""
        except Exception:
            v = ""
        return v or (s.ProcessId, s.DisplayName or "")

    def _pname(self, s):
        pid = s.ProcessId
        if pid in self._names:
            return self._names[pid]
        try:
            name = (s.Process.name() or "").lower()
        except Exception:
            name = ""
        self._names[pid] = name
        return name

    def tick(self, now):
        cfg = self.config.snapshot()
        if not cfg.get("mute_enabled", True):
            if self._vol:
                self.restore()
            self.in_game = False
            self.controlled = 0
            self.muted = []
            return

        if now - self._last_enum > 3.0:
            self._last_enum = now
            self._sessions = self._enum()
            # PID 会被系统复用：进程名缓存不能永久保留，每次重枚举就清一次
            self._names.clear()

        games = {str(g).lower() for g in (cfg.get("mute_games") or [])}
        ignore = {str(g).lower() for g in (cfg.get("mute_ignore") or [])}
        self.foreground, self._fg_pid = foreground_info()
        self.in_game = bool(games) and self.foreground in games
        mute_bg_listed = bool(cfg.get("mute_listed_in_background", True))
        # 与 config.DEFAULTS 保持一致（这里以前写 True，缺键时行为和界面对不上）
        mute_others = bool(cfg.get("mute_others_in_game", False))

        fg_vol = _num(cfg, "mute_fg_volume", 1.0, 0.0, 1.0)
        bg_vol = _num(cfg, "mute_bg_volume", 0.0, 0.0, 1.0)
        alpha = min(1.0, 200.0 / max(100.0, _num(cfg, "mute_easing_ms", 500, 50, 10000)))
        # UWP / 商店游戏的前台窗口属于 ApplicationFrameHost，进程名对不上游戏本体。
        # 这种情况下别把列表里的程序当"后台"静音——那会让正在玩的游戏直接没声音。
        fg_uncertain = self.foreground in ("applicationframehost.exe", "shellhost.exe")

        controlled = 0
        seen = set()
        muted = []
        for s in self._sessions:
            # 绝不碰自己：否则会把自己的播放音量压掉，判定抖动时还会来回抖
            if s.ProcessId == self._self_pid:
                continue
            name = self._pname(s)
            if name in ALWAYS_IGNORE or name in ignore:
                continue
            key = self._key(s)
            seen.add(key)
            try:
                raw = float(s.SimpleAudioVolume.GetMasterVolume())
            except Exception:
                continue

            listed = name in games
            # 进程名比对外加 PID 比对：同名程序、以及靠启动器拉起的游戏都能认准
            is_foreground = bool(name) and (name == self.foreground
                                            or s.ProcessId == self._fg_pid)
            if listed and is_foreground:
                target = fg_vol          # 在前台：保持正常音量
            elif listed and mute_bg_listed and not fg_uncertain:
                target = bg_vol          # 在后台（最小化/挂机）：静音它自己
            elif self.in_game and mute_others:
                target = bg_vol          # 游戏在前台：其它程序让路
            else:
                # 不在处理范围：如果我们之前动过，还原一次，然后完全放手。
                # 以前会把 target 设成"记录的原始音量"，于是用户手动把音量调小
                # 之后，音量条会被一点点拉回去（看起来像自己在跳）。
                if key in self._orig:
                    orig = self._orig.pop(key)
                    self._vol.pop(key, None)
                    try:
                        if abs(raw - orig) > 0.005:
                            s.SimpleAudioVolume.SetMasterVolume(orig, None)
                    except Exception:
                        pass
                continue

            last = self._vol.get(key)
            if key not in self._orig:
                # 只有真的要改它时才记原值，避免把没动过的程序也记下来
                self._orig[key] = raw
            elif last is not None and abs(raw - last) > 0.05:
                # 用户自己拖了音量条：以他的新值为基准，别再拉回旧值
                self._orig[key] = raw

            cur = last if last is not None else raw
            new = cur + (target - cur) * alpha
            if abs(new - target) < 0.01:
                new = target
            self._vol[key] = new
            try:
                if abs(raw - new) > 0.005:
                    s.SimpleAudioVolume.SetMasterVolume(new, None)
                if new < 0.99 and new < self._orig[key] - 0.05:
                    controlled += 1
                    muted.append(name or ("pid %s" % s.ProcessId))
            except Exception:
                continue
        for k in [k for k in self._vol if k not in seen]:
            self._vol.pop(k, None)
        if len(self._orig) > 200:
            self._orig = {k: v for k, v in self._orig.items() if k in seen}
        self.controlled = controlled
        self.muted = list(dict.fromkeys(muted))[:5]

    def restore(self):
        for s in self._sessions or self._enum():
            try:
                if not (s.SimpleAudioVolume and s.ProcessId):
                    continue
                if s.ProcessId == self._self_pid:
                    continue
                key = self._key(s)
                # 只还原「我们确实动过」的会话。以前缺省写 1.0，等于把没被处理过
                # 的程序（新开的、在忽略名单里的）音量强行拉到 100% —— 退出/关开关
                # 时莫名其妙声音变大就是这么来的。
                if key not in self._orig:
                    continue
                # 当前值已经不等于我们上次写入的值 → 用户自己调过，别覆盖
                last = self._vol.get(key)
                try:
                    cur = float(s.SimpleAudioVolume.GetMasterVolume())
                except Exception:
                    cur = None
                if last is not None and cur is not None and abs(cur - last) > 0.05:
                    continue
                if cur is None or abs(cur - self._orig[key]) > 0.005:
                    s.SimpleAudioVolume.SetMasterVolume(self._orig[key], None)
            except Exception:
                continue
        self._vol.clear()
        self._orig.clear()
        self.muted = []
        self.controlled = 0
