"""音频均衡器 · 单一程序入口。

一个进程里同时做两件事：
  1. 响度均衡（CABLE 回环采集 -> BS.1770 测量 -> 只压不升 -> 软限幅 -> 耳机）
  2. 游戏时自动静音其它程序

只有一个托盘图标、一个窗口。
"""
import ctypes
import ctypes.wintypes as wt
import os
import queue
import sys
import threading
import time
import traceback

import muter as muter_mod          # 先 import：pycaw(comtypes) 必须先于 soundcard
import theme
from config import Config, app_dir
from device_guard import DeviceGuard
from engine import Engine
from gui import APP_TITLE, AppWindow, show_error
from muter import Muter, ensure_com

MUTEX_NAME = "Local\\AudioBalancer_v1"
EVENT_NAME = "Local\\AudioBalancer_Show"
LOG_PATH = os.path.join(app_dir(), "音频均衡器.log")


def resource_path(name):
    """打包后资源在 _MEIPASS 里，源码运行就在本目录。"""
    base = getattr(sys, "_MEIPASS", app_dir())
    return os.path.join(base, name)


# use_last_error=True：否则 CreateMutexW 之后读到的 LastError 不可靠，
# 会把"已有实例"误判成"第一个实例"，于是又开一个，出现两个托盘/两个引擎。
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SW_RESTORE = 9
PROCESS_NAME = "音频均衡器.exe"


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _existing_windows():
    """已有实例的顶层窗口句柄（按进程名 + 标题找，不依赖 IPC）。

    只认标题是「音频均衡器」的窗口：PyInstaller 自带的
    "PyInstaller Onefile Hidden Window" 也是本进程的窗口，
    一旦被 ShowWindow 就会弹出个黑色空窗口。
    """
    try:
        import psutil
    except Exception:
        return []
    pids = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() == PROCESS_NAME.lower():
                pids.add(p.info["pid"])
        except Exception:
            continue
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if APP_TITLE in (buf.value or ""):
                found.append((hwnd, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(cb, 0)
    return found


def acquire_single_instance():
    """返回 (是否第一个实例, 事件句柄)。已有实例时通知它显示窗口。"""
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return False, None
    ev = kernel32.CreateEventW(None, False, False, EVENT_NAME)
    if not ev:                      # 事件已存在（上一份互斥体已被回收）
        return False, None
    return True, ev


def notify_existing():
    """通知已有实例把窗口显示出来；不顺带开新进程。"""
    h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, EVENT_NAME)
    if h:
        kernel32.SetEvent(h)
        kernel32.CloseHandle(h)
    # 等它自己 show()（窗口出现需要一点时间），再顺手拉到最前
    for _ in range(10):
        time.sleep(0.15)
        wins = _existing_windows()
        vis = [hwnd for hwnd, shown in wins if shown]
        if vis:
            try:
                user32.SetForegroundWindow(vis[0])
            except Exception:
                pass
            return


def watch_show_event(handle, cmds):
    INFINITE = 0xFFFFFFFF
    while True:
        rc = kernel32.WaitForSingleObject(handle, INFINITE)
        if rc == 0:
            cmds.put("show")


PHASE = {"name": "idle", "since": time.time()}


def set_phase(name):
    PHASE["name"] = name
    PHASE["since"] = time.time()


def watchdog(guard):
    """盯住控制线程：卡在哪一步、卡了多久，直接写进日志。
    控制线程被某个 COM 调用挂住时，这里还能继续跑，是唯一的现场证据。"""
    last = time.time()
    while True:
        time.sleep(5)
        now = time.time()
        stuck = now - PHASE["since"]
        if stuck > 8:
            log("控制线程卡在「%s」已 %.0f 秒" % (PHASE["name"], stuck))
            continue
        if now - last >= 30:
            last = now
            log("心跳 guard.ticks=%d 默认=%s 注入=%d 错误=%s 阶段=%s"
                % (guard.ticks if guard else -1,
                   (guard.default_name if guard else "") or "-",
                   guard.restored if guard else -1,
                   (guard.error if guard else None) or "-",
                   PHASE["name"]))


def control_loop(config, engine, muter, cmds, guard=None):
    """自动静音的驱动线程；顺便监控引擎异常、轮询系统默认输出设备。"""
    ensure_com()
    last_err = None
    last_guard = 0.0
    while True:
        try:
            # 先做设备守护：切默认输出会让会话枚举变慢，若放在后面，
            # 守护会被 muter 拖住几十秒才响应。
            if guard is not None:
                now = time.time()
                if now - last_guard >= guard.interval:
                    last_guard = now
                    set_phase("guard.tick")
                    t1 = time.time()
                    guard.tick()
                    dg = time.time() - t1
                    if dg > 2.0:
                        log("guard.tick 耗时 %.1f 秒" % dg)
            set_phase("muter.tick")
            t0 = time.time()
            muter.tick(time.time())
            dm = time.time() - t0
            if dm > 2.0:
                log("muter.tick 耗时 %.1f 秒" % dm)
            set_phase("idle")
            err = engine.status.get("error")
            if err != last_err:
                last_err = err
                if err:
                    log("引擎异常：%s" % err)
        except Exception:
            log("控制循环异常：\n" + traceback.format_exc())
        time.sleep(0.3)


def make_icon_image():
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    size = 128
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, size - 5, size - 5), fill=(37, 99, 235, 255))
    # 三条高低不同的柱，表示"把音量拉平"
    bars = [(30, 62, 52, 96), (54, 44, 76, 96), (78, 54, 100, 96)]
    for x0, y0, x1, y1 in bars:
        d.rounded_rectangle((x0, y0, x1, y1), radius=6, fill=(255, 255, 255, 235))
    return img


def build_tray(config, cmds):
    try:
        import pystray
        from pystray import Menu, MenuItem
    except Exception as exc:
        log("托盘不可用：%s" % exc)
        return None

    image = make_icon_image()

    def show(icon, item):
        cmds.put("show")

    def toggle_enabled(icon, item):
        config.set("enabled", not config.get("enabled", True))

    def toggle_mute(icon, item):
        config.set("mute_enabled", not config.get("mute_enabled", True))

    def quit_app(icon, item):
        cmds.put("quit")

    menu = Menu(
        MenuItem("打开主界面", show, default=True),
        Menu.SEPARATOR,
        MenuItem("响度均衡", toggle_enabled,
                 checked=lambda i: bool(config.get("enabled", True))),
        MenuItem("自动静音", toggle_mute,
                 checked=lambda i: bool(config.get("mute_enabled", True))),
        Menu.SEPARATOR,
        MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("AudioBalancer", image, APP_TITLE, menu)
    return icon


def main():
    theme.set_dpi_aware()          # 否则 125% 缩放下整屏字被系统拉糊
    ok, ev = acquire_single_instance()
    if not ok:
        # 已经有实例在跑：把它的窗口叫出来，绝不再起一个引擎/托盘
        notify_existing()
        return

    log("===== 启动 =====")
    config = Config()
    engine = Engine(config)
    muter = Muter(config)
    guard = DeviceGuard(config, engine, on_event=log, log=log)
    cmds = queue.Queue()

    import tkinter as tk
    root = tk.Tk()
    ico = resource_path("icon.ico")
    if os.path.exists(ico):
        try:
            root.iconbitmap(default=ico)
        except Exception:
            pass
    hidden = ("--minimized" in sys.argv) or bool(config.get("start_minimized", False))
    app = AppWindow(root, config, engine, muter, cmds, guard=guard, start_hidden=hidden)

    icon = build_tray(config, cmds)
    if icon is not None:
        threading.Thread(target=icon.run, daemon=True).start()

    threading.Thread(target=watch_show_event, args=(ev, cmds), daemon=True).start()
    threading.Thread(target=control_loop, args=(config, engine, muter, cmds, guard),
                     daemon=True).start()
    threading.Thread(target=watchdog, args=(guard,), daemon=True).start()

    ensure_com()
    engine.start()

    try:
        root.mainloop()
    finally:
        log("===== 退出 =====")
        try:
            guard.stop()
        except Exception:
            pass
        try:
            muter.restore()
        except Exception:
            pass
        engine.stop()
        try:
            config.save_now()
        except Exception:
            pass
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("启动失败：\n" + traceback.format_exc())
        try:
            show_error("音频均衡器", "启动失败：\n%s" % traceback.format_exc()[-600:])
        except Exception:
            pass
        sys.exit(1)
