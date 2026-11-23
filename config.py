"""配置读写：单个 JSON 文件，原子写入，改完立即生效。

改动不再每次立刻写盘：拖动滑块时 0.4 秒内的所有改动会合并成一次写入
（内存里立刻生效，引擎/静音模块下一次取快照就看到了），程序退出时强制落盘。
"""
import copy
import json
import os
import sys
import tempfile
import threading
import time

DEFAULTS = {
    # 均衡
    "enabled": True,
    # 处理方式：
    #   limit = 仅限制（只压超过目标的，安静内容原样通过）
    #   auto  = 自动电平（低于目标范围提上来、高于范围压下去、范围内不动）
    "level_mode": "auto",
    # 当前选中的音量均衡预设：预设名 / "custom" / ""（空 = 按数值反推，兼容旧配置）。
    # 为什么需要它：以前「选中哪一档」是靠数值反推的，点「自定义」不写任何值，
    # 反推回去还是原来那档 —— 表现为「点了自定义没反应，必须先改个数才行」。
    "loud_preset": "",
    "target_lufs": -18.0,
    # 目标是个范围：target ± range 之内不处理，超出才拉回边缘。
    # 只有自动电平模式用它（仅限制模式 range 视为 0）。
    "target_range_db": 3.0,
    # 自动电平最多往上提多少 dB（提太多会把底噪一起放大）
    "max_boost_db": 12.0,
    # 处理前衰减：送进增益/限幅之前先降低这么多 dB。
    # 最终响度 = 目标响度 - 该值，同时给限幅器留出余量。
    "pre_gain_db": 0.0,
    "max_attenuation_db": 20.0,
    "limiter_ceiling_db": -1.0,
    # 低电平拐点（护底噪）：比目标响度低 low_knee_db 以下的内容完全不提升，
    # 往上 low_knee_width_db 之内线性恢复。0 = 关闭（恢复旧的无差别提升）。
    # 为什么用它而不是噪声门：噪声门要检测、要开合过渡，反而更容易被听出来；
    # 拐点是在增益层面"不给提升"，没有开关动作。
    "low_knee_db": 34.0,
    "low_knee_width_db": 10.0,
    "lufs_window_ms": 1000,
    # 采集/播放块长（毫秒）。越小延迟越低，越小也越容易被别的线程抢占导致咔哒声。
    # 端到端延迟 ≈ 块长 + 队列余量(≤1块) + WASAPI 缓冲(块长+10)。
    "block_ms": 15,
    "downmix": True,
    # WASAPI 输出缓冲（ms）。0 = 自动（两块 + 6ms）。手调低于两块会更容易「刺啦」，
    # 只在明确要极限低延迟时才动。
    "out_buffer_ms": 0,
    # 用 Windows 音量滑块（CABLE Input 那个）控制最终音量。
    # 启动时会把 CABLE 的音量设为 100%，之后滑块直接作为输出增益。
    "use_windows_volume": True,
    # 音量曲线：滑块 20% = 正常音量(1.0)，100% = 正常音量 + winvol_max_db。
    # 线性映射下 20% 只有 0.2 倍（几乎无声），改成对数刻度后低段更好用。
    "winvol_ref": 0.20,
    "winvol_max_db": 10.0,
    "silence_gate_lufs": -60.0,
    "dc_blocker_hz": 10.0,
    "gain_attack_ms": 120.0,
    "gain_release_ms": 900.0,
    # 直通：完全不做响度处理，只搬运（排查问题 / 只想要自动静音时用）
    "passthrough": False,
    # 设备
    "input_device": "",
    "output_device": "",
    # 自动接管：Windows 里换默认输出设备时，把它当作新的输出设备，
    # 再把系统默认改回虚拟声卡
    "auto_follow_default": True,
    # 远程桌面（RDP）会话里是否也接管声音。默认 False = 不接管：
    # 在远程桌面里完全不处理声音、也不改系统默认输出，声音原样走对面设备；
    # 只有用户显式打开才像本机一样处理（会要求把默认输出改成 CABLE）。
    "rdp_takeover": False,
    # 自动静音
    "mute_enabled": True,
    # 列表里的程序在后台（最小化 / 挂机）时静音它自己 —— 默认开
    "mute_listed_in_background": True,
    # 列表里的程序在前台时，把其它程序压到「其它程序音量」—— 默认关
    "mute_others_in_game": False,
    "mute_fg_volume": 1.0,
    "mute_bg_volume": 0.0,
    "mute_easing_ms": 500,
    "mute_ignore": [],
    "mute_games": [
        "YuanShen.exe", "BH3.exe", "StarRail.exe", "ZenlessZoneZero.exe",
        "Endfield.exe", "Langrisser.exe", "Mecharashi.exe", "TDJ.exe",
        "Dragonheir.exe", "Warframe.x64.exe", "VALORANT-Win64-Shipping.exe",
        "雷索纳斯.exe", "Watcher of Realms.exe",
        "MuMuNxDevice.exe", "MuMuPlayer.exe",
        "lss.exe", "RagnarokX.exe", "dragonsoul.exe", "dragon.exe", "ywzh.exe",
    ],
    # 界面
    "start_minimized": False,
    "window_size": "940x790",
}


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class Config:
    def __init__(self, path=None, autosave_delay=0.4):
        self.path = path or os.path.join(app_dir(), "config.json")
        self.save_error = None
        self.load_error = None
        self.saved_at = 0.0
        self._data = copy.deepcopy(DEFAULTS)
        self._lock = threading.RLock()
        self._version = 0
        self._timer = None
        self._dirty = False
        self._delay = float(autosave_delay)
        self.load()

    def load(self):
        """从磁盘重读。返回是否真的读到了不一样的值（界面用它决定要不要重绘）。"""
        changed = False
        try:
            # utf-8-sig：别人（编辑器 / PowerShell）保存时可能带 BOM，
            # 用 utf-8 读会直接抛异常 → 整个配置静默失效，绝不能这样。
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                with self._lock:
                    for k, v in data.items():
                        if k in DEFAULTS and self._data.get(k) != v:
                            self._data[k] = v
                            changed = True
                    if changed:
                        self._version += 1
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.load_error = str(exc)
            self.save_error = "配置文件读取失败（已用默认值）：%s" % exc
        return changed

    def save(self):
        with self._lock:
            data = copy.deepcopy(self._data)
        tmp = None
        try:
            folder = os.path.dirname(self.path) or "."
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            tmp = None
            self.save_error = None
            self.saved_at = time.time()
            return True
        except Exception as exc:
            self.save_error = str(exc)
            return False
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        """改一项：内存立即生效，磁盘写入合并到 0.4 秒后。"""
        with self._lock:
            self._data[key] = value
            self._version += 1
        self._touch()

    def update(self, patch):
        with self._lock:
            self._data.update(patch)
            self._version += 1
        self._touch()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def version(self):
        """配置版本号：热路径（音频每块）用它判断要不要重新取一次配置。

        每块都 deepcopy 一份配置在 100 块/秒时是纯粹的浪费；版本号没变就直接复用。
        """
        with self._lock:
            return self._version

    def view(self):
        """浅拷贝快照，给热路径用（比 deepcopy 便宜一个数量级）。

        引擎只读、不改里面的值；列表类配置（名单）它也不碰，浅拷贝足够安全。
        """
        with self._lock:
            return dict(self._data)

    # ---------- 自动保存 ----------
    def _touch(self):
        with self._lock:
            self._dirty = True
            if self._timer is not None:
                return
            t = threading.Timer(self._delay, self._flush)
            t.daemon = True
            self._timer = t
            t.start()

    def _flush(self):
        with self._lock:
            self._timer = None
            if not self._dirty:
                return
            self._dirty = False
        self.save()

    def save_now(self):
        """立即落盘（退出、点「立即保存」时用）。"""
        with self._lock:
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None
            self._dirty = False
        return self.save()

    # ---------- 给界面看的状态 ----------
    @property
    def save_delay(self):
        return self._delay

    def is_dirty(self):
        """还有改动没落盘。界面右下角按钮靠它显示「保存中…/已保存」。"""
        with self._lock:
            return bool(self._dirty)
