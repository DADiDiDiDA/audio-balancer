"""主题：亚克力窗口 + 跟随系统深色/浅色。

亚克力不是一个开关，是三层叠出来的：
  1) DWM 给窗口挂上 acrylic 背板（Win11: DWMWA_SYSTEMBACKDROP_TYPE=3；
     Win10 退回 SetWindowCompositionAttribute 的 ACCENT_ENABLE_ACRYLICBLURBEHIND）；
  2) 把整个窗口设成轻微半透明 —— 只做第 1 步的话 Tk 会把客户区涂成不透明，
     模糊层完全露不出来（这是网上很多「tk 亚克力」教程失效的原因）；
  3) 标题栏也切到深色，否则浅色标题栏配深色界面特别割裂。

注意：半透明是整窗叠加（-alpha），所以控件颜色要先按自己的底色画好，
不能指望 Tk 支持「半透明控件」。
"""
import ctypes
import sys

FONT = "Microsoft YaHei UI"

DARK = {
    "bg": "#191c22",
    "card": "#23272f",
    "card2": "#2b303a",
    "line": "#333945",
    "text": "#eef1f7",
    "sub": "#9aa3b2",
    "dim": "#6f7887",
    "accent": "#4d8dff",
    "accent_dim": "#3a5b96",
    "on_accent": "#ffffff",
    "track": "#3a414e",
    "ok": "#3ecf8e",
    "warn": "#f5a524",
    "err": "#ff6b6b",
    "sidebar": "#1d2027",
}

LIGHT = {
    "bg": "#eef1f7",
    "card": "#ffffff",
    "card2": "#f2f4f9",
    "line": "#e3e7ef",
    "text": "#1a1e26",
    "sub": "#6b7382",
    "dim": "#98a0ae",
    "accent": "#2f6bff",
    "accent_dim": "#b9cdff",
    "on_accent": "#ffffff",
    "track": "#dfe4ed",
    "ok": "#0f9159",
    "warn": "#b45309",
    "err": "#d02f2f",
    "sidebar": "#e6eaf2",
}

_cache = {}


def palette(dark=None):
    if dark is None:
        dark = system_dark()
    return DARK if dark else LIGHT


def system_dark():
    """系统「应用模式」是不是深色。读不到就当深色（本程序主打深色）。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        winreg.CloseKey(k)
        return int(v) == 0
    except Exception:
        return True


# ---------- 亚克力 ----------
class _MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                ("t", ctypes.c_int), ("b", ctypes.c_int)]


class _ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WCADATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


def _hwnd(root):
    """Tk 的顶层 HWND：winfo_id() 拿到的是客户区子窗口，父窗口才带标题栏。

    必须显式声明 restype=c_void_p：64 位下 HWND 是 64 位，用默认 c_int
    接会被截断，DwmSetWindowAttribute 就静默失败（标题栏一直是浅色）。
    """
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = [ctypes.c_void_p]
    h = user32.GetParent(ctypes.c_void_p(int(root.winfo_id())))
    return int(h) if h else int(root.winfo_id())


def _hex_to_bgr(color, alpha=0xCC):
    c = color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return (alpha << 24) | (b << 16) | (g << 8) | r


def apply_acrylic(root, dark, alpha=0.94):
    """给窗口挂亚克力背板 + 深色标题栏。失败就静默降级（不影响功能）。"""
    try:
        hwnd = _hwnd(root)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]
        build = getattr(sys.getwindowsversion(), "build", 0)
        # 深色标题栏（Win10 1809+ / Win11 都是 20）
        v = ctypes.c_int(1 if dark else 0)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(v), 4)
        # 标题栏背景色匹配内容区：浅色模式下光靠 attribute 20 会给系统灰，
        # 和内容区的浅蓝白不搭；用 DWMWA_CAPTION_COLOR(35) 显式指定颜色。
        if build >= 22000:
            try:
                pal = palette(dark)
                hexc = pal["bg"].lstrip("#")
                r, g, b = int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)
                color = ctypes.c_uint((b << 16) | (g << 8) | r)   # DWM 用 COLORREF (BGR)
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 35,
                                          ctypes.byref(color), 4)
                # 标题栏文字：深色主题用浅字，浅色主题用深字（DWMWA_TEXT_COLOR=36）
                t = pal["text"].lstrip("#")
                tr, tg, tb = int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
                tcol = ctypes.c_uint((tb << 16) | (tg << 8) | tr)
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 36,
                                          ctypes.byref(tcol), 4)
            except Exception:
                pass
        if build >= 22000:
            bd = ctypes.c_int(3)      # 3 = DWMSBT_TRANSIENTWINDOW（亚克力）
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 38, ctypes.byref(bd), 4)
        else:
            _win10_blur(hwnd, dark)
        root.attributes("-alpha", float(alpha))
    except Exception:
        try:
            root.attributes("-alpha", 1.0)
        except Exception:
            pass


def _win10_blur(hwnd, dark):
    try:
        user32 = ctypes.windll.user32
        policy = _ACCENT_POLICY()
        policy.AccentState = 4                    # ACCENT_ENABLE_ACRYLICBLURBEHIND
        policy.AccentFlags = 2
        policy.GradientColor = _hex_to_bgr("#202020" if dark else "#f0f2f7", 0x99)
        data = _WCADATA()
        data.Attribute = 19                       # WCA_ACCENT_POLICY
        data.SizeOfData = ctypes.sizeof(policy)
        data.Data = ctypes.cast(ctypes.byref(policy), ctypes.c_void_p)
        user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except Exception:
        pass


def set_dpi_aware():
    """不做 DPI 感知的话，125% 缩放下整个界面会被系统拉糊。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------- 颜色工具 ----------
def _rgb(color):
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(c1, c2, t):
    """在两色之间插值（t=0 返回 c1）。用于 hover / 动画中间色。"""
    t = max(0.0, min(1.0, float(t)))
    r1, g1, b1 = _rgb(c1)
    r2, g2, b2 = _rgb(c2)
    return "#%02x%02x%02x" % (int(r1 + (r2 - r1) * t),
                              int(g1 + (g2 - g1) * t),
                              int(b1 + (b2 - b1) * t))


def alpha_on(color, bg, a):
    """把 color 以 a 的不透明度压到 bg 上（模拟半透明色块）。"""
    return mix(bg, color, a)
