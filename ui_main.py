"""UI 进程入口：只跑界面，不碰音频引擎/静音/设备守护。

启动方式：音频均衡器.exe --ui
由核心进程按需拉起；直接双击（无参数）时核心进程也会拉起它。
"""
import os
import queue
import sys
import threading
import time

import ipc
import theme


def _bring_existing_ui():
    """已有 UI 实例时把它叫到前台（通过命名事件让它自己 lift）。"""
    ipc.set_event(ipc.EV_UI_SHOW)


class StatusEngine:
    """引擎状态的只读代理。核心进程才是真正干活的那一个。"""

    def __init__(self):
        self.status = {}

    def list_inputs(self):
        return list(self.status.get("inputs") or [])

    def list_outputs(self):
        return list(self.status.get("outputs") or [])

    def request_reload(self):
        # 配置文件的变化由核心进程自己监视（见 core.control_loop）
        pass

    def start(self):
        pass

    def stop(self):
        pass


class StatusMuter:
    def __init__(self):
        self.in_game = False
        self.foreground = ""
        self.controlled = 0
        self.muted = []

    def restore(self):
        # 关 UI 不影响核心进程的自动静音
        pass


class StatusGuard:
    def __init__(self):
        self._s = {}

    def status(self):
        return self._s

    def stop(self):
        pass


def _watch_status(status_engine, muter, guard, config, cmds):
    """后台轮询：状态文件 + 配置变化 + 退出事件。"""
    last_mtime = 0.0
    # 刚启动时核心可能还没写出状态文件（双击 exe 时两边几乎同时起），
    # 直接按 status_age 判「核心已退出」会让界面 accidentally 自杀。
    grace_until = time.time() + 12.0
    while True:
        st = ipc.read_status()
        if st:
            s = dict(st)
            s.pop("inputs", None)
            s.pop("outputs", None)
            s.pop("guard", None)
            s.pop("muted", None)
            s.pop("ts", None)
            status_engine.status = s
            status_engine.status["inputs"] = st.get("inputs") or []
            status_engine.status["outputs"] = st.get("outputs") or []
            muter.in_game = bool(st.get("mute_in_game"))
            muter.foreground = st.get("foreground") or ""
            muter.controlled = int(st.get("controlled") or 0)
            muter.muted = list(st.get("muted") or [])
            guard._s = dict(st.get("guard") or {})
        # 核心进程结束 → UI 跟着退（启动宽限期内不看年龄）
        if time.time() >= grace_until and ipc.status_age() > 6:
            cmds.put("quit")
            return
        try:
            mtime = os.path.getmtime(config.path)
            if mtime != last_mtime:
                if last_mtime:
                    config.load()
                last_mtime = mtime
        except Exception:
            pass
        time.sleep(0.25)


def _watch_events(cmds):
    import ctypes
    show = ipc.create_event(ipc.EV_UI_SHOW)
    quit_ev = ipc.create_event(ipc.EV_UI_QUIT)
    k32 = ctypes.windll.kernel32
    while True:
        # 分别等两个事件：都复位后继续等（0.05s 轮询，不占 CPU）
        for h, cmd in ((show, "show"), (quit_ev, "quit")):
            if k32.WaitForSingleObject(h, 0) == 0:
                cmds.put(cmd)
        time.sleep(0.05)


def run_ui():
    if not ipc.acquire_mutex(ipc.MUTEX_UI):
        _bring_existing_ui()
        return
    theme.set_dpi_aware()

    import tkinter as tk
    from config import Config, app_dir
    from gui import APP_TITLE, AppWindow

    config = Config()
    engine = StatusEngine()
    muter = StatusMuter()
    guard = StatusGuard()
    cmds = queue.Queue()

    root = tk.Tk()
    # 打包后 icon.ico 在 PyInstaller 的 _MEIPASS 临时目录，不在 exe 旁边
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(getattr(sys, "_MEIPASS", ""))
    bases.append(app_dir())
    bases.append(os.path.dirname(os.path.abspath(__file__)))
    for base in bases:
        if not base:
            continue
        ico = os.path.join(base, "icon.ico")
        if os.path.exists(ico):
            try:
                root.iconbitmap(default=ico)
                break
            except Exception:
                pass
    app = AppWindow(root, config, engine, muter, cmds, guard=guard)
    # UI 是按需进程：关掉窗口就真的退出 UI，核心进程继续在托盘里跑；
    # 点托盘「打开主界面」再拉起一个新的 UI 进程。
    root.protocol("WM_DELETE_WINDOW", app.quit)

    threading.Thread(target=_watch_status,
                     args=(engine, muter, guard, config, cmds), daemon=True).start()
    threading.Thread(target=_watch_events, args=(cmds,), daemon=True).start()
    try:
        root.mainloop()
    finally:
        try:
            config.save_now()
        except Exception:
            pass


if __name__ == "__main__":
    run_ui()
