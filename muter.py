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


def foreground_process_name():
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    try:
        return (psutil.Process(pid.value).name() or "").lower()
    except Exception:
        return ""


class Muter:
    def __init__(self, config):
        self.config = config
        self._vol = {}
        self._orig = {}          # key -> 接管前用户自己的音量
        self._sessions = []
        self._names = {}
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
            if len(self._names) > 300:
                self._names.clear()

        games = {str(g).lower() for g in (cfg.get("mute_games") or [])}
        ignore = {str(g).lower() for g in (cfg.get("mute_ignore") or [])}
        self.foreground = foreground_process_name()
        self.in_game = bool(games) and self.foreground in games
        mute_bg_listed = bool(cfg.get("mute_listed_in_background", True))
        mute_others = bool(cfg.get("mute_others_in_game", True))

        fg_vol = float(cfg.get("mute_fg_volume", 1.0))
        bg_vol = float(cfg.get("mute_bg_volume", 0.0))
        alpha = min(1.0, 200.0 / max(100.0, float(cfg.get("mute_easing_ms", 500))))

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
            # 记住用户原本的音量，恢复时按原值还原，而不是粗暴拉满 1.0
            if key not in self._orig:
                self._orig[key] = raw

            listed = name in games
            is_foreground = bool(name) and name == self.foreground
            if listed and is_foreground:
                # 列表里的程序在前台：保持正常音量
                target = fg_vol
            elif listed and mute_bg_listed:
                # 列表里的程序在后台（最小化/挂机）：静音它自己
                target = bg_vol
            elif self.in_game and mute_others:
                # 列表里的程序在前台：其它程序让路
                target = bg_vol
            else:
                target = self._orig[key]

            cur = self._vol.get(key, raw)
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
                s.SimpleAudioVolume.SetMasterVolume(self._orig[key], None)
            except Exception:
                continue
        self._vol.clear()
        self._orig.clear()
        self.muted = []
        self.controlled = 0
