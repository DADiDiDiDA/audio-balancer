"""核心进程与 UI 进程之间的轻量 IPC。

约定：
  * 状态：核心进程每 ~0.2s 原子写一份 JSON，UI 进程只读；
  * 配置：两边都指向同一个 config.json，核心进程监视 mtime 自动重载；
  * 事件：Windows 命名事件，用来「唤出 UI」「通知 UI 退出」。
  不用网络端口，不用管理员权限。
"""
import ctypes
import json
import os
import tempfile

from config import app_dir

STATUS_PATH = os.path.join(app_dir(), ".audiobalancer_status.json")

# 用 Global\ 而非 Local\：Local 内核对象按会话隔离，远程桌面会另开一个会话，
# 导致每个会话各起一个核心进程、抢同一个 CABLE 设备（表现：报「找不到 CABLE」
# 但声音又正常，日志里出现两套心跳）。Global 让整机只有一个实例。
NS = "Global\\"
MUTEX_CORE = NS + "AudioBalancer_v2"
MUTEX_UI = NS + "AudioBalancer_UI_v2"
EV_SHOW_UI = NS + "AudioBalancer_ShowUI_v2"
EV_UI_SHOW = NS + "AudioBalancer_UIShow_v2"
EV_UI_QUIT = NS + "AudioBalancer_UIQuit_v2"
# 界面点「重新扫描」→ 让核心立刻重新枚举一次音频设备（否则最多要等 15 秒）
EV_RESCAN = NS + "AudioBalancer_Rescan_v2"

ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5
EVENT_MODIFY_STATE = 0x0002

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# 64 位下 HWND/HANDLE 是指针，默认 c_int 会截断导致句柄失效
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.OpenEventW.restype = ctypes.c_void_p


def acquire_mutex(name):
    """返回是否第一个实例。Global 无权限时回退 Local（按会话隔离，聊胜于无）。"""
    ctypes.set_last_error(0)
    kernel32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if err == ERROR_ACCESS_DENIED and name.startswith(NS):
        ctypes.set_last_error(0)
        kernel32.CreateMutexW(None, False, "Local\\" + name[len(NS):])
        err = ctypes.get_last_error()
    return err != ERROR_ALREADY_EXISTS


def create_event(name):
    h = kernel32.CreateEventW(None, False, False, name)
    if not h and name.startswith(NS):
        # Global 事件无权限时回退 Local（同会话内仍可用）
        h = kernel32.CreateEventW(None, False, False, "Local\\" + name[len(NS):])
    return h


def set_event(name):
    h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not h and name.startswith(NS):
        h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False,
                                "Local\\" + name[len(NS):])
    if h:
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.SetEvent(h)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(h)
        return True
    return False


def write_status(data):
    tmp = None
    try:
        folder = os.path.dirname(STATUS_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATUS_PATH)
        tmp = None
        return True
    except Exception:
        return False
    finally:
        # os.replace 偶发失败（目标文件正被读）会留下 .tmp，跑久了堆一地
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def read_status():
    try:
        with open(STATUS_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def status_age():
    try:
        return abs(__import__("time").time() - os.path.getmtime(STATUS_PATH))
    except Exception:
        return 1e9
