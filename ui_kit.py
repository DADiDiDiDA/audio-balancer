"""自绘控件：形状用 PIL 画成抗锯齿图片贴到 Canvas，文字用 Canvas 原生文本。

为什么不用 ttk：ttk 在 Windows 上只能用系统主题（vista），深色/圆角/亚克力
一律做不到；自绘才能圆角不锯齿、hover 变色、滑块随手拖动。
PIL 生成 4 倍图再缩回来做超采样，圆角才不会有毛刺。
"""
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from theme import FONT, mix

_SS = 4                       # 超采样倍数
_cache = {}


def reset_cache():
    _cache.clear()


def _render(w, h, r, fill, outline, ow):
    W, H = max(1, w) * _SS, max(1, h) * _SS
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    R = max(0.0, min(float(r) * _SS, W / 2.0, H / 2.0))
    box = [0, 0, W - 1, H - 1]
    if outline:
        d.rounded_rectangle(box, radius=R, fill=fill, outline=outline,
                            width=max(1, int(round(ow * _SS))))
    else:
        d.rounded_rectangle(box, radius=R, fill=fill)
    return img.resize((max(1, w), max(1, h)), Image.LANCZOS)


def photo(w, h, r, fill, outline=None, ow=1.0):
    """取（并缓存）一张圆角矩形图片。"""
    key = (int(w), int(h), round(float(r), 1), fill, outline, round(ow, 2))
    img = _cache.get(key)
    if img is None:
        if len(_cache) > 600:
            _cache.clear()
        img = ImageTk.PhotoImage(_render(int(w), int(h), r, fill, outline, ow))
        _cache[key] = img
    return img


def shape(cv, x, y, w, h, r=0, fill=None, outline=None, ow=1.0):
    """在 (x,y) 贴一块圆角矩形，返回 item id。"""
    w, h = int(round(w)), int(round(h))
    if w <= 0 or h <= 0 or fill is None:
        return None
    return cv.create_image(int(round(x)), int(round(y)),
                           image=photo(w, h, r, fill, outline, ow), anchor="nw")


_width_cache = {}


def text_width(s, size=10, bold=False):
    """量一段文字的像素宽度（排「标签 + 小图标」时要用）。"""
    key = (s, size, bold)
    v = _width_cache.get(key)
    if v is None:
        try:
            from tkinter import font as tkfont
            v = tkfont.Font(font=(FONT, size, "bold") if bold else (FONT, size)).measure(s)
        except Exception:
            v = len(s) * size
        _width_cache[key] = v
    return v


# ---------------------------------------------------------------- 控件
class Toggle:
    """开关：0/1，120ms 滑动动画。"""

    def __init__(self, cv, hits, x, y, get, on_toggle, pal, w=46, h=26):
        self.cv, self.pal = cv, pal
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)
        self.get, self.on_toggle = get, on_toggle
        self.on = bool(get())
        self.pos = 1.0 if self.on else 0.0
        self.r = self.h / 2.0 - 3
        self.img_on = photo(self.w, self.h, self.h / 2.0, pal["accent"])
        self.img_off = photo(self.w, self.h, self.h / 2.0, pal["track"])
        self.track = cv.create_image(self.x, self.y,
                                     image=self.img_on if self.on else self.img_off,
                                     anchor="nw")
        self.knob = shape(cv, self._kx() - self.r, self.y + self.h / 2.0 - self.r,
                          2 * self.r, 2 * self.r, self.r, fill="#ffffff")
        hits.append({"box": (self.x - 6, self.y - 6, self.x + self.w + 6, self.y + self.h + 6),
                     "press": self._click, "cursor": "hand2"})
        self._job = None

    def _kx(self):
        pad = self.h / 2.0
        return self.x + pad + (self.w - self.h) * self.pos

    def _click(self, *_):
        self.on = not self.on
        try:
            self.on_toggle(self.on)
        except Exception:
            pass
        self.cv.itemconfigure(self.track, image=self.img_on if self.on else self.img_off)
        self._start()
        return True

    def _start(self):
        if self._job:
            try:
                self.cv.after_cancel(self._job)
            except Exception:
                pass
        self._step()

    def _step(self):
        target = 1.0 if self.on else 0.0
        gap = target - self.pos
        if abs(gap) <= 0.12:
            self.pos = target
        else:
            self.pos += gap * 0.45
        try:
            self.cv.coords(self.knob, self._kx() - self.r, self.y + self.h / 2.0 - self.r)
        except Exception:
            return
        self._job = self.cv.after(16, self._step) if self.pos != target else None


class Slider:
    """滑块：拖 / 点都跟着走，值按 step 吸附。"""

    def __init__(self, cv, hits, x, y, w, lo, hi, step, value, on_change, pal,
                 disabled=False, fmt=None):
        self.cv, self.pal = cv, pal
        self.x, self.y, self.w = float(x), float(y), float(w)
        self.lo, self.hi = float(lo), float(hi)
        self.step = float(step) if step else 0.1
        self.on_change = on_change
        self.fmt = fmt or (lambda v: "%g" % v)
        self.disabled = bool(disabled)
        self.value = self._snap(float(value))
        col_bg = pal["line"] if self.disabled else pal["track"]
        self.track = cv.create_rectangle(self.x, self.y - 2, self.x + self.w, self.y + 2,
                                         fill=col_bg, outline="")
        self.fill = cv.create_rectangle(self.x, self.y - 2, self.x, self.y + 2,
                                        fill=pal["dim"] if self.disabled else pal["accent"],
                                        outline="")
        self.r = 8
        self.knob = shape(cv, 0, self.y - self.r, self.r * 2, self.r * 2, self.r,
                          fill=pal["card2"] if self.disabled else "#ffffff",
                          outline=pal["line"] if self.disabled else None)
        self._draw()
        if not self.disabled:
            hits.append({"box": (self.x - 10, self.y - 16, self.x + self.w + 10, self.y + 16),
                         "press": self._press, "drag": self._drag, "cursor": "hand2"})

    def _snap(self, v):
        v = min(max(v, self.lo), self.hi)
        if self.step > 0:
            v = round((v - self.lo) / self.step) * self.step + self.lo
        return round(min(max(v, self.lo), self.hi), 4)

    def _draw(self):
        span = (self.hi - self.lo) or 1.0
        ratio = (self.value - self.lo) / span
        cx = self.x + self.w * ratio
        try:
            self.cv.coords(self.fill, self.x, self.y - 2, cx, self.y + 2)
            self.cv.coords(self.knob, cx - self.r, self.y - self.r)
        except Exception:
            pass

    def set_value(self, v, notify=False):
        self.value = self._snap(float(v))
        self._draw()
        if notify:
            self.on_change(self.value)

    def _where(self, x):
        span = (self.hi - self.lo) or 1.0
        return self._snap(self.lo + (x - self.x) / max(self.w, 1.0) * span)

    def _press(self, x, y=None):
        v = self._where(x)
        if abs(v - self.value) > 1e-9:
            self.value = v
            self._draw()
            self.on_change(v)
        return True

    def _drag(self, x, y=None):
        self._press(x)


class Segment:
    """分段选择：一排选项，选中项是滑块。"""

    def __init__(self, cv, hits, x, y, w, h, options, index, on_pick, pal):
        self.cv, self.pal = cv, pal
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)
        self.n = max(1, len(options))
        self.on_pick = on_pick
        self.index = int(index)
        shape(cv, self.x, self.y, self.w, self.h, self.h / 2.0,
              fill=pal["card2"], outline=pal["line"])
        self.slot = (self.w - 6) / self.n
        self.sel = shape(cv, 0, self.y + 3, self.slot, self.h - 6, (self.h - 6) / 2.0,
                         fill=pal["accent"])
        self.labels = []
        for i, text in enumerate(options):
            cx = self.x + 3 + self.slot * (i + 0.5)
            self.labels.append(cv.create_text(
                cx, self.y + self.h / 2.0, text=text, font=(FONT, 10),
                fill=pal["on_accent"] if i == self.index else pal["sub"]))
        self._place()
        hits.append({"box": (self.x, self.y, self.x + self.w, self.y + self.h),
                     "press": self._press, "cursor": "hand2"})

    def _place(self):
        try:
            self.cv.coords(self.sel, self.x + 3 + self.slot * self.index, self.y + 3)
            for i, item in enumerate(self.labels):
                self.cv.itemconfigure(
                    item, fill=self.pal["on_accent"] if i == self.index else self.pal["sub"])
        except Exception:
            pass

    def _press(self, x, *_):
        i = int((x - self.x - 3) // max(self.slot, 1))
        i = max(0, min(self.n - 1, i))
        changed = i != self.index
        self.index = i
        if changed:
            self._place()
        # 即便点的是当前已选中的那一项也要回调：预设被数值反推显示成"已选中"、
        # 用户手动改过参数后点回来想恢复原值，不回调就永远点不动。
        self.on_pick(i)
        return True


class Button:
    def __init__(self, cv, hits, x, y, w, h, text, on_click, pal,
                 kind="primary", size=10):
        self.cv, self.pal = cv, pal
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)
        self.kind, self.on_click = kind, on_click
        self.size = size
        self._colors(kind)
        self.img = shape(cv, self.x, self.y, self.w, self.h, min(10, self.h / 2.0),
                         fill=self.bg, outline=self.line)
        self.label = cv.create_text(self.x + self.w / 2.0, self.y + self.h / 2.0, text=text,
                                    font=(FONT, size), fill=self.fg)
        hits.append({"box": (self.x, self.y, self.x + self.w, self.y + self.h),
                     "press": self._press, "cursor": "hand2", "widget": self})

    def _colors(self, kind):
        pal = self.pal
        r = min(10, self.h / 2.0)
        if kind == "primary":
            self.bg, self.bg_hi = pal["accent"], mix(pal["accent"], "#ffffff", 0.16)
            self.fg, self.line = pal["on_accent"], None
        else:
            self.bg, self.bg_hi = pal["card2"], pal["line"]
            self.fg, self.line = pal["text"], pal["line"]
        self.r = r
        self.img_bg = photo(int(self.w), int(self.h), r, self.bg, self.line)
        self.img_hi = photo(int(self.w), int(self.h), r, self.bg_hi, self.line)

    def set_kind(self, kind):
        """primary = 有未保存改动；ghost = 已保存（样式退到次要，但仍可点）。"""
        if kind == self.kind:
            return
        self.kind = kind
        self._colors(kind)
        try:
            self.cv.itemconfigure(self.img, image=self.img_bg)
            self.cv.itemconfigure(self.label, fill=self.fg)
        except Exception:
            pass

    def _press(self, *_):
        try:
            self.on_click()
        except Exception:
            pass
        return True

    def hover(self, on):
        try:
            self.cv.itemconfigure(self.img, image=self.img_hi if on else self.img_bg)
        except Exception:
            pass

    def set_text(self, text, fg=None):
        # 文字没变就别 configure：Tk 每次 itemconfigure 都会标记重绘，
        # 界面每秒 4~5 次空更新是空闲时白白吃掉 CPU 的元凶之一
        if text == getattr(self, "_text", None) and fg == getattr(self, "_fg", None):
            return
        self._text, self._fg = text, fg
        try:
            self.cv.itemconfigure(self.label, text=text)
            if fg:
                self.cv.itemconfigure(self.label, fill=fg)
        except Exception:
            pass


# ---------------------------------------------------------------- 画笔
class Painter:
    """一页画布上的排版助手：卡片、文字、字段、输入框、列表。"""

    def __init__(self, cv, pal):
        self.cv = cv
        self.pal = pal
        self.hits = []
        self.widgets = []

    # ---- 基础 ----
    def text(self, x, y, s, size=10, color=None, bold=False, anchor="w",
             width=None, justify="left"):
        f = (FONT, size, "bold") if bold else (FONT, size)
        kw = {"width": width, "justify": justify} if width else {}
        return self.cv.create_text(x, y, text=s, font=f, anchor=anchor,
                                   fill=color or self.pal["text"], **kw)

    def card(self, x, y, w, h, r=16, fill=None, outline=None):
        return shape(self.cv, x, y, w, h, r,
                     fill=fill or self.pal["card"], outline=outline or self.pal["line"])

    def section(self, x, y, title, note=None, right=None, tip=None):
        self.text(x, y, title, 11, bold=True)
        if tip:
            self.help(x + text_width(title, 11, True) + 12, y, tip)
        if note:
            if right:
                self.text(right, y, note, 9, self.pal["dim"], anchor="e")
            else:
                self.text(x, y + 19, note, 9, self.pal["sub"])

    def divider(self, x, y, w):
        return self.cv.create_line(x, y, x + w, y, fill=self.pal["line"])

    def help(self, x, y, text, r=7.0):
        """一个「!」小圆点：鼠标移上去由窗口画说明气泡。

        画在这里而不是用 ttk 的 ToolTip，是因为整页都是 Canvas 自绘，
        只有把热点一起注册进 hits，命中判定才和视觉一致。
        """
        pal = self.pal
        shape(self.cv, x - r, y - r, r * 2, r * 2, r,
              fill=pal["card2"], outline=pal["line"])
        self.cv.create_text(x, y + 0.5, text="!", font=(FONT, 8, "bold"),
                            fill=pal["sub"])
        self.hits.append({"box": (x - r - 2, y - r - 2, x + r + 2, y + r + 2),
                          "tip": text, "cursor": ""})

    # ---- 控件 ----
    def toggle(self, x, y, get, on_toggle, w=46, h=26):
        t = Toggle(self.cv, self.hits, x, y, get, on_toggle, self.pal, w=w, h=h)
        self.widgets.append(t)
        return t

    def slider(self, x, y, w, lo, hi, step, value, on_change, disabled=False, fmt=None):
        s = Slider(self.cv, self.hits, x, y, w, lo, hi, step, value, on_change,
                   self.pal, disabled=disabled, fmt=fmt)
        self.widgets.append(s)
        return s

    def segment(self, x, y, w, h, options, index, on_pick):
        s = Segment(self.cv, self.hits, x, y, w, h, options, index, on_pick, self.pal)
        self.widgets.append(s)
        return s

    def button(self, x, y, w, h, text, on_click, kind="primary", size=10):
        b = Button(self.cv, self.hits, x, y, w, h, text, on_click, self.pal,
                   kind=kind, size=size)
        self.widgets.append(b)
        return b

    def field(self, x, y, w, label, value, lo, hi, step, on_change,
              fmt=None, note=None, disabled=False, tip=None):
        """一行字段：标题一行、滑块一行、数值靠右。返回 (滑块, 数值文本 item)。"""
        fmt = fmt or (lambda v: "%g" % v)
        self.text(x, y, label, 10, self.pal["dim"] if disabled else self.pal["sub"])
        if tip:
            self.help(x + text_width(label, 10) + 12, y, tip)
        if note:
            self.text(x + w, y, note, 9, self.pal["dim"], anchor="e")
        # 数值文字最宽约 80px（如 "-17.5 LUFS" / "-1.0 dBFS"），
        # 轨道必须提前收手，否则滑块推到最右时旋钮会压在数字上
        sl = self.slider(x, y + 22, max(40, w - 92), lo, hi, step, value, on_change,
                         disabled=disabled, fmt=fmt)
        val = self.text(x + w, y + 22, fmt(value), 10,
                        self.pal["dim"] if disabled else self.pal["text"], anchor="e")

        def show(v):
            try:
                self.cv.itemconfigure(val, text=fmt(v))
            except Exception:
                pass
        sl.value_label = val
        sl.show = show
        if on_change is None:
            sl.on_change = lambda v: None
        real = sl.on_change
        sl.on_change = lambda v, _r=real, _s=show: (_r(v), _s(v))
        return sl

    def _track(self, widget):
        """登记 create_window 里的真实控件：canvas.delete("all") 只删画布图元，
        不会销毁这些 Tk 控件，不主动 destroy 就会在每次重绘后永久留在 Tcl 里。"""
        lst = getattr(self.cv, "_embedded", None)
        if lst is None:
            lst = []
            try:
                self.cv._embedded = lst
            except Exception:
                return
        lst.append(widget)

    def entry(self, x, y, w, h, var, on_commit=None, show=None):
        shape(self.cv, x, y, w, h, 9, fill=self.pal["card2"], outline=self.pal["line"])
        e = tk.Entry(self.cv, textvariable=var, bd=0, relief="flat",
                     bg=self.pal["card2"], fg=self.pal["text"],
                     insertbackground=self.pal["text"], font=(FONT, 10),
                     highlightthickness=0, justify="left")
        if show:
            e.configure(show=show)
        self.cv.create_window(x + 12, y + h / 2.0, window=e, anchor="w",
                              width=int(w - 24), height=int(h - 8))

        def commit(*_):
            if on_commit:
                on_commit()
        e.bind("<Return>", commit)
        e.bind("<FocusOut>", commit)
        self._track(e)
        return e

    def listbox(self, x, y, w, h, font_size=10):
        lb = tk.Listbox(self.cv, bd=0, relief="flat", highlightthickness=0,
                        bg=self.pal["card2"], fg=self.pal["text"],
                        selectbackground=self.pal["accent"],
                        selectforeground=self.pal["on_accent"],
                        activestyle="none", font=(FONT, font_size),
                        exportselection=False)
        self.cv.create_window(x, y, window=lb, anchor="nw", width=int(w), height=int(h))
        self._track(lb)
        return lb
