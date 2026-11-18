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

MUTEX_CORE = "Local\\AudioBalancer_v1"
MUTEX_UI = "Local\\AudioBalancer_UI_v1"
EV_SHOW_UI = "Local\\AudioBalancer_ShowUI"
EV_UI_SHOW = "Local\\AudioBalancer_UIShow"
EV_UI_QUIT = "Local\\AudioBalancer_UIQuit"

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def acquire_mutex(name):
    """返回 (是否第一个实例, 互斥体句柄)。"""
    kernel32.CreateMutexW(None, False, name)
    return (ctypes.get_last_error() != ERROR_ALREADY_EXISTS)


def create_event(name):
    return kernel32.CreateEventW(None, False, False, name)


def set_event(name):
    h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if h:
        kernel32.SetEvent(h)
        kernel32.CloseHandle(h)
        return True
    return False


def write_status(data):
    try:
        folder = os.path.dirname(STATUS_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATUS_PATH)
        return True
    except Exception:
        return False


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
