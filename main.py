"""音频均衡器 · 核心进程。

职责：音频引擎、自动静音、设备守护、托盘。**不创建任何窗口，也不 import tkinter**。
界面在独立进程（--ui）里按需启动；关掉界面核心继续跑，开机自启只跑核心。

一个托盘图标、一个常驻进程。
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback

import ipc
import muter as muter_mod          # 先 import：pycaw(comtypes) 必须先于 soundcard
import session
import theme
from config import Config, app_dir
from device_guard import DeviceGuard
from engine import Engine
from muter import Muter, ensure_com

LOG_PATH = os.path.join(app_dir(), "音频均衡器.log")
LOG_MAX = 1024 * 1024      # 超过 1MB 就轮转成 .log.1（常驻几天能涨到几 MB）


def log(msg):
    try:
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX:
                os.replace(LOG_PATH, LOG_PATH + ".1")
        except OSError:
            pass            # 文件不存在 / 正被占用：照常追加就行
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def spawn_ui():
    """拉起（或唤出）界面进程。核心进程永远不自己建窗口。"""
    try:
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--ui"]
        else:
            args = [sys.executable, os.path.join(app_dir(), "main.py"), "--ui"]
        subprocess.Popen(args, close_fds=True)
    except Exception:
        log("启动界面失败：\n" + traceback.format_exc())


def make_icon_image():
    """托盘、窗口、exe 共用同一张 icon.ico，保证三处图标完全一致。"""
    ico = os.path.join(app_dir(), "icon.ico")
    if not os.path.exists(ico):
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    # 托盘 / 窗口 / exe 三者都从同一个 icon.ico 取，保证完全一致。
    # 注意：多尺寸 ICO 必须设 .size 再 .load().copy()；直接 convert 可能拿到空帧。
    try:
        from PIL import Image, ImageDraw
        img = None
        with Image.open(ico) as f:
            for sz in (64, 32, 256, 48):
                try:
                    f.size = (sz, sz)
                    f.load()
                    cand = f.convert("RGBA")
                    if cand.getchannel("A").getbbox():
                        img = cand
                        break
                except Exception:
                    continue
        if img is not None:
            return img
        # 兜底：ICO 取帧全失败时重画同款图案，绝不让托盘变空白
        s = 256
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((5, 5, s-6, s-6), radius=56, fill=(37, 99, 235, 255))
        for x0, top, bot in ((21, 50, 82), (43, 28, 82), (65, 40, 82)):
            d.rounded_rectangle((int(x0*s/100), int(top*s/100),
                                 int((x0+13)*s/100), int(bot*s/100)),
                                radius=int(3*s/100), fill=(255, 255, 255, 255))
        return img
    except Exception:
        return None


def build_tray(config, stop):
    try:
        import pystray
        from pystray import Menu, MenuItem
    except Exception as exc:
        log("托盘不可用：%s" % exc)
        return None

    def show(_icon, _item):
        spawn_ui()

    def toggle_enabled(_icon, _item):
        config.set("enabled", not config.get("enabled", True))

    def toggle_mute(_icon, _item):
        config.set("mute_enabled", not config.get("mute_enabled", True))

    def toggle_rdp(_icon, _item):
        config.set("rdp_takeover", not config.get("rdp_takeover", False))

    def quit_app(_icon, _item):
        ipc.set_event(ipc.EV_UI_QUIT)
        stop.set()

    menu = Menu(
        MenuItem("打开主界面", show, default=True),
        Menu.SEPARATOR,
        MenuItem("响度均衡", toggle_enabled,
                 checked=lambda i: bool(config.get("enabled", True))),
        MenuItem("自动静音", toggle_mute,
                 checked=lambda i: bool(config.get("mute_enabled", True))),
        MenuItem("远程桌面中也接管", toggle_rdp,
                 checked=lambda i: bool(config.get("rdp_takeover", False))),
        Menu.SEPARATOR,
        MenuItem("退出", quit_app),
    )
    # 换 ID 强制 Windows 重新注册托盘图标：旧 ID 曾被缓存成 pystray 默认羽毛图标
    # （早期版本图标取到空帧时 pystray 用了默认图，Windows 按 ID 缓存了下来）。
    return pystray.Icon("AudioBalancer_v2", make_icon_image(), "音频均衡器", menu)


def watch_show_event(stop):
    """双击 exe / 托盘打开：核心已存在时拉起界面。"""
    h = ipc.create_event(ipc.EV_SHOW_UI)
    if not h:
        # 句柄创建失败时 WaitForSingleObject(0, ...) 会立刻返回 WAIT_FAILED，
        # 这个循环会变成 100% 占一个核的空转，日志里还什么线索都没有
        log("无法创建「显示界面」事件，双击 exe 拉起界面的功能不可用")
        return
    k32 = ctypes.windll.kernel32
    while not stop.is_set():
        rc = k32.WaitForSingleObject(h, 200)
        if rc == 0:
            spawn_ui()
        elif rc != 0x102:      # WAIT_TIMEOUT 之外的异常状态：别空转
            stop.wait(1.0)


PHASE = {"name": "idle", "since": time.time()}


def set_phase(name):
    PHASE["name"] = name
    PHASE["since"] = time.time()


def control_loop(config, engine, muter, guard, stop, icon=None):
    """调度自动静音 + 设备守护；顺带写状态文件、监视配置、监控引擎异常。"""
    ensure_com()
    last_err = None
    last_guard = 0.0
    last_status = 0.0
    last_devices = 0.0
    last_payload = None
    last_write = 0.0
    last_paused = None
    rdp_restored = False
    devices = {"inputs": [], "outputs": []}
    try:
        mtime = os.path.getmtime(config.path)
    except Exception:
        mtime = 0.0
    keys = ("input_device", "output_device", "block_ms", "use_windows_volume",
            "downmix", "rdp_takeover")
    rescan_evt = ipc.create_event(ipc.EV_RESCAN)     # 界面点「重新扫描」用
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k32.WaitForSingleObject.restype = ctypes.c_uint
    while not stop.is_set():
        try:
            # 1) 设备守护优先：换默认输出时会话枚举很慢，放后面会拖住它
            if guard is not None:
                now = time.time()
                if now - last_guard >= guard.interval:
                    last_guard = now
                    set_phase("guard.tick")
                    guard.tick()
            # 远程桌面场景（默认）：自动静音也要停手，并把之前压的音量还回去
            if session.blocked(bool(config.get("rdp_takeover", False))):
                if not rdp_restored:
                    rdp_restored = True
                    try:
                        muter.restore()
                    except Exception:
                        pass
                    muter.in_game = False
                    muter.controlled = 0
                    muter.muted = []
                    log("远程桌面场景：不接管声音，自动静音已暂停")
            else:
                if rdp_restored:
                    rdp_restored = False
                    log("回到本机会话：恢复接管声音")
                set_phase("muter.tick")
                muter.tick(time.time())
            set_phase("idle")

            err = engine.status.get("error")
            if err != last_err:
                last_err = err
                if err:
                    log("引擎异常：%s" % err)

            # 2) 配置热重载：UI 进程改了 config.json，核心必须跟上
            try:
                new_mtime = os.path.getmtime(config.path)
            except Exception:
                new_mtime = mtime
            if new_mtime != mtime:
                before = tuple(config.get(k) for k in keys)
                config.load()
                after = tuple(config.get(k) for k in keys)
                mtime = new_mtime
                if before != after:
                    log("配置变更（设备/缓冲/音量），重建音频链路")
                    engine.request_reload()

            # 3) 设备列表给 UI 用（UI 进程不再自己初始化音频 COM）
            now = time.time()
            if rescan_evt and k32.WaitForSingleObject(rescan_evt, 0) == 0:
                last_devices = 0.0           # 界面要求立刻重扫
            if now - last_devices > 15.0:
                last_devices = now
                ins, outs = engine.list_devices()      # 一次枚举，别枚举两遍
                if ins != devices["inputs"] or outs != devices["outputs"]:
                    devices = {"inputs": ins, "outputs": outs}

            # 4) 状态文件：UI 进程只读它
            if now - last_status >= 0.2:
                last_status = now
                st = dict(engine.status)
                st.update(devices)
                st.update({
                    "mute_in_game": bool(muter.in_game),
                    "foreground": muter.foreground or "",
                    "controlled": int(muter.controlled or 0),
                    "muted": list(muter.muted or []),
                    "guard": guard.status() if guard else {},
                })
                # 内容没变就别写盘：静音时每秒 5 次「建临时文件 + 原子替换」纯属
                # 折腾磁盘。最长 1 秒兜底写一次，界面判活（>6s 认为核心没了）依旧安全。
                if st != last_payload or now - last_write >= 1.0:
                    last_payload = dict(st)
                    last_write = now
                    st["ts"] = now
                    ipc.write_status(st)
                # 托盘提示跟着「远程桌面未接管」状态变，鼠标停上去就能看出来
                paused = st.get("paused")
                if icon is not None and paused != last_paused:
                    last_paused = paused
                    try:
                        icon.title = ("音频均衡器 · 远程桌面未接管"
                                      if paused == "rdp" else "音频均衡器")
                    except Exception:
                        pass
        except Exception:
            log("控制循环异常：\n" + traceback.format_exc())
        stop.wait(0.3)

    # 退出：在本线程还原音量。主线程调 restore() 会跨线程碰 STA 的会话对象
    # （RPC_E_WRONG_THREAD 被 except 吞掉），上一轮 SetMasterVolume(0) 就残留在系统里。
    try:
        muter.restore()
    except Exception:
        pass
    muter.in_game = False
    muter.controlled = 0
    muter.muted = []


def watchdog(guard, stop):
    """盯住控制线程：卡在哪一步、卡了多久，直接写进日志。"""
    last = time.time()
    while not stop.is_set():
        stop.wait(5)
        if stop.is_set():
            return
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


def main():
    theme.set_dpi_aware()
    if "--ui" in sys.argv:
        from ui_main import run_ui
        run_ui()
        return

    config = Config()
    if not ipc.acquire_mutex(ipc.MUTEX_CORE):
        # 已有核心进程：叫它把界面拉出来，绝不再起一个引擎/托盘
        if session.blocked(bool(config.get("rdp_takeover", False))):
            # 远程桌面里默认不接管：不去别人（本机控制台）的会话里弹窗口
            log("远程桌面场景：已有实例在运行，本会话按设置不接管（退出）")
        else:
            ipc.set_event(ipc.EV_SHOW_UI)
        return

    log("===== 核心启动 =====")
    log("运行在%s" % session.describe())
    engine = Engine(config)
    muter = Muter(config)
    guard = DeviceGuard(config, engine, on_event=log, log=log)
    stop = threading.Event()

    icon = build_tray(config, stop)
    if icon is not None:
        threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=watch_show_event, args=(stop,), daemon=True).start()
    ctl = threading.Thread(target=control_loop,
                           args=(config, engine, muter, guard, stop, icon), daemon=True)
    ctl.start()
    threading.Thread(target=watchdog, args=(guard, stop), daemon=True).start()

    ensure_com()
    engine.start()

    # 双击 exe 默认把界面带出来；开机自启（--minimized）只跑核心
    if "--minimized" not in sys.argv and not config.get("start_minimized", False):
        spawn_ui()

    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        log("===== 核心退出 =====")
        ipc.set_event(ipc.EV_UI_QUIT)
        stop.set()
        try:
            ctl.join(timeout=3.0)       # 等控制线程把音量还回去再退出
        except Exception:
            pass
        try:
            guard.stop()
        except Exception:
            pass
        try:
            muter.restore()             # 兜底：控制线程没跑到收尾时
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
        log("核心启动失败：\n" + traceback.format_exc())
        sys.exit(1)
