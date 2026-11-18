"""主界面：左侧导航 + 四页，全自绘，亚克力背景，跟随系统深色。

界面不依赖 ttk 主题（只有下拉框用 ttk）：卡片、开关、滑块、按钮都由
ui_kit 用 PIL 形状 + Canvas 文本画出来，所以圆角抗锯齿、hover 会变色、
深浅色切换只要换一套调色板。

刷新策略：页面内容只在切页 / 改窗口大小 / 改动后重建；实时数值（响度、
增益、状态、保存状态）只改文字和坐标，不重建 —— 否则拖滑块会被打断。
"""
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageFilter, ImageGrab, ImageTk

import theme
import ui_kit
from theme import FONT

APP_TITLE = "音频均衡器"
VERSION = "1.4"

SIDE_W = 192

NAV = [("loudness", "响度均衡"), ("mute", "自动静音"),
       ("device", "音频设备"), ("about", "关于")]

METER_LO, METER_HI = -45.0, -5.0


def _fmt_db(v):
    return "关闭" if v <= -39.5 else "%+.1f dB" % v


def _fmt_lufs(v):
    return "静音" if v < -60 else "%.1f" % v


class AppWindow:
    def __init__(self, root, config, engine, muter, cmd_queue, guard=None,
                 start_hidden=False):
        self.root = root
        self.config = config
        self.engine = engine
        self.muter = muter
        self.cmds = cmd_queue
        self.guard = guard

        self.dark = theme.system_dark()
        self.pal = theme.palette(self.dark)
        self.page = "loudness"
        self._hits = []
        self._side_hits = []
        self._active = None
        self._hover = None
        self._cursor = None
        self._painter = None
        self._last_change = 0.0
        self._resize_job = None
        self._last_size = (0, 0)
        self._btn_save = None
        self.meter = None
        self.m_band = None
        self._glass_raw = None
        self._img_body = None
        self._img_side = None

        root.title(APP_TITLE)
        root.geometry(config.get("window_size", "940x760"))
        root.minsize(880, 660)
        root.configure(bg=self.pal["bg"])
        theme.apply_acrylic(root, self.dark, alpha=1.0)
        self._style_ttk()

        # 先藏起来：亚克力底要抓「窗口背后的桌面」，窗口自己在屏幕上就抓到自己了
        root.withdraw()
        self.side = tk.Canvas(root, bg=self.pal["sidebar"], bd=0,
                              highlightthickness=0, width=SIDE_W)
        self.side.pack(side="left", fill="y")
        self.body = tk.Canvas(root, bg=self.pal["bg"], bd=0, highlightthickness=0)
        self.body.pack(side="left", fill="both", expand=True)

        self.var_input = tk.StringVar(value=config.get("input_device", "") or "")
        self.var_output = tk.StringVar(value=config.get("output_device", "") or "")
        self.var_newgame = tk.StringVar()
        self._inputs, self._outputs = [], []
        self._rescan(silent=True)

        self._bind()
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.after(50, self.render)
        self._poll()
        self.root.update_idletasks()
        self._capture_glass()
        if not start_hidden:
            root.deiconify()
        # 标题栏深浅色必须在窗口真正映射之后设，否则 DwmSetWindowAttribute 无效
        self.root.after(80, self._apply_frame)

    # ---------------------------------------------------------------- 外观
    def _style_ttk(self):
        pal = self.pal
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Acrylic.TCombobox", fieldbackground=pal["card2"],
                     background=pal["card2"], foreground=pal["text"],
                     arrowcolor=pal["sub"], bordercolor=pal["line"],
                     lightcolor=pal["card2"], darkcolor=pal["card2"],
                     selectbackground=pal["card2"], selectforeground=pal["text"],
                     padding=6, relief="flat")
        st.map("Acrylic.TCombobox",
               fieldbackground=[("readonly", pal["card2"])],
               foreground=[("readonly", pal["text"])],
               bordercolor=[("focus", pal["accent"]), ("!focus", pal["line"])])
        self.root.option_add("*TCombobox*Listbox.background", pal["card2"])
        self.root.option_add("*TCombobox*Listbox.foreground", pal["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", pal["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", pal["on_accent"])
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    def _apply_frame(self):
        try:
            theme.apply_acrylic(self.root, self.dark, alpha=1.0)
        except Exception:
            pass

    def apply_theme(self):
        """系统深色开关变了：换调色板 + 重建整页。"""
        self.dark = theme.system_dark()
        self.pal = theme.palette(self.dark)
        self.root.configure(bg=self.pal["bg"])
        self.side.configure(bg=self.pal["sidebar"])
        self.body.configure(bg=self.pal["bg"])
        self._style_ttk()
        self._apply_frame()
        self.render()

    # ---------------------------------------------------------------- 事件
    def _bind(self):
        c = self.body
        c.bind("<Button-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Motion>", self._motion)
        c.bind("<Configure>", self._on_resize)
        s = self.side
        s.bind("<Button-1>", self._side_press)
        s.bind("<Motion>", self._side_motion)

    @staticmethod
    def _hit_at(hits, x, y):
        for h in reversed(hits):
            x1, y1, x2, y2 = h["box"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return h
        return None

    def _press(self, ev):
        hit = self._hit_at(self._hits, ev.x, ev.y)
        self._active = hit
        if hit and hit.get("press"):
            hit["press"](ev.x, ev.y)

    def _drag(self, ev):
        hit = self._active
        if hit and hit.get("drag"):
            hit["drag"](ev.x, ev.y)

    def _release(self, ev):
        self._active = None

    def _motion(self, ev):
        hit = self._hit_at(self._hits, ev.x, ev.y)
        widget = hit.get("widget") if hit else None
        if widget is not self._hover:
            if self._hover is not None and hasattr(self._hover, "hover"):
                self._hover.hover(False)
            self._hover = widget
            if widget is not None and hasattr(widget, "hover"):
                widget.hover(True)
        cur = (hit or {}).get("cursor") or ""
        if cur != self._cursor:
            self._cursor = cur
            try:
                self.body.configure(cursor=cur)
            except Exception:
                pass

    def _side_press(self, ev):
        hit = self._hit_at(self._side_hits, ev.x, ev.y)
        if hit and hit.get("press"):
            hit["press"](ev.x, ev.y)

    def _side_motion(self, ev):
        hit = self._hit_at(self._side_hits, ev.x, ev.y)
        try:
            self.side.configure(cursor="hand2" if hit else "")
        except Exception:
            pass

    def _on_resize(self, ev):
        size = (ev.width, ev.height)
        if size == self._last_size:
            return
        self._last_size = size
        if self._resize_job:
            try:
                self.root.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.root.after(90, self._do_resize)

    def _do_resize(self):
        self._resize_job = None
        self.render()

    # ---------------------------------------------------------------- 保存状态
    def _mark_change(self):
        self._last_change = time.time()
        self._refresh_save()

    def _refresh_save(self):
        btn = self._btn_save
        if btn is None:
            return
        if self.config.save_error:
            btn.set_kind("primary")
            btn.set_text("保存失败", self.pal["err"])
        elif self.config.is_dirty() or (time.time() - self._last_change) < (self.config.save_delay + 0.25):
            btn.set_kind("primary")
            btn.set_text("保存中…", self.pal["on_accent"])
        else:
            btn.set_kind("ghost")
            btn.set_text("已保存", self.pal["sub"])

    def _save_now(self):
        self.config.save_now()
        self._last_change = 0.0
        self._refresh_save()

    # ---------------------------------------------------------------- 亚克力底
    def _capture_glass(self):
        """抓窗口背后的桌面，缩到 1/12 做高斯模糊 —— 这就是"毛玻璃"的来源。

        Tk 的窗口拿不到 DWM 的背景模糊（实测条纹背景一条不糊），所以只能用
        抓屏 + 模糊 + 加色调，再把结果铺成窗口底色；好处是文字仍然 100% 不透明，
        不会像直接调 -alpha 那样整屏发灰。
        """
        try:
            self.root.update_idletasks()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w < 100 or h < 100:
                return
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h)).convert("RGB")
            small = img.resize((max(1, w // 12), max(1, h // 12)), Image.BILINEAR)
            self._glass_raw = small.filter(ImageFilter.GaussianBlur(2.4))
        except Exception:
            self._glass_raw = None

    def _glass_full(self, W, H):
        if self._glass_raw is None:
            return None
        try:
            total = SIDE_W + W
            full = self._glass_raw.resize((total, H), Image.BICUBIC)
            tint = (16, 18, 23, 156) if self.dark else (246, 248, 252, 186)
            ov = Image.new("RGBA", full.size, tint)
            return Image.alpha_composite(full.convert("RGBA"), ov).convert("RGB")
        except Exception:
            return None

    def _draw_glass(self, W, H):
        full = self._glass_full(W, H)
        if full is None:
            self._img_body = self._img_side = None
            return
        self._img_body = ImageTk.PhotoImage(full.crop((SIDE_W, 0, SIDE_W + W, H)))
        self._img_side = ImageTk.PhotoImage(full.crop((0, 0, SIDE_W, H)))
        self.body.create_image(0, 0, image=self._img_body, anchor="nw")

    # ---------------------------------------------------------------- 渲染
    def render(self):
        pal = self.pal
        self.body.delete("all")
        ui_kit.reset_cache()
        self._hits = []
        self._hover = None
        self._active = None
        self._btn_save = None

        W = max(self.body.winfo_width(), 640)
        H = max(self.body.winfo_height(), 520)
        pad = 26
        x, w = pad, W - pad * 2

        self._draw_glass(W, H)
        self._render_sidebar(H)

        p = ui_kit.Painter(self.body, pal)
        self._painter = p
        self._render_footer(p, x, w, H)
        getattr(self, "_page_" + self.page)(p, x, 24, w, H - 56)

        self._hits = p.hits
        self._update_live()

    def _render_sidebar(self, H):
        cv = self.side
        cv.delete("all")
        self._side_hits = []
        pal = self.pal
        if self._img_side is not None:
            cv.create_image(0, 0, image=self._img_side, anchor="nw")
        else:
            cv.configure(bg=pal["sidebar"])

        ui_kit.shape(cv, 24, 24, 38, 38, 19, fill=pal["accent"])
        for bx, bh in ((32, 12), (39, 20), (46, 14)):
            ui_kit.shape(cv, bx, 43 - bh, 4, bh, 2, fill=pal["on_accent"])
        cv.create_text(74, 34, text=APP_TITLE, font=(FONT, 12, "bold"),
                       fill=pal["text"], anchor="w")
        cv.create_text(74, 53, text="v" + VERSION, font=(FONT, 9),
                       fill=pal["dim"], anchor="w")

        y = 92
        for key, label in NAV:
            active = (key == self.page)
            if active:
                ui_kit.shape(cv, 14, y, 164, 40, 11, fill=pal["card"])
                ui_kit.shape(cv, 14, y + 10, 3, 20, 1.5, fill=pal["accent"])
            cv.create_text(38, y + 20, text=label, anchor="w",
                           font=(FONT, 11, "bold" if active else "normal"),
                           fill=pal["text"] if active else pal["sub"])
            # 事件分发会传 (x, y)，回调必须能吞掉这两个参数
            self._side_hits.append({"box": (14, y, 178, y + 40),
                                    "press": (lambda _x, _y, k=key: self._goto(k))})
            y += 46

        st = self.engine.status
        running = bool(st.get("running"))
        err = st.get("error")
        color = pal["err"] if (err or not running) else pal["ok"]
        dy = H - 44
        ui_kit.shape(cv, 26, dy - 4, 9, 9, 4.5, fill=color)
        cv.create_text(44, dy, text=("链路中断" if err else ("运行中" if running else "未运行")),
                       anchor="w", font=(FONT, 10), fill=pal["sub"])

    def _render_footer(self, p, x, w, H):
        pal = self.pal
        y = H - 52
        p.cv.create_line(x, y - 10, x + w, y - 10, fill=pal["line"])
        self._foot = p.cv.create_text(x, y + 16, text="", anchor="w",
                                      font=(FONT, 9), fill=pal["sub"], tags="foot")
        self._btn_save = p.button(x + w - 106, y - 3, 106, 34, "已保存",
                                  self._save_now, kind="primary", size=10)
        self._refresh_save()

    def _goto(self, key):
        if key == self.page:
            return
        self.page = key
        self.render()

    # ---------------------------------------------------------------- 页面
    def _page_loudness(self, p, x, y, w, H):
        pal = self.pal
        cfg = self.config
        auto = str(cfg.get("level_mode", "auto")).lower() != "limit"

        p.text(x, y + 6, "响度均衡", 18, bold=True)
        p.text(x, y + 34, "把声音拉进目标范围：偏小的提上来，偏大的压下去", 10, pal["sub"])

        # --- 当前响度 ---
        cy = y + 62
        p.card(x, cy, w, 104)
        p.text(x + 22, cy + 22, "当前响度", 10, pal["sub"])
        self.m_lufs = p.text(x + 22, cy + 56, "—", 22, bold=True)
        p.text(x + 22 + 118, cy + 60, "LUFS", 9, pal["dim"])
        self.m_gain = p.text(x + w - 24, cy + 22, "增益 —", 10, pal["sub"], anchor="e")
        self.m_mode = p.text(x + w - 24, cy + 43, "—", 9, pal["dim"], anchor="e")

        mx0, mx1, my = x + 196, x + w - 24, cy + 56
        self.meter = (mx0, mx1, my)
        ui_kit.shape(p.cv, mx0, my - 3, mx1 - mx0, 6, 3, fill=pal["track"])
        self.m_band = None
        self.m_band_txt = p.text((mx0 + mx1) / 2, my + 22, "", 8, pal["sub"], anchor="center")
        self._draw_band()
        self.m_mark = ui_kit.shape(p.cv, self._mmap(float(self.engine.status.get("lufs", -70))) - 2,
                                   my - 10, 5, 20, 2.5, fill=pal["accent"])
        p.text(mx0, my + 22, "−45", 8, pal["dim"])
        p.text(mx1, my + 22, "−5", 8, pal["dim"], anchor="e")

        # --- 处理方式 ---
        by = cy + 116
        p.card(x, by, w, 214)
        p.section(x + 22, by + 26, "处理方式")
        p.segment(x + 22, by + 50, 236, 34, ["仅限制", "自动电平"],
                  1 if auto else 0, self._on_mode)
        p.text(x + w - 24 - 54, by + 58, "启用均衡", 10, pal["sub"], anchor="e")
        p.toggle(x + w - 24 - 46, by + 54, lambda: bool(cfg.get("enabled", True)),
                 self._on_enabled)
        desc = ("自动电平：低于范围就提上来（最多 +%.0f dB），高于范围就压下去，落在范围内不动，"
                "声音会一直待在目标附近。" % float(cfg.get("max_boost_db", 12))
                if auto else
                "仅限制：只把超过目标的部分压下来，安静的内容原样通过（不会放大底噪）。")
        p.text(x + 22, by + 98, desc, 9, pal["sub"], width=w - 44)

        col = (w - 44 - 26) / 2.0
        p.field(x + 22, by + 150, col, "目标响度", float(cfg.get("target_lufs", -18)),
                -30, -8, 0.5, self._on_target, fmt=lambda v: "%.1f LUFS" % v)
        p.field(x + 22 + col + 26, by + 150, col, "目标范围 ±",
                float(cfg.get("target_range_db", 3)), 0, 8, 0.5, self._on_range,
                fmt=lambda v: "±%.1f dB" % v, disabled=not auto,
                note="" if auto else "仅自动电平")

        # --- 限制与余量 ---
        ly = by + 226
        p.card(x, ly, w, 186)
        p.section(x + 22, ly + 26, "限制与余量")
        p.field(x + 22, ly + 52, col, "最大衰减",
                float(cfg.get("max_attenuation_db", 20)), 0, 36, 1,
                self._on_maxatt, fmt=lambda v: "−%.0f dB" % v)
        p.field(x + 22 + col + 26, ly + 52, col, "最大提升",
                float(cfg.get("max_boost_db", 12)), 0, 24, 1,
                self._on_maxboost, fmt=lambda v: "+%.0f dB" % v,
                disabled=not auto, note="" if auto else "仅自动电平")
        p.field(x + 22, ly + 108, col, "限幅上限",
                float(cfg.get("limiter_ceiling_db", -1)), -6, 0, 0.5,
                self._on_ceiling, fmt=lambda v: "%.1f dBFS" % v)
        p.field(x + 22 + col + 26, ly + 108, col, "处理前降低",
                float(cfg.get("pre_gain_db", 0)), -24, 0, 1,
                self._on_pre, fmt=lambda v: "%+.0f dB" % v)
        p.text(x + 22, ly + 162, "直通：完全不做处理，只把声音原样送到耳机", 9, pal["sub"])
        p.toggle(x + w - 24 - 46, ly + 154,
                 lambda: bool(cfg.get("passthrough", False)), self._on_passthrough)

    def _page_mute(self, p, x, y, w, H):
        pal = self.pal
        cfg = self.config
        p.text(x, y + 6, "自动静音", 18, bold=True)
        p.text(x, y + 34, "按名单判断：谁在挂机、谁该让路", 10, pal["sub"])

        cy = y + 62
        p.card(x, cy, w, 62)
        p.text(x + 22, cy + 22, "启用自动静音", 11, bold=True)
        p.text(x + 22, cy + 43, "关掉后立刻恢复所有程序的原音量", 9, pal["sub"])
        p.toggle(x + w - 24 - 46, cy + 18, lambda: bool(cfg.get("mute_enabled", True)),
                 self._on_mute)

        ry = cy + 74
        p.card(x, ry, w, 146)
        p.section(x + 22, ry + 24, "规则", "两条规则各自独立，只认名单里的程序", right=x + w - 22)
        p.text(x + 22, ry + 54, "挂机静音", 10)
        p.text(x + 22, ry + 74, "名单里的程序切到后台（最小化 / 挂机）就把它自己静音", 9, pal["sub"])
        p.toggle(x + w - 24 - 46, ry + 48,
                 lambda: bool(cfg.get("mute_listed_in_background", True)), self._on_mute_bg)
        p.divider(x + 22, ry + 94, w - 44)
        p.text(x + 22, ry + 112, "游戏时压制其它程序", 10)
        p.text(x + 22, ry + 132, "名单里的程序在前台时，把其它程序压到「压到」的值", 9, pal["sub"])
        p.toggle(x + w - 24 - 46, ry + 106,
                 lambda: bool(cfg.get("mute_others_in_game", False)), self._on_mute_others)

        vy = ry + 158
        p.card(x, vy, w, 142)
        p.section(x + 22, vy + 24, "音量")
        col = (w - 44 - 26) / 2.0
        p.field(x + 22, vy + 50, col, "名单程序在前台时",
                float(cfg.get("mute_fg_volume", 1.0)), 0, 1, 0.05,
                self._on_fg, fmt=lambda v: "%.0f%%" % (v * 100))
        p.field(x + 22 + col + 26, vy + 50, col, "压到",
                float(cfg.get("mute_bg_volume", 0.0)), 0, 1, 0.05,
                self._on_bg, fmt=lambda v: "%.0f%%" % (v * 100))
        p.field(x + 22, vy + 106, col, "渐变时长",
                float(cfg.get("mute_easing_ms", 300)), 100, 2000, 50,
                self._on_easing, fmt=lambda v: "%.0f ms" % v)

        gy = vy + 154
        head, row_h, line_px = 52, 44, 23     # 23px = YaHei 10pt 的实测行高
        list_h = int(H - gy - 8 - head - row_h)
        list_h = max(line_px * 3, (list_h // line_px) * line_px)   # 整行数，避免半行被切
        gh = head + list_h + row_h
        p.card(x, gy, w, gh)
        p.section(x + 22, gy + 26, "名单", "填进程名，如 StarRail.exe（不区分大小写）",
                  right=x + w - 22)
        lb = p.listbox(x + 24, gy + head, w - 48, list_h)
        for g in (cfg.get("mute_games", []) or []):
            lb.insert("end", g)
        self.m_list = lb
        row_y = gy + head + list_h + 8
        p.entry(x + 22, row_y, 202, 32, self.var_newgame, self._add_game)
        p.button(x + 22 + 210, row_y, 62, 32, "添加", self._add_game)
        p.button(x + 22 + 280, row_y, 88, 32, "删除选中", self._del_game, kind="ghost")
        p.button(x + 22 + 376, row_y, 92, 32, "恢复默认", self._reset_games, kind="ghost")

    def _page_device(self, p, x, y, w, H):
        pal = self.pal
        cfg = self.config
        p.text(x, y + 6, "音频设备", 18, bold=True)
        p.text(x, y + 34, "声音从 CABLE 虚拟声卡进来，处理后送到耳机", 10, pal["sub"])

        cy = y + 62
        p.card(x, cy, w, 214)
        p.section(x + 22, cy + 24, "设备", "改完立即重连（约 0.2 秒静音）", right=x + w - 22)

        p.text(x + 22, cy + 70, "监听输入", 10, pal["sub"])
        cb1 = ttk.Combobox(p.cv, textvariable=self.var_input,
                           values=[""] + self._inputs, state="readonly",
                           style="Acrylic.TCombobox", font=(FONT, 10))
        cb1.bind("<<ComboboxSelected>>", lambda e: self._apply_device())
        p.cv.create_window(x + 132, cy + 62, window=cb1, anchor="nw",
                           width=w - 176, height=30)

        p.text(x + 22, cy + 116, "输出设备", 10, pal["sub"])
        cb2 = ttk.Combobox(p.cv, textvariable=self.var_output,
                           values=[""] + self._outputs, state="readonly",
                           style="Acrylic.TCombobox", font=(FONT, 10))
        cb2.bind("<<ComboboxSelected>>", lambda e: self._apply_device())
        p.cv.create_window(x + 132, cy + 108, window=cb2, anchor="nw",
                           width=w - 176, height=30)

        blk = float(cfg.get("block_ms", 20))
        p.field(x + 22, cy + 152, w - 44, "缓冲（越小延迟越低，越小越容易咔哒）",
                blk, 10, 100, 1, self._on_block, fmt=lambda v: "%.0f ms" % v)
        p.text(x + 22, cy + 200,
               "当前端到端延迟约 %.0f ms（块长 ×2 + 设备缓冲）" % (blk * 2 + max(30, blk + 18)),
               8, pal["dim"])

        ty = cy + 226
        p.card(x, ty, w, 144)
        p.text(x + 22, ty + 26, "自动接管系统默认输出", 11, bold=True)
        p.toggle(x + w - 24 - 46, ty + 20, lambda: bool(cfg.get("auto_follow_default", True)),
                 self._on_follow)
        p.text(x + 22, ty + 64,
               "在 Windows 里把默认输出换成耳机，本程序自动把它接成输出设备，"
               "再把系统默认改回虚拟声卡 —— 下次换耳机只要在 Windows 里点一下。",
               9, pal["sub"], width=w - 60)
        self.m_default = p.text(x + 22, ty + 118, "系统默认输出：—", 9, pal["dim"])

        vc = ty + 156
        p.card(x, vc, w, 84)
        p.text(x + 22, vc + 26, "用 Windows 音量滑块控制声音大小", 11, bold=True)
        p.text(x + 22, vc + 60,
               "系统音量里「CABLE In 16ch」的滑块就是最终音量（启动时置 100%）。"
               "回环不受它影响，引擎会主动读它当输出增益。", 9, pal["sub"], width=w - 60)
        p.toggle(x + w - 24 - 46, vc + 22,
                 lambda: bool(cfg.get("use_windows_volume", True)), self._on_winvol)

        by = vc + 96
        p.button(x + 22, by, 96, 34, "重新扫描", self._rescan_click, kind="ghost")
        p.button(x + 126, by, 116, 34, "应用并重连", self._apply_device)
        p.text(x + 22, by + 52,
               "提示：系统默认输出设成「CABLE In 16ch / CABLE Input」，本程序从它采集声音。",
               9, pal["dim"], width=w - 44)

    def _page_about(self, p, x, y, w, H):
        pal = self.pal
        p.text(x, y + 6, "关于", 18, bold=True)
        p.text(x, y + 34, "%s v%s" % (APP_TITLE, VERSION), 10, pal["sub"])

        cy = y + 62
        p.card(x, cy, w, 258)
        p.text(x + 24, cy + 28, "一个程序做两件事", 11, bold=True)
        p.text(x + 24, cy + 60, "响度均衡", 10, pal["accent"], bold=True)
        p.text(x + 24, cy + 80,
               "从 CABLE 虚拟声卡采集正在播放的声音，按 ITU-R BS.1770 测节目响度，"
               "自动电平把它拉进目标范围，再经软限幅送到耳机。", 9, pal["sub"], width=w - 48)
        p.text(x + 24, cy + 130, "自动静音", 10, pal["accent"], bold=True)
        p.text(x + 24, cy + 150,
               "名单里的程序挂机（在后台）时静音它自己；可选地在它前台时让其它程序让路。",
               9, pal["sub"], width=w - 48)
        p.divider(x + 24, cy + 200, w - 48)
        p.text(x + 24, cy + 220,
               "不改系统音量，也不改各程序音量（自动静音例外：退出或关闭开关时按原值恢复）。",
               9, pal["dim"], width=w - 48)

        p.card(x, cy + 270, w, 100)
        p.text(x + 24, cy + 294, "配置文件", 9, pal["dim"])
        p.text(x + 24, cy + 316, (self.config.path or ""), 9, pal["sub"], width=w - 48)
        p.text(x + 24, cy + 344,
               "端到端延迟默认约 %d ms，可在「音频设备」里用缓冲滑块调节"
               % int(float(self.config.get("block_ms", 33)) * 2
                     + max(30, float(self.config.get("block_ms", 33)) + 18)),
               9, pal["dim"])

    # ---------------------------------------------------------------- 实时刷新
    def _mmap(self, v):
        mx0, mx1, _ = self.meter
        v = min(max(v, METER_LO), METER_HI)
        return mx0 + (v - METER_LO) / (METER_HI - METER_LO) * (mx1 - mx0)

    def _draw_band(self):
        """目标范围高亮：拖动滑块时只重画这一块，不重建整页。"""
        if not self.meter:
            return
        pal = self.pal
        cfg = self.config
        auto = str(cfg.get("level_mode", "auto")).lower() != "limit"
        target = float(cfg.get("target_lufs", -18))
        rng = float(cfg.get("target_range_db", 3)) if auto else 0.0
        cv = self._painter.cv
        _, _, my = self.meter
        if self.m_band is not None:
            try:
                cv.delete(self.m_band)
            except Exception:
                pass
            self.m_band = None
        b0, b1 = self._mmap(target - rng), self._mmap(target + rng)
        if b1 - b0 >= 3:
            img = ui_kit.shape(cv, b0, my - 3, b1 - b0, 6, 3, fill=pal["accent_dim"])
            if img is not None:
                try:
                    cv.tag_lower(img, self.m_mark)
                except Exception:
                    pass
                self.m_band = img
        txt = ("目标 %.1f ± %.1f" % (target, rng)) if auto else ("超过 %.1f 才压" % target)
        try:
            cv.itemconfigure(self.m_band_txt, text=txt)
        except Exception:
            pass

    def _update_live(self):
        st = self.engine.status
        pal = self.pal
        lufs = float(st.get("lufs", -70))
        gain = float(st.get("gain_db", 0))
        err = st.get("error")
        waiting = bool(st.get("waiting"))
        if self.page == "loudness":
            cv = self._painter.cv if self._painter else None
            if cv is not None:
                try:
                    cv.itemconfigure(self.m_lufs, text=_fmt_lufs(lufs))
                    cv.itemconfigure(self.m_gain, text="增益 " + _fmt_db(gain))
                    cv.itemconfigure(self.m_mode,
                                     text=("已绕过音效" if st.get("out_mode") == "raw"
                                           else "经系统音效"))
                    cv.coords(self.m_mark, self._mmap(lufs) - 2, self.meter[2] - 10)
                except Exception:
                    pass
            foot = ("响度 %s LUFS · 增益 %s · Win音量 %.0f%%" % (
                _fmt_lufs(lufs), _fmt_db(gain),
                float(st.get("win_vol", 1.0) or 1.0) * 100))
            self._set_foot(foot, pal["sub"])
        elif err:
            self._set_foot(str(err), pal["err"])
        elif waiting:
            self._set_foot("等待声音进入 CABLE：在 Windows 里播放任意声音即可开始", pal["warn"])
        else:
            self._set_foot("设置修改后立即生效并自动保存", pal["dim"])
        self._refresh_save()
        if self.page == "device":
            self._update_default_label()

    def _set_foot(self, text, color):
        cv = self.body
        try:
            items = cv.find_withtag("foot")
            if items:
                cv.itemconfigure(items[0], text=text, fill=color)
        except Exception:
            pass

    def _update_default_label(self):
        if not getattr(self, "m_default", None) or self.guard is None:
            return
        g = self.guard.status()
        line = "系统默认输出：%s" % (g["default"] or "—")
        if g.get("error"):
            line += "　⚠ %s" % g["error"]
        elif g.get("virtual"):
            line += "　（虚拟声卡 · 正常）"
        try:
            self._painter.cv.itemconfigure(
                self.m_default, text=line,
                fill=self.pal["err"] if g.get("error") else self.pal["dim"])
        except Exception:
            pass

    def _poll(self):
        try:
            while True:
                cmd = self.cmds.get_nowait()
                if cmd == "show":
                    self.show()
                elif cmd == "quit":
                    self.quit()
                    return
        except Exception:
            pass
        try:
            if theme.system_dark() != self.dark:
                self.apply_theme()
        except Exception:
            pass
        self._follow_devices()
        self._follow_config()
        self._update_live()
        self.root.after(220, self._poll)

    def _follow_devices(self):
        """核心进程异步提供设备列表；UI 启动时状态文件可能还没写好。

        下拉框是按 self._inputs / _outputs 建的，列表变化时必须重画当前页，
        否则「音频设备」页永远是空的两个下拉框（必现 bug）。
        """
        try:
            ins = list(self.engine.status.get("inputs") or [])
            outs = list(self.engine.status.get("outputs") or [])
        except Exception:
            return
        if ins == self._inputs and outs == self._outputs:
            return
        self._inputs, self._outputs = ins, outs
        if self.page == "device":
            self.render()

    def _follow_config(self):
        """托盘 / 设备守护改了配置时，界面跟着变（不反向写配置）。"""
        try:
            out_cfg = self.config.get("output_device", "") or ""
            if (self.var_output.get() or "") != out_cfg:
                self.var_output.set(out_cfg)
            in_cfg = self.config.get("input_device", "") or ""
            if (self.var_input.get() or "") != in_cfg:
                self.var_input.set(in_cfg)
        except Exception:
            pass

    # ---------------------------------------------------------------- 回调
    def _set(self, key, value):
        self.config.set(key, value)
        self._mark_change()

    def _on_enabled(self, v):
        self._set("enabled", bool(v))

    def _on_passthrough(self, v):
        self._set("passthrough", bool(v))

    def _on_mode(self, i):
        self._set("level_mode", "auto" if i == 1 else "limit")
        self.root.after(10, self.render)

    def _on_target(self, v):
        self._set("target_lufs", float(v))
        self._draw_band()

    def _on_range(self, v):
        self._set("target_range_db", float(v))
        self._draw_band()

    def _on_maxatt(self, v):
        self._set("max_attenuation_db", float(v))

    def _on_maxboost(self, v):
        self._set("max_boost_db", float(v))

    def _on_ceiling(self, v):
        self._set("limiter_ceiling_db", float(v))

    def _on_pre(self, v):
        self._set("pre_gain_db", float(v))

    def _on_mute(self, v):
        self._set("mute_enabled", bool(v))

    def _on_mute_bg(self, v):
        self._set("mute_listed_in_background", bool(v))

    def _on_mute_others(self, v):
        self._set("mute_others_in_game", bool(v))

    def _on_fg(self, v):
        self._set("mute_fg_volume", float(v))

    def _on_bg(self, v):
        self._set("mute_bg_volume", float(v))

    def _on_easing(self, v):
        self._set("mute_easing_ms", float(v))

    def _on_follow(self, v):
        self._set("auto_follow_default", bool(v))

    def _on_winvol(self, v):
        self._set("use_windows_volume", bool(v))
        self.root.after(10, lambda: self.engine.request_reload())

    def _on_block(self, v):
        """缓冲改动要重建会话才生效；拖动时别每格都重连，停手 0.6 秒再应用。"""
        self._set("block_ms", float(v))
        job = getattr(self, "_block_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._block_job = self.root.after(600, self._apply_block)

    def _apply_block(self):
        self._block_job = None
        self.engine.request_reload()
        self._set_foot("已应用缓冲设置，正在重连…", self.pal["warn"])

    # ---- 名单 ----
    def _fill_list(self):
        lb = getattr(self, "m_list", None)
        if lb is None:
            return
        try:
            lb.delete(0, "end")
            for g in (self.config.get("mute_games", []) or []):
                lb.insert("end", g)
        except Exception:
            pass

    def _add_game(self):
        name = (self.var_newgame.get() or "").strip()
        if not name:
            return
        games = list(self.config.get("mute_games", []) or [])
        if name.lower() in [g.lower() for g in games]:
            return
        games.append(name)
        self._set("mute_games", games)
        self.var_newgame.set("")
        self._fill_list()

    def _del_game(self):
        lb = getattr(self, "m_list", None)
        if lb is None:
            return
        sel = list(lb.curselection())
        if not sel:
            return
        games = list(self.config.get("mute_games", []) or [])
        for i in reversed(sel):
            if 0 <= i < len(games):
                games.pop(i)
        self._set("mute_games", games)
        self._fill_list()

    def _reset_games(self):
        from config import DEFAULTS
        self._set("mute_games", list(DEFAULTS["mute_games"]))
        self._fill_list()

    # ---- 设备 ----
    def _rescan(self, silent=False):
        try:
            self._inputs = self.engine.list_inputs()
            self._outputs = self.engine.list_outputs()
        except Exception:
            self._inputs, self._outputs = [], []

    def _rescan_click(self):
        self._rescan()
        self.render()

    def _apply_device(self):
        self.config.update({
            "input_device": self.var_input.get(),
            "output_device": self.var_output.get(),
        })
        self.engine.request_reload()
        self._mark_change()
        self._set_foot("已应用设备设置，正在重连…", self.pal["warn"])

    # ---------------------------------------------------------------- 窗口
    def show(self):
        try:
            if self.root.state() != "normal":
                # 藏起来的时候才能抓到「背后的桌面」，顺便重画一次
                self._capture_glass()
                self.render()
        except Exception:
            pass
        self.root.deiconify()
        self.root.lift()
        self._apply_frame()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def hide(self):
        self.config.set("window_size", "%sx%s" % (
            self.root.winfo_width(), self.root.winfo_height()))
        self.config.save_now()
        self.root.withdraw()

    def quit(self):
        try:
            self.config.set("window_size", "%sx%s" % (
                self.root.winfo_width(), self.root.winfo_height()))
            self.muter.restore()
        except Exception:
            pass
        try:
            self.config.save_now()
        except Exception:
            pass
        if self.guard is not None:
            try:
                self.guard.stop()
            except Exception:
                pass
        self.engine.stop()
        self.root.destroy()


def show_error(title, message):
    from tkinter import messagebox
    messagebox.showerror(title, message)
