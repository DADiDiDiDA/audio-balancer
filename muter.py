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
import json
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


def _user_vol(raw):
    """把「接管前的音量」规整成用户真正的意图。

    0 不能当原值：那多半是**我们自己**压下去的（或者上一个版本压成 0 之后
    Windows 把它记住了）。把 0 记成原值，前台恢复算出来还是 0，表现就是
    「模拟器切到前台也没声音」，而且重启程序也救不回来。取不到/为 0 时
    一律当 1.0 处理。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(v) or v <= 0.01:
        return 1.0
    return min(1.0, v)


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
        self._orig = {}          # key -> (接管前音量, pid, 进程名)
        self._sessions = []
        self._names = {}
        self._games_seen = set()
        self._fg_pid = 0
        self._stopping = False
        self._last_enum = 0.0
        self._last_tick = None
        self._self_pid = os.getpid()
        self.foreground = ""
        self.in_game = False
        self.controlled = 0
        self.muted = []          # 正在被压低的程序名（界面显示用）
        self.error = None
        # 上次被我们压过的程序 -> 它原本的音量。只存内存不够：均衡器重启后
        # 会话可能还停在 0（Windows 会记住每个程序的应用音量），此时既没有
        # 接管记录、又把 0 当成用户意图，前台也恢复不了 —— 就是「模拟器前台
        # 也没声音」。所以按进程名落一份小状态，重启后能认出来并还原。
        self._saved = {}
        self._saved_written = {}
        self._last_save = 0.0
        self._load_saved()

    # ---------- 原音量状态（跨进程重启保留） ----------
    def _state_path(self):
        try:
            base = os.path.dirname(str(self.config.path))
        except Exception:
            return ""
        return os.path.join(base, "mute_state.json") if base else ""

    def _load_saved(self):
        p = self._state_path()
        if not p:
            return
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            vols = data.get("volumes") if isinstance(data, dict) else None
            if isinstance(vols, dict):
                self._saved = {str(k).lower(): _user_vol(v) for k, v in vols.items()}
                self._saved_written = dict(self._saved)
        except Exception:
            self._saved = {}

    def _remember(self, now):
        """把「进程名 -> 原音量」写盘（最多 5 秒一次，内容没变不写）。

        以已写过的内容为底再叠加：restore() 会清空 `_orig`，若直接整份覆盖，
        刚恢复完就把记录写没了，下次启动又认不出「那些 0 是我们压的」。
        """
        cur = dict(self._saved_written)
        for rec in self._orig.values():
            name, orig = rec[2], _user_vol(rec[0])
            if name:
                cur[name] = orig
        if cur == self._saved_written or now - self._last_save < 5.0:
            return
        p = self._state_path()
        if not p:
            return
        try:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"volumes": cur}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            self._saved_written = dict(cur)
            self._saved = dict(cur)
            self._last_save = now
        except Exception:
            pass

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

    def _orig_key(self, s, name):
        """在 _orig 里找这个会话的记录：优先会话标识，其次 pid+进程名。

        会话标识（InstanceIdentifier）会随进程重开 / 音频引擎重建而变化。
        只按标识记的话，程序重开一次就再也认不出「这是我压低过的」，
        音量会永久停在 0 —— 删名单也救不回来。
        """
        key = self._key(s)
        if key in self._orig:
            return key
        pid = int(getattr(s, "ProcessId", 0) or 0)
        if not pid:
            return ""
        for k, rec in list(self._orig.items()):
            if rec[1] == pid and (not name or rec[2] == name):
                # 迁移到新标识，后续写入/恢复都走同一个键
                self._orig[key] = self._orig.pop(k)
                if k in self._vol:
                    self._vol[key] = self._vol.pop(k)
                return key
        return ""

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
            # _orig 非空也要还原：只判断 _vol 时，会话标识变化后 _vol 已清、
            # _orig 还留着记录，关掉开关就再也恢复不了。
            if self._vol or self._orig:
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
        # 前台是否丢掉用户手动音量、直接写成固定值
        force_fg = bool(cfg.get("mute_force_fg", False))
        fg_default = _num(cfg, "mute_fg_volume", 1.0, 0.0, 1.0)
        fg_map = cfg.get("mute_fg_map")
        fg_map = fg_map if isinstance(fg_map, dict) else {}

        def fg_for(nm):
            """这个程序在前台时的音量系数（没单独设过就用默认值）。"""
            v = fg_map.get(nm)
            if v is None:
                return fg_default
            try:
                v = float(v)
            except (TypeError, ValueError):
                return fg_default
            if not math.isfinite(v):
                return fg_default
            return max(0.0, min(1.0, v))

        bg_vol = _num(cfg, "mute_bg_volume", 0.0, 0.0, 1.0)
        # 渐变系数按「真实经过时间」算，而不是固定按 0.3 秒一次估：
        # 以前 easing=300ms 实际要约 0.9 秒才到位，设 2000ms 和 1000ms 听感差不多。
        if self._last_tick is None:
            # 第一拍只记录时间：没有「上一次」就没有真实间隔，不能凭 0.3 秒猜
            self._last_tick = now
            alpha = 0.0
        else:
            dt = min(max(now - self._last_tick, 0.0), 1.0)
            self._last_tick = now
            tau = max(0.05, _num(cfg, "mute_easing_ms", 500, 50, 10000) / 1000.0)
            alpha = 1.0 - math.exp(-dt / tau)
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
            okey = self._orig_key(s, name)
            if okey:
                key = okey               # 会话标识变过：后面的记录都用迁移后的键
                seen.add(key)            # 迁移后的键也算「本轮见过」，否则会被末尾清理掉
            if (listed and key not in self._orig and name
                    and raw <= 0.01 and name in self._saved):
                # 上一次运行把它压成了 0，Windows 也把这个 0 记住了（程序重启
                # 后会话音量还是 0）。原值从状态文件里取回来，否则前台也算 0。
                self._orig[key] = (self._saved[name],
                                   int(getattr(s, "ProcessId", 0) or 0), name)
            if listed and is_foreground:
                # 在前台：回到「用户自己设的音量」，再乘上这个程序配的比例。
                # 以前直接用 fg_vol（默认 100%），会把用户手动调到 30% 的游戏
                # 硬拉回 100% —— 听起来就是「程序自己在改我的音量」。
                # 打开「丢弃手动音量」后就不乘了，直接写死成这个固定值。
                vol = fg_for(name)
                if force_fg:
                    target = vol
                else:
                    base = (_user_vol(self._orig[key][0]) if key in self._orig
                            else _user_vol(raw))
                    target = base * vol
            elif listed and mute_bg_listed and not fg_uncertain:
                target = bg_vol          # 在后台（最小化/挂机）：静音它自己
            else:
                # 不在处理范围：如果我们之前动过，还原一次，然后完全放手。
                # 以前会把 target 设成"记录的原始音量"，于是用户手动把音量调小
                # 之后，音量条会被一点点拉回去（看起来像自己在跳）。
                if key in self._orig:
                    orig = self._orig.pop(key)[0]
                    self._vol.pop(key, None)
                    try:
                        if abs(raw - orig) > 0.005:
                            s.SimpleAudioVolume.SetMasterVolume(orig, None)
                    except Exception:
                        pass
                continue

            last = self._vol.get(key)
            if key not in self._orig:
                if abs(target - raw) <= 0.005:
                    # 不需要改它（例如前台 100%、用户本来就是这个音量）：
                    # 完全放手，连「接管记录」都不留，避免退出时反而去动它
                    continue
                # 只有真的要改它时才记原值，避免把没动过的程序也记下来
                self._orig[key] = (_user_vol(raw), int(getattr(s, "ProcessId", 0) or 0), name)
            elif last is not None and abs(raw - last) > 0.05:
                # 用户自己拖了音量条：以他的新值为基准，别再拉回旧值
                self._orig[key] = (_user_vol(raw), int(getattr(s, "ProcessId", 0) or 0), name)

            cur = last if last is not None else raw
            new = cur + (target - cur) * alpha
            if abs(new - target) < 0.01:
                new = target
            self._vol[key] = new
            try:
                if abs(raw - new) > 0.005:
                    s.SimpleAudioVolume.SetMasterVolume(new, None)
                if new < 0.99 and new < self._orig[key][0] - 0.05:
                    controlled += 1
                    muted.append(name or ("pid %s" % s.ProcessId))
            except Exception:
                continue
        for k in [k for k in self._vol if k not in seen]:
            self._vol.pop(k, None)
        # 会话暂时不在枚举里（程序暂停出声 / 采集空档）时保留记录：
        # 进程还活着就等它回来，进程没了才丢弃。否则「删除名单后恢复」
        # 会因为这些记录被清掉而永远恢复不了。
        dead = [k for k, rec in self._orig.items()
                if k not in seen and not _pid_alive(rec[1])]
        for k in dead:
            self._orig.pop(k, None)
            self._vol.pop(k, None)
        if len(self._orig) > 200:
            self._orig = {k: v for k, v in self._orig.items() if k in seen}
        self.controlled = controlled
        self.muted = list(dict.fromkeys(muted))[:5]
        self._remember(now)

    def restore(self):
        for s in self._sessions or self._enum():
            try:
                if not (s.SimpleAudioVolume and s.ProcessId):
                    continue
                if s.ProcessId == self._self_pid:
                    continue
                name = self._pname(s)
                # 只还原「我们确实动过」的会话。以前缺省写 1.0，等于把没被处理过
                # 的程序（新开的、在忽略名单里的）音量强行拉到 100% —— 退出/关开关
                # 时莫名其妙声音变大就是这么来的。
                okey = self._orig_key(s, name)
                if not okey:
                    continue
                orig = _user_vol(self._orig[okey][0])
                # 当前值已经不等于我们上次写入的值 → 用户自己调过，别覆盖
                last = self._vol.get(okey)
                try:
                    cur = float(s.SimpleAudioVolume.GetMasterVolume())
                except Exception:
                    cur = None
                if last is not None and cur is not None and abs(cur - last) > 0.05:
                    continue
                if cur is None or abs(cur - orig) > 0.005:
                    s.SimpleAudioVolume.SetMasterVolume(orig, None)
            except Exception:
                continue
        self._vol.clear()
        self._orig.clear()
        self.muted = []
        self.controlled = 0


def _pid_alive(pid):
    if not pid:
        return False
    try:
        return psutil.pid_exists(int(pid))
    except Exception:
        return False
