"""开机自启管理（HKCU\\Run，不需要管理员权限）。

开机时以 --minimized 启动：只跑核心进程、不开界面，不打扰用户。
自启的真实状态以注册表为准（可能被用户手动改过），不另存到 config.json。
"""
import os
import sys

APP_NAME = "音频均衡器"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def exe_command():
    """返回写进注册表的启动命令。"""
    if getattr(sys, "frozen", False):
        # 打包后：exe 路径 + --minimized
        return '"%s" --minimized' % os.path.abspath(sys.executable)
    # 源码模式：pythonw 无窗口启动 main.py
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    main = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return '"%s" "%s" --minimized' % (pyw, main)


def is_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value, _ = winreg.QueryValueEx(k, APP_NAME)
            return bool(value)
    except Exception:
        return False


def set_enabled(on):
    try:
        import winreg
        if not on:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
            return True
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, exe_command())
        return True
    except Exception:
        return False
