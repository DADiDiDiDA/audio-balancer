"""系统默认输出设备的守护。

目标：让所有程序的播放都先进 CABLE（虚拟声卡），由本程序处理后送到真实耳机。

做法（就是 AudioBalance 的行为）：
  * 系统默认输出是虚拟声卡时 —— 什么都不做；
  * 系统默认输出被换成真实设备 A 时 —— 把 A 记成新的输出设备，
    再把系统默认输改回虚拟声卡 C（用户听到的仍然是"处理后"的声音，
    但下次想换设备只要在 Windows 里点一下即可）。

改默认设备用的是 Windows 未公开但所有播放器都在用的 IPolicyConfig，
不需要管理员权限，也不写注册表。
"""
import ctypes
from ctypes import POINTER, c_int, c_longlong, c_void_p, c_wchar_p

from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown, CoCreateInstance
from pycaw.pycaw import AudioUtilities

import session

CLSID_POLICY = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
CLSID_POLICY_VISTA = GUID("{294935CE-F637-4E7C-A41B-AB255460B862}")

EROLE_CONSOLE = 0
EROLE_MULTIMEDIA = 1


class IPolicyConfig(IUnknown):
    _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetMixFormat",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["out"], POINTER(c_void_p), "ppFormat")),
        COMMETHOD([], HRESULT, "GetDeviceFormat",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_int, "bDefault"),
                  (["out"], POINTER(c_void_p), "ppFormat")),
        COMMETHOD([], HRESULT, "ResetDeviceFormat",
                  (["in"], c_wchar_p, "pszDeviceName")),
        COMMETHOD([], HRESULT, "SetDeviceFormat",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_void_p, "pEndpointFormat"),
                  (["in"], c_void_p, "pMixFormat")),
        COMMETHOD([], HRESULT, "GetProcessingPeriod",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_int, "bDefault"),
                  (["out"], POINTER(c_longlong), "pmftDefaultPeriod"),
                  (["out"], POINTER(c_longlong), "pmftMinimumPeriod")),
        COMMETHOD([], HRESULT, "SetProcessingPeriod",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], POINTER(c_longlong), "pmftPeriod")),
        COMMETHOD([], HRESULT, "GetShareMode",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["out"], POINTER(c_void_p), "pMode")),
        COMMETHOD([], HRESULT, "SetShareMode",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_void_p, "mode")),
        COMMETHOD([], HRESULT, "GetPropertyValue",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_void_p, "key"),
                  (["out"], POINTER(c_void_p), "pv")),
        COMMETHOD([], HRESULT, "SetPropertyValue",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_void_p, "key"),
                  (["in"], c_void_p, "pv")),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_int, "eRole")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility",
                  (["in"], c_wchar_p, "pszDeviceName"),
                  (["in"], c_int, "bVisible")),
    ]


VIRTUAL_HINTS = ("cable", "vb-audio", "virtual", "虚拟")


def is_virtual_name(name):
    low = (name or "").lower()
    return any(k in low for k in VIRTUAL_HINTS)


def _policy():
    try:
        return CoCreateInstance(CLSID_POLICY, IPolicyConfig, CLSCTX_ALL)
    except Exception:
        return CoCreateInstance(CLSID_POLICY_VISTA, IPolicyConfig, CLSCTX_ALL)


def set_default_render(device_id):
    """把系统默认输出设备设为 device_id。成功返回 True。"""
    try:
        pc = _policy()
        ok = True
        for role in (EROLE_CONSOLE, EROLE_MULTIMEDIA):
            try:
                pc.SetDefaultEndpoint(device_id, role)
            except Exception:
                ok = False
        return ok
    except Exception:
        return False


_default_cache = {"id": None, "name": ""}


def default_render():
    """默认渲染（多媒体角色）端点的 IMMDevice 指针。

    比 `AudioUtilities.GetSpeakers()` 便宜得多：不激活任何接口、不读属性表。
    自动静音枚举会话时也用这个指针 Activate 会话管理器，省掉同一次 CreateDevice。
    """
    en = AudioUtilities.GetDeviceEnumerator()
    return en.GetDefaultAudioEndpoint(0, 1)     # eRender, eMultimedia


def current_default():
    """返回 (id, 名称)；失败返回 (None, "")。

    为什么不用 pycaw 的 `AudioUtilities.GetSpeakers()`：它内部会 CreateDevice，
    把端点属性表**整表**读一遍（实测每次 ~10ms），而这函数在控制循环里 1.5 秒
    调一次、界面刷新时还要调，白白烧 CPU。这里改成两步：
      1) 只取默认端点的 id（便宜的 COM 调用）；
      2) id 没变就直接用缓存的名字，只有真的换了设备才去读一次属性。
    代价是设备被重命名后要等一次切换才更新——无所谓。
    """
    try:
        ptr = default_render()
        dev_id = ptr.GetId()
    except Exception:
        try:
            d = AudioUtilities.GetSpeakers()
            return d.id, (d.FriendlyName or "")
        except Exception:
            return None, ""
    if dev_id and dev_id == _default_cache["id"] and _default_cache["name"]:
        return dev_id, _default_cache["name"]
    name = ""
    try:
        name = AudioUtilities.CreateDevice(ptr).FriendlyName or ""
    except Exception:
        pass
    _default_cache["id"], _default_cache["name"] = dev_id, name
    return dev_id, name


def is_virtual_name(name):
    low = (name or "").lower()
    return any(k in low for k in VIRTUAL_HINTS)


class DeviceGuard:
    """轮询系统默认输出，用户换设备时接管。

    注意：这里**不自己开线程**，由 main 的调度线程每 1.5 秒调一次 tick()。
    早期版本用了独立线程 + threading.Thread 的 _stop 名字，打包后在真机上
    只生效一次就再不动了（线程悄悄退出），挂到已知稳定的线程上更可靠。
    """

    def __init__(self, config, engine, interval=1.5, on_event=None, log=None):
        self.config = config
        self.engine = engine
        self.interval = float(interval)
        self.on_event = on_event
        self.log = log or (lambda msg: None)
        self.default_name = ""
        self.virtual = None          # (id, name) 最近一次看到的虚拟默认设备
        self.restored = 0            # 已接管次数
        self.error = None
        self.ticks = 0
        self._handled = None
        self._last_real = None
        self._pre = None             # 接管前的真实默认设备 (id, name)，交还时用
        self._rdp_released = False   # 已经为远程桌面交还过默认输出
        self._running = True

    # ---------- 生命周期 ----------
    def stop(self):
        self._running = False

    def tick(self):
        """由调度线程调用，内部自己处理异常，绝不往外抛。"""
        if not self._running:
            return
        self.ticks += 1
        try:
            self._tick()
            if self.ticks == 1:
                self.log("默认设备守护已启动，当前默认输出：%s" % (self.default_name or "未知"))
        except Exception as exc:
            msg = str(exc).splitlines()[0][:100] if str(exc) else type(exc).__name__
            if msg != self.error:
                self.error = msg
                self.log("默认设备守护出错：%s" % msg)

    # ---------- 核心 ----------
    def _rdp_blocked(self):
        """远程桌面里不接管（用户没打开「在远程桌面中接管」时）。"""
        try:
            return session.blocked(bool(self.config.get("rdp_takeover", False)))
        except Exception:
            return False

    def _tick(self):
        dev_id, name = current_default()
        if not dev_id:
            return
        self.default_name = name

        # 远程桌面场景：不接管，还要把之前改走的默认输出还回去
        if self._rdp_blocked():
            self._release_for_rdp(dev_id, name)
            return
        self._rdp_released = False

        # 默认已经是虚拟声卡：正常状态，记下来备用
        if is_virtual_name(name):
            self.virtual = (dev_id, name)
            self._handled = None
            return

        # 用户把默认输出换成了真实设备 —— 但我们自己刚改的不要重复处理
        if name != self._last_real:
            self._last_real = name
            self.log("检测到默认输出不是虚拟声卡：%s" % name)
        if not bool(self.config.get("auto_follow_default", True)):
            return
        if dev_id == self._handled:
            return
        self._handled = dev_id
        # 记住「接管前的真实默认设备」：远程桌面交还时优先还给它
        self._pre = (dev_id, name)

        want_back = self.virtual or self._fallback_virtual()
        # 1) 先把这个真实设备设为新的输出设备（引擎会重连到它）
        old_out = (self.config.get("output_device") or "")
        if old_out.strip().lower() != name.strip().lower():
            self.config.set("output_device", name)
            self.log("默认输出被切换为「%s」，已作为新的输出设备" % name)
            self._notify("输出设备已切换为「%s」" % name)
        self.engine.request_reload()

        # 2) 再把系统默认输出改回虚拟声卡，保证所有程序的声音仍走本程序
        if want_back is None:
            self.error = "找不到虚拟声卡，无法还原系统默认输出"
            return
        ok = set_default_render(want_back[0])
        if ok:
            self.restored += 1
            self.error = None
            self.log("已把系统默认输出还原为「%s」" % want_back[1])
        else:
            self.error = "还原系统默认输出失败（可手动在 Windows 里改回）"

    def _release_for_rdp(self, dev_id, name):
        """远程桌面里按设置不接管：把被我们改成虚拟声卡的默认输出还回去。

        只在「默认输出现在是虚拟声卡」时才动手——说明是我们（或上次运行）改的，
        不还回去的话远程桌面这边就没有声音了。
        """
        self._handled = None
        if self._rdp_released:
            return
        self._rdp_released = True
        if not is_virtual_name(name):
            self.log("远程桌面场景：按设置不接管（默认输出「%s」保持不动）"
                     % (name or "未知"))
            return
        target = self._release_target()
        if target is None:
            self.error = "远程桌面：找不到可交还的真实输出设备"
            return
        if set_default_render(target[0]):
            self.restored += 1
            self.error = None
            self.log("远程桌面场景：已把系统默认输出交还给「%s」（不接管）" % target[1])
            self._notify("远程桌面：已交还系统默认输出（未接管）")
        else:
            self.error = "远程桌面：交还系统默认输出失败（可手动改回）"

    def _release_target(self):
        """挑一个「交还」用的真实输出设备：优先接管前的那个，其次远程音频。"""
        cands = []
        try:
            for d in AudioUtilities.GetAllDevices():
                nm = d.FriendlyName or ""
                try:
                    state = int(getattr(d.state, "value", d.state))
                except Exception:
                    state = 1
                if state != 1 or is_virtual_name(nm):
                    continue
                cands.append((d.id, nm))
        except Exception:
            return None
        if not cands:
            return None
        want = ""
        for src in ((self._pre or (None, ""))[1],
                    self.config.get("output_device", "") or ""):
            if (src or "").strip():
                want = src.strip().lower()
                break
        for did, nm in cands:
            if want and nm.strip().lower() == want:
                return (did, nm)
        for did, nm in cands:
            low = nm.lower()
            if "remote" in low or "远程" in nm or "rdp" in low:
                return (did, nm)
        return cands[0]

    def _fallback_virtual(self):
        """没见过虚拟默认设备时（例如程序启动时默认已是耳机），
        从设备列表里找一个虚拟的「输入」端点。"""
        want = (self.config.get("input_device") or "").strip().lower()
        try:
            devices = AudioUtilities.GetAllDevices()
        except Exception:
            return None
        cands = []
        for d in devices:
            nm = d.FriendlyName or ""
            if not is_virtual_name(nm) or "output" in nm.lower():
                continue
            if want and want in nm.lower():
                return (d.id, nm)
            cands.append((d.id, nm))
        for did, nm in cands:
            if "input" in nm.lower():
                return (did, nm)
        return cands[0] if cands else None

    def _notify(self, text):
        if self.on_event:
            try:
                self.on_event(text)
            except Exception:
                pass

    # ---------- 给界面用 ----------
    def status(self):
        return {
            "default": self.default_name,
            "virtual": (self.virtual or (None, ""))[1],
            "restored": self.restored,
            "error": self.error,
            "rdp": self._rdp_blocked(),
        }
