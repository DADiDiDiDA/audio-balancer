"""会话判定：本进程是不是跑在远程桌面（RDP）会话里、本地会话有没有被挤掉。

为什么单独拎出来：远程桌面和本地控制台是两个会话，默认输出设备、音频端点
都是各算各的。程序默认**不接管**远程桌面的声音（不处理响度、也不改默认输出），
这样 RDP 里的声音直接送到对面设备；用户在「音频设备」页打开「在远程桌面中接管」
后才像本机一样接管。

判定分两路（任一条成立就算「当前处于远程桌面场景」）：
  1. 本进程所在会话是 RDP 会话（程序是在远程桌面里启动的，最常见）；
  2. 本进程在控制台会话里，但这个会话现在是「已断开」——有人用远程桌面连进来，
     把本地会话挤掉了（这时本机没人听声音，继续抢 CABLE 只会让两边都别扭）。
"""
import ctypes
import time

SM_REMOTESESSION = 0x1000
WTS_CURRENT_SESSION = -1
WTSClientProtocolType = 16
WTSConnectState = 8
WTS_PROTOCOL_RDP = 2
WTS_ACTIVE = 0
WTS_DISCONNECTED = 4

_flag = None       # 进程在哪个会话是启动时就定死的，只探测一次就够
_sid = None
_detached = {"t": -1e9, "value": False}


def session_id():
    """本进程所在的 Windows 会话 ID（控制台一般是 1，远程桌面是 2 起）。"""
    global _sid
    if _sid is not None:
        return _sid
    try:
        sid = ctypes.c_ulong(0xFFFFFFFF)
        k32 = ctypes.windll.kernel32
        k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid))
        _sid = int(sid.value)
    except Exception:
        _sid = -1
    return _sid


def _wts_query(cls, fmt):
    """查当前会话的一项 WTS 信息，按 fmt 解释；失败返回 None。"""
    try:
        buf = ctypes.c_void_p()
        size = ctypes.c_ulong(0)
        ok = ctypes.windll.wtsapi32.WTSQuerySessionInformationW(
            None, WTS_CURRENT_SESSION, cls, ctypes.byref(buf), ctypes.byref(size))
        if not ok or not buf.value:
            return None
        try:
            return ctypes.cast(buf, ctypes.POINTER(fmt))[0]
        finally:
            ctypes.windll.wtsapi32.WTSFreeMemory(buf)
    except Exception:
        return None


def detect():
    """本进程所在会话是不是远程桌面（不走缓存）。"""
    # 1) 官方推荐：GetSystemMetrics(SM_REMOTESESSION) 非 0 = 本会话是远程桌面
    try:
        if ctypes.windll.user32.GetSystemMetrics(SM_REMOTESESSION):
            return True
    except Exception:
        pass
    # 2) 兜底：查会话的客户端协议类型（2 = RDP，0 = 控制台）
    proto = _wts_query(WTSClientProtocolType, ctypes.c_ushort)
    if proto is not None:
        return int(proto) == WTS_PROTOCOL_RDP
    return False


def is_remote(force=False):
    """本进程是否在远程桌面会话中。结果缓存（会话不会中途改变）。"""
    global _flag
    if force or _flag is None:
        _flag = detect()
    return bool(_flag)


def connect_state():
    """本会话的连接状态：0=活动、4=已断开、-1=查不到。"""
    v = _wts_query(WTSConnectState, ctypes.c_uint)
    return int(v) if v is not None else -1


def console_detached(ttl=1.5):
    """在控制台会话里，但它现在是「已断开」（通常是被远程桌面挤掉的）。

    结果缓存 ttl 秒：引擎每块、界面每次刷新都会问一次，
    真去查 WTS 每次都是内核/LSA 调用，没必要。
    """
    now = time.time()
    if now - _detached["t"] < ttl:
        return _detached["value"]
    value = False
    try:
        if not is_remote():
            console = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
            value = int(console) == session_id() and connect_state() == WTS_DISCONNECTED
    except Exception:
        value = False
    _detached["t"], _detached["value"] = now, value
    return value


def blocked(takeover=False):
    """当前是否应该「不接管声音」。takeover = 用户打开的「在远程桌面中接管」。"""
    if takeover:
        return False
    return is_remote() or console_detached()


def describe():
    """写日志用的一句话。"""
    if is_remote():
        return "远程桌面会话（会话 %d）" % session_id()
    if console_detached():
        return "控制台会话已断开（会话 %d，被远程桌面占用）" % session_id()
    return "本机会话（会话 %d）" % session_id()
