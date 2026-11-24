"""主界面：左侧导航 + 四页，全自绘，亚克力背景，跟随系统深色。

界面不依赖 ttk 主题（只有下拉框用 ttk）：卡片、开关、滑块、按钮都由
ui_kit 用 PIL 形状 + Canvas 文本画出来，所以圆角抗锯齿、hover 会变色、
深浅色切换只要换一套调色板。

刷新策略：页面内容只在切页 / 改窗口大小 / 改动后重建；实时数值（响度、
增益、状态、保存状态）只改文字和坐标，不重建 —— 否则拖滑块会被打断。
"""
import math
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageFilter, ImageGrab, ImageTk

import autostart
import ipc
import theme
import ui_kit
from theme import FONT

APP_TITLE = "音频均衡器"
VERSION = "1.8.3"

SIDE_W = 192

NAV = [("loudness", "响度均衡"), ("mute", "自动静音"),
       ("device", "音频设备"), ("about", "关于")]

METER_LO, METER_HI = -45.0, -5.0

# 音量均衡预设：一次把「目标 + 范围 + 上限 + 跟随快慢」整套设好。
# 顺序即界面顺序；None = 自定义（不写入任何值，只记录"当前是手动模式"）。
# 判定逻辑：配置里的 loud_preset 说了算（预设名 / "custom"）；老配置没这个键时
# 才按数值反推，保证从旧版本升级上来的人看到的还是原来那一档。
LOUD_PRESETS = [
    ("自定义", None,
     "手动模式：下面各项自己调；点上面任意预设可一键套用。"),
    ("人声", {"enabled": True, "level_mode": "auto",
              "target_lufs": -16.0, "target_range_db": 3.0,
              "max_boost_db": 15.0, "max_attenuation_db": 20.0,
              "gain_attack_ms": 60, "gain_release_ms": 500},
     "人声 / 播客：跟得快，小声说话也听得清（目标 −16 LUFS）。"),
    ("均衡", {"enabled": True, "level_mode": "auto",
              "target_lufs": -18.0, "target_range_db": 3.0,
              "max_boost_db": 12.0, "max_attenuation_db": 20.0,
              "gain_attack_ms": 120, "gain_release_ms": 900},
     "均衡：音乐 / 视频通用，动得不多，听起来最自然（目标 −18 LUFS）。"),
    ("电影", {"enabled": True, "level_mode": "auto",
              "target_lufs": -20.0, "target_range_db": 4.0,
              "max_boost_db": 10.0, "max_attenuation_db": 24.0,
              "gain_attack_ms": 150, "gain_release_ms": 1500},
     "电影 / 剧集：保住大动态，只收一收过响的部分（目标 −20 LUFS）。"),
    ("夜间", {"enabled": True, "level_mode": "auto",
              "target_lufs": -23.0, "target_range_db": 2.0,
              "max_boost_db": 18.0, "max_attenuation_db": 30.0,
              "gain_attack_ms": 40, "gain_release_ms": 300},
     "夜间 / 游戏：把动态压扁，音量不大也什么都听得见（目标 −23 LUFS）。"),
]

# 参与「预设是否还成立」判定的键。enabled 不算：它是总开关，
# 关掉均衡不该让预设高亮跳到「自定义」；其余五个就是预设真正定义的东西。
PRESET_KEYS = ("level_mode", "target_lufs", "target_range_db",
               "max_boost_db", "max_attenuation_db",
               "gain_attack_ms", "gain_release_ms")



def _as_float(v, default=0.0):
    """状态文件里的数值可能缺失 / 是 None / 是字符串 / 是 NaN，统一收口。

    失败或非有限值都返回 default（可以是 None），调用方不用再各写一遍 try。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _fmt_db(v):
    """增益显示。非有限值（NaN/inf）一律显示「—」：曾经会把 "nan" 直接画到界面上。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    return "关闭" if v <= -39.5 else "%+.1f dB" % v


def _latency_ms(block_ms):
    """端到端延迟估算，与引擎口径一致：

    采集/队列 2 块 + 设备缓冲（两块+6ms，下限 24）+ 限幅器前瞻 2ms。
    """
    block_ms = float(block_ms)
    return block_ms * 2 + max(24.0, block_ms * 2 + 6) + 2.0


def _fmt_lufs(v):
    """响度显示。NaN / ±inf / 非数字都不能漏到界面上（只显示「—」）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
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
        self._tip_text = None      # 当前悬停的说明文字
        self._tip_ids = []         # 气泡的 canvas item（重建页面时清掉）
        self._foot_hold = None     # 状态栏临时提示 (文字, 颜色, 截止时间)
        self._live = {}            # 实时数值的「上次显示值」，没变就不重绘
        self._devices_dirty = False  # 设备列表变了但正在拖控件，松手后再整页重画
        self.meter = None
        self.m_band = None
        self.m_preset_seg = None   # 预设分段控件（改数值时要即时把高亮挪到「自定义」）
        self.m_preset_desc = None
        self.m_lat = None          # 设备页「当前端到端延迟」
        self.m_lufs = self.m_gain = self.m_mode = self.m_mark = None
        self._glass_raw = None
        self._img_body = None
        self._img_side = None

        root.title(APP_TITLE)
        root.geometry(config.get("window_size", "940x760"))
        root.minsize(900, 760)
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
        c.bind("<Leave>", lambda e: self._hide_tip())
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
        tip = (hit or {}).get("tip")
        if tip != self._tip_text:
            if tip:
                self._show_tip(tip, hit.get("box"))
            else:
                self._hide_tip()

    # ---------------------------------------------------------------- 说明气泡
    def _show_tip(self, text, box=None):
        """在图标下方画说明气泡（纯 canvas 图元，画在最上层，不参与点击）。"""
        self._hide_tip()
        cv = self.body
        # 先用一条屏外文本量出自动换行后的真实尺寸
        tmp = cv.create_text(-4000, -4000, text=text, font=(FONT, 9),
                             width=250, anchor="nw", fill=self.pal["text"])
        bb = cv.bbox(tmp)
        cv.delete(tmp)
        if not bb:
            return
        tw, th = max(60, bb[2] - bb[0]), max(16, bb[3] - bb[1])
        bw, bh = tw + 24, th + 14
        W = max(cv.winfo_width(), 640)
        H = max(cv.winfo_height(), 520)
        if box:
            ax = (box[0] + box[2]) / 2.0 - bw / 2.0
            ay = box[3] + 9
        else:
            ax, ay = 40.0, 40.0
        ax = min(max(8.0, ax), W - bw - 8.0)
        if ay + bh > H - 10:                    # 下面放不下就翻到图标上方
            ay = (box[1] - bh - 9) if box else max(8.0, ay - bh - 20)
        ay = max(8.0, ay)
        bg = ui_kit.shape(cv, ax, ay, bw, bh, 8,
                          fill=self.pal["card2"], outline=self.pal["line"])
        tx = cv.create_text(ax + 12, ay + 7, text=text, font=(FONT, 9),
                            width=250, anchor="nw", fill=self.pal["text"])
        self._tip_ids = [i for i in (bg, tx) if i is not None]
        self._tip_text = text

    def _hide_tip(self):
        for i in self._tip_ids:
            try:
                self.body.delete(i)
            except Exception:
                pass
        self._tip_ids = []
        self._tip_text = None

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
        # 先销毁上一轮的内嵌控件（下拉框 / 输入框 / 列表框）：
        # canvas.delete("all") 只清画布图元，这些 Tk 控件会一直挂在 canvas 上
        for w in list(getattr(self.body, "_embedded", []) or []):
            try:
                w.destroy()
            except Exception:
                pass
        try:
            self.body._embedded = []
        except Exception:
            pass
        self.body.delete("all")
        ui_kit.reset_cache()
        self._hits = []
        self._hover = None
        self._active = None
        self._btn_save = None
        self._tip_ids = []          # delete("all") 已经连带删掉气泡
        self._tip_text = None
        self._live = {}             # 画布重建了，缓存的「上次显示值」全部作废

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

        # 与 icon.ico 同款：蓝色圆角方 + 三根白柱（比例照搬，保证标题栏/侧栏一致）
        ui_kit.shape(cv, 24, 24, 38, 38, 9, fill=pal["accent"])
        for bx, top, bot in ((32, 43, 55), (39, 35, 55), (46, 39, 55)):
            ui_kit.shape(cv, bx, top, 5, bot - top, 2, fill=pal["on_accent"])
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
        paused = st.get("paused")
        if err:
            color, label = pal["err"], "链路中断"
        elif paused == "rdp":
            color, label = pal["warn"], "远程桌面 · 已暂停"
        elif not running:
            color, label = pal["err"], "未运行"
        else:
            color, label = pal["ok"], "运行中"
        dy = H - 44
        ui_kit.shape(cv, 26, dy - 4, 9, 9, 4.5, fill=color)
        cv.create_text(44, dy, text=label,
                       anchor="w", font=(FONT, 10), fill=pal["sub"])

    def _render_footer(self, p, x, w, H):
        pal = self.pal
        y = H - 52
        line = p.cv.create_line(x, y - 10, x + w, y - 10, fill=pal["line"])
        self._foot = p.cv.create_text(x, y + 16, text="", anchor="w",
                                      font=(FONT, 9), fill=pal["sub"], tags="foot")
        self._btn_save = p.button(x + w - 106, y - 3, 106, 34, "已保存",
                                  self._save_now, kind="primary", size=10)
        # 分隔线和保存按钮打上 chrome 标记：内容区做越界检查时要排除它们
        try:
            p.cv.itemconfigure(line, tags="chrome")
            p.cv.itemconfigure(self._btn_save.img, tags="chrome")
            p.cv.itemconfigure(self._btn_save.label, tags="chrome")
        except Exception:
            pass
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
        cy = y + 58
        p.card(x, cy, w, 100)
        p.text(x + 22, cy + 20, "当前响度", 10, pal["sub"])
        self.m_lufs = p.text(x + 22, cy + 54, "—", 22, bold=True)
        p.text(x + 22 + 118, cy + 58, "LUFS", 9, pal["dim"])
        self.m_gain = p.text(x + w - 24, cy + 20, "增益 —", 10, pal["sub"], anchor="e")
        self.m_mode = p.text(x + w - 24, cy + 41, "—", 9, pal["dim"], anchor="e")

        mx0, mx1, my = x + 196, x + w - 24, cy + 54
        self.meter = (mx0, mx1, my)
        ui_kit.shape(p.cv, mx0, my - 3, mx1 - mx0, 6, 3, fill=pal["track"])
        self.m_band = None
        self.m_band_txt = p.text((mx0 + mx1) / 2, my + 22, "", 8, pal["sub"], anchor="center")
        self._draw_band()
        self.m_mark = ui_kit.shape(
            p.cv, self._mmap(_as_float(self.engine.status.get("lufs"), -70.0)) - 2,
            my - 10, 5, 20, 2.5, fill=pal["accent"])
        p.text(mx0, my + 22, "−45", 8, pal["dim"])
        p.text(mx1, my + 22, "−5", 8, pal["dim"], anchor="e")

        # --- 音量均衡预设 ---
        py = cy + 112
        p.card(x, py, w, 92)
        p.section(x + 22, py + 22, "音量均衡预设",
                  tip="一次设好目标响度、范围、上限和跟随快慢；"
                      "选好之后手动微调任何一项，会自动切到「自定义」。")
        idx = self._preset_index()
        self.m_preset_seg = p.segment(x + 22, py + 34, w - 44, 30,
                                      [n for n, _s, _d in LOUD_PRESETS], idx,
                                      self._on_preset)
        self.m_preset_desc = p.text(x + 22, py + 72, LOUD_PRESETS[idx][2], 9,
                                    pal["sub"], width=w - 44)

        # --- 处理方式 ---
        by = py + 102
        p.card(x, by, w, 206)
        p.section(x + 22, by + 24, "处理方式",
                  tip="自动电平：低于目标就提、高于目标就压，让声音一直待在目标附近；"
                      "仅限制：只压过响的部分，安静内容原样通过（不放大底噪）。")
        p.segment(x + 22, by + 46, 236, 34, ["仅限制", "自动电平"],
                  1 if auto else 0, self._on_mode)
        p.text(x + w - 96, by + 50, "启用均衡", 10, pal["sub"], anchor="e")
        p.help(x + w - 84, by + 50,
               "关掉后不做任何电平调整，只按音量/直通处理；自动静音不受影响。")
        p.toggle(x + w - 24 - 46, by + 46, lambda: bool(cfg.get("enabled", True)),
                 self._on_enabled)
        desc = ("自动电平：低于范围就提上来、高于范围就压下去，落在范围内不动，"
                "让声音一直待在目标附近（最多提升 +%.0fdB）。"
                % float(cfg.get("max_boost_db", 12))
                if auto else
                "仅限制：只把超过目标的部分压下来，安静的内容原样通过（不会放大底噪）。")
        p.text(x + 22, by + 88, desc, 9, pal["sub"], width=w - 44, anchor="nw")

        col = (w - 44 - 26) / 2.0
        p.field(x + 22, by + 142, col, "目标响度", float(cfg.get("target_lufs", -18)),
                -30, -8, 0.5, self._on_target, fmt=lambda v: "%.1f LUFS" % v,
                tip="希望听到的平均响度。−18 LUFS ≈ 常见视频的正常音量；数字越小越响。")
        p.field(x + 22 + col + 26, by + 142, col, "目标范围 ±",
                float(cfg.get("target_range_db", 3)), 0, 8, 0.5, self._on_range,
                fmt=lambda v: "±%.1f dB" % v, disabled=not auto,
                note="" if auto else "仅自动电平",
                tip="允许的偏差带。落在目标附近这个范围内就不动手，避免一直微调导致的「呼吸感」。")

        # --- 限制与余量 ---
        ly = by + 214
        p.card(x, ly, w, 178)
        p.section(x + 22, ly + 24, "限制与余量",
                  tip="这几项是安全边界：决定最多能提升/衰减多少，以及输出峰值的天花板。")
        p.field(x + 22, ly + 46, col, "最大衰减",
                float(cfg.get("max_attenuation_db", 20)), 0, 36, 1,
                self._on_maxatt, fmt=lambda v: "−%.0f dB" % v,
                tip="最多把响的部分压下去多少 dB。小一点更保动态，大一点更平。")
        p.field(x + 22 + col + 26, ly + 46, col, "最大提升",
                float(cfg.get("max_boost_db", 12)), 0, 24, 1,
                self._on_maxboost, fmt=lambda v: "+%.0f dB" % v,
                disabled=not auto, note="" if auto else "仅自动电平",
                tip="最多把安静的部分提上来多少 dB。提得越多底噪也一起被放大；"
                    "比目标低 34 dB 以上的片段不会再提升（护底噪拐点）。")
        p.field(x + 22, ly + 98, col, "限幅上限",
                float(cfg.get("limiter_ceiling_db", -1)), -6, 0, 0.5,
                self._on_ceiling, fmt=lambda v: "%.1f dBTP" % v,
                tip="输出的绝对上限，按真峰值（dBTP）算：会检查采样点之间的峰值，"
                    "不是只看采样点。留 −1 dB 是给 AAC/MP3 重编码的余量。")
        p.field(x + 22 + col + 26, ly + 98, col, "处理前降低",
                float(cfg.get("pre_gain_db", 0)), -24, 0, 1,
                self._on_pre, fmt=lambda v: "%+.0f dB" % v,
                tip="整体先降这么多再处理：等于把音量基准放低，同时给限幅器留更多余量。")
        p.text(x + 22, ly + 150, "直通：不做响度处理，原样送到耳机（保留 0 dBFS 过载保护）",
               9, pal["sub"])
        p.toggle(x + w - 24 - 46, ly + 142,
                 lambda: bool(cfg.get("passthrough", False)), self._on_passthrough)

    def _page_mute(self, p, x, y, w, H):
        pal = self.pal
        cfg = self.config
        p.text(x, y + 6, "自动静音", 18, bold=True)
        p.text(x, y + 34, "按名单判断：谁在挂机、谁该让路", 10, pal["sub"])

        cy = y + 62
        p.card(x, cy, w, 62)
        p.text(x + 22, cy + 22, "启用自动静音", 11, bold=True)
        p.help(x + 22 + ui_kit.text_width("启用自动静音", 11, True) + 12, cy + 22,
               "总开关。关掉后立刻恢复所有程序的原音量，不再自动静音。")
        p.text(x + 22, cy + 43, "关掉后立刻恢复所有程序的原音量", 9, pal["sub"])
        p.toggle(x + w - 24 - 46, cy + 18, lambda: bool(cfg.get("mute_enabled", True)),
                 self._on_mute)

        ry = cy + 74
        p.card(x, ry, w, 146)
        p.section(x + 22, ry + 24, "规则", "两条规则各自独立，只认名单里的程序", right=x + w - 22)
        p.text(x + 22, ry + 54, "挂机静音", 10)
        p.help(x + 22 + ui_kit.text_width("挂机静音", 10) + 12, ry + 54,
               "名单里的程序切到后台（最小化/挂机）就只把它自己静音，其它程序不受影响。")
        p.text(x + 22, ry + 74, "名单里的程序切到后台（最小化 / 挂机）就把它自己静音", 9, pal["sub"])
        p.toggle(x + w - 24 - 46, ry + 48,
                 lambda: bool(cfg.get("mute_listed_in_background", True)), self._on_mute_bg)
        p.divider(x + 22, ry + 94, w - 44)
        p.text(x + 22, ry + 112, "游戏时压制其它程序", 10)
        p.help(x + 22 + ui_kit.text_width("游戏时压制其它程序", 10) + 12, ry + 112,
               "名单里的程序在前台时，把其它程序的音量压到下面「压到」的值，"
               "用来突出游戏/会议的声音。")
        p.text(x + 22, ry + 132, "名单里的程序在前台时，把其它程序压到「压到」的值", 9, pal["sub"])
        p.toggle(x + w - 24 - 46, ry + 106,
                 lambda: bool(cfg.get("mute_others_in_game", False)), self._on_mute_others)

        vy = ry + 158
        p.card(x, vy, w, 160)
        p.section(x + 22, vy + 24, "音量")
        col = (w - 44 - 26) / 2.0
        p.field(x + 22, vy + 52, col, "名单程序在前台时",
                float(cfg.get("mute_fg_volume", 1.0)), 0, 1, 0.05,
                self._on_fg, fmt=lambda v: "%.0f%%" % (v * 100),
                tip="名单里的程序自己在前台时的音量，一般保持 100%。")
        p.field(x + 22 + col + 26, vy + 52, col, "压到",
                float(cfg.get("mute_bg_volume", 0.0)), 0, 1, 0.05,
                self._on_bg, fmt=lambda v: "%.0f%%" % (v * 100),
                tip="别的程序被压到多少音量。0% 就是完全静音，想还能听见点就用 10~20%。")
        p.field(x + 22, vy + 110, col, "渐变时长",
                float(cfg.get("mute_easing_ms", 300)), 100, 2000, 50,
                self._on_easing, fmt=lambda v: "%.0f ms" % v,
                tip="音量变化的过渡时间。太短会有突兀的「啪」，太长会显得反应迟钝。")

        gy = vy + 174
        head, row_h, line_px = 52, 44, 23     # 23px = YaHei 10pt 的实测行高
        list_h = int(H - gy - 8 - head - row_h - 6)
        list_h = max(line_px * 3, (list_h // line_px) * line_px)   # 整行数，避免半行被切
        # +6：输入框高 32，行高 44 时底部只剩 4px，给卡片底留点呼吸
        gh = head + list_h + row_h + 6
        p.card(x, gy, w, gh)
        p.section(x + 22, gy + 26, "名单", "填进程名，如 StarRail.exe（不区分大小写）",
                  right=x + w - 22,
                  tip="只认这里的程序。进程名看任务管理器「详细信息」里的名称，不区分大小写。")
        lb = p.listbox(x + 24, gy + head, w - 48, list_h)
        for g in sorted((cfg.get("mute_games", []) or []), key=str.lower):
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

        cy = y + 58
        p.card(x, cy, w, 212)
        p.section(x + 22, cy + 22, "设备", "改完立即重连（约 0.2 秒静音）", right=x + w - 22)

        p.text(x + 22, cy + 62, "监听输入", 10, pal["sub"])
        p.help(x + 22 + ui_kit.text_width("监听输入", 10) + 12, cy + 62,
               "从哪里采集声音。一般是 CABLE：系统默认输出指向它，所有程序的声音都会经过它。")
        cb1 = ttk.Combobox(p.cv, textvariable=self.var_input,
                           values=[""] + self._inputs, state="readonly",
                           style="Acrylic.TCombobox", font=(FONT, 10))
        cb1.bind("<<ComboboxSelected>>", lambda e: self._apply_device())
        p.cv.create_window(x + 132, cy + 56, window=cb1, anchor="nw",
                           width=w - 176, height=30)
        p._track(cb1)

        p.text(x + 22, cy + 106, "输出设备", 10, pal["sub"])
        p.help(x + 22 + ui_kit.text_width("输出设备", 10) + 12, cy + 106,
               "处理完送到哪个设备，通常是你的耳机/音箱。")
        cb2 = ttk.Combobox(p.cv, textvariable=self.var_output,
                           values=[""] + self._outputs, state="readonly",
                           style="Acrylic.TCombobox", font=(FONT, 10))
        cb2.bind("<<ComboboxSelected>>", lambda e: self._apply_device())
        p.cv.create_window(x + 132, cy + 100, window=cb2, anchor="nw",
                           width=w - 176, height=30)
        p._track(cb2)

        blk = float(cfg.get("block_ms", 15))
        p.field(x + 22, cy + 142, w - 44, "缓冲（越小延迟越低；10ms 已很稳）",
                blk, 10, 100, 1, self._on_block, fmt=lambda v: "%.0f ms" % v,
                tip="每块处理的时长：越小延迟越低，但越依赖设备缓冲去挡线程抖动。11–15 ms 兼顾跟手和稳定。")
        self.m_lat = p.text(x + 22, cy + 192,
                            "当前端到端延迟约 %.0f ms（采集/队列 2 块 + 设备缓冲）"
                            % _latency_ms(blk),
                            8, pal["dim"])

        ty = cy + 222
        p.card(x, ty, w, 158)
        p.text(x + 22, ty + 22, "自动接管系统默认输出", 11, bold=True)
        p.help(x + 22 + ui_kit.text_width("自动接管系统默认输出", 11, True) + 12, ty + 22,
               "你在系统里把默认输出点到耳机时，本程序自动把耳机设为输出设备，"
               "再把系统默认改回虚拟声卡——声音不断，下次换耳机再点一下就行。")
        p.toggle(x + w - 24 - 46, ty + 16, lambda: bool(cfg.get("auto_follow_default", True)),
                 self._on_follow)
        p.text(x + 22, ty + 46,
               "系统里把默认输出点成耳机时，自动把它设为输出设备，再把系统默认改回虚拟声卡。",
               9, pal["sub"], width=w - 48, anchor="nw")
        p.divider(x + 22, ty + 70, w - 44)
        p.text(x + 22, ty + 88, "在远程桌面中接管", 11, bold=True)
        p.help(x + 22 + ui_kit.text_width("在远程桌面中接管", 11, True) + 12, ty + 88,
               "关掉（默认）：远程桌面会话里本程序完全不碰声音——不处理响度、不改默认输出，"
               "声音直接送到对面的设备；打开：远程桌面里也像本机一样接管（需要把默认输出改成 CABLE）。")
        p.toggle(x + w - 24 - 46, ty + 82,
                 lambda: bool(cfg.get("rdp_takeover", False)), self._on_rdp)
        p.text(x + 22, ty + 110,
               "关闭时远程桌面里不处理声音、也不改系统默认输出。",
               9, pal["sub"], width=w - 48, anchor="nw")
        self.m_default = p.text(x + 22, ty + 140, "系统默认输出：—", 9, pal["dim"])

        vc = ty + 170
        p.card(x, vc, w, 162)
        p.text(x + 22, vc + 22, "用 Windows 音量滑块控制声音大小", 11, bold=True)
        p.help(x + 22 + ui_kit.text_width("用 Windows 音量滑块控制声音大小", 11, True) + 12,
               vc + 22,
               "引擎只读系统音量滑块的值当输出增益，绝不修改你的滑块。关掉则完全按内部增益输出。")
        p.text(x + 22, vc + 46,
               "系统音量里「CABLE In 16ch」的滑块即为最终音量，引擎主动读它当输出增益。",
               9, pal["sub"], width=w - 60)
        p.toggle(x + w - 24 - 46, vc + 16,
                 lambda: bool(cfg.get("use_windows_volume", True)), self._on_winvol)

        col = (w - 44 - 26) / 2.0
        p.field(x + 22, vc + 68, col, "正常音量位置",
                float(cfg.get("winvol_ref", 0.20)) * 100, 5, 60, 1,
                self._on_winvol_ref, fmt=lambda v: "%.0f%%" % v,
                note="此处=标准音量",
                tip="滑块停在这个位置时输出 0 dB（标准音量）。习惯把音量拉得很低的话，把它调小。")
        p.field(x + 22 + col + 26, vc + 68, col, "最大增益",
                float(cfg.get("winvol_max_db", 10.0)), 0, 20, 0.5,
                self._on_winvol_max, fmt=lambda v: "+%.1f dB" % v,
                note="100% 时",
                tip="滑块拉到 100% 时额外增加多少 dB。加太多会让软限幅一直在工作，听感发毛。")
        p.text(x + 22, vc + 132,
               "默认 20% = 正常音量，100% = +10dB；觉得整体偏大/偏小就调上面两项。",
               9, pal["dim"], width=w - 44)

        by = vc + 172
        p.button(x + 22, by, 96, 34, "重新扫描", self._rescan_click, kind="ghost")
        p.button(x + 126, by, 116, 34, "应用并重连", self._apply_device)
        # 提示放按钮同一行右侧，不额外占高度，避免和底部状态栏重叠
        p.text(x + 256, by + 6,
               "系统默认输出设为 CABLE 虚拟声卡，本程序从它采集声音。",
               9, pal["dim"], anchor="nw", width=w - 280)

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
               "自动电平把它拉进目标范围，再经软限幅送到耳机。", 9, pal["sub"],
               width=w - 48, anchor="nw")
        p.text(x + 24, cy + 134, "自动静音", 10, pal["accent"], bold=True)
        p.text(x + 24, cy + 154,
               "名单里的程序挂机（在后台）时静音它自己；可选地在它前台时让其它程序让路。",
               9, pal["sub"], width=w - 48, anchor="nw")
        p.divider(x + 24, cy + 202, w - 48)
        p.text(x + 24, cy + 216,
               "只读系统音量滑块、绝不修改它；也不改各程序音量（自动静音例外：退出时按原值恢复）。",
               9, pal["dim"], width=w - 48, anchor="nw")

        p.card(x, cy + 270, w, 188)
        p.text(x + 24, cy + 292, "开机自启", 11, bold=True)
        p.help(x + 24 + ui_kit.text_width("开机自启", 11, True) + 12, cy + 292,
               "登录 Windows 时静默启动核心：托盘常驻、不弹界面，声音处理立刻可用。")
        p.text(x + 24, cy + 316,
               "开机时静默启动核心（托盘常驻、不弹界面），声音处理立即就绪。",
               9, pal["sub"], width=w - 96, anchor="nw")
        p.toggle(x + w - 24 - 46, cy + 290, autostart.is_enabled, self._on_autostart)

        p.divider(x + 24, cy + 350, w - 48)
        p.text(x + 24, cy + 366, "配置文件", 9, pal["dim"])
        p.text(x + 24, cy + 386, (self.config.path or ""), 9, pal["sub"],
               width=w - 48, anchor="nw")
        p.text(x + 24, cy + 422,
               "端到端延迟默认约 %d ms，可在「音频设备」里用缓冲滑块调节"
               % int(_latency_ms(self.config.get("block_ms", 15))),
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
        # 状态文件来自另一个进程：字段缺失、None、字符串、NaN 都可能出现，
        # 这里统一收口，宁可显示「—」也不能让格式化抛异常（抛了就不刷新了）
        lufs = _as_float(st.get("lufs"), -70.0)
        gain = _as_float(st.get("gain_db"), 0.0)
        err = st.get("error")
        waiting = bool(st.get("waiting"))
        paused = st.get("paused")
        if paused == "rdp" and not bool(st.get("running")):
            self._set_foot("远程桌面会话中：按设置不接管声音（可在「音频设备」页开启）",
                           pal["warn"])
        elif self.page == "loudness" and self._painter is not None:
            self._set_live_text("lufs", self.m_lufs, _fmt_lufs(lufs))
            self._set_live_text("gain", self.m_gain, "增益 " + _fmt_db(gain))
            self._set_live_text("mode", self.m_mode,
                                "已绕过音效" if st.get("out_mode") == "raw" else "经系统音效")
            self._set_mark(lufs)
            parts = ["响度 %s LUFS" % _fmt_lufs(lufs), "增益 " + _fmt_db(gain)]
            tp = _as_float(st.get("out_tp_db"), None)
            if tp is not None and tp > -90 and math.isfinite(tp):
                parts.append("峰值 %.1f dBTP" % tp)
            parts.append("Win音量 %.0f%%"
                         % (_as_float(st.get("win_vol"), 1.0) * 100))
            self._set_foot(" · ".join(parts), pal["sub"])
        elif err:
            self._set_foot(str(err), pal["err"])
        elif waiting:
            self._set_foot("等待声音进入 CABLE：在 Windows 里播放任意声音即可开始", pal["warn"])
        else:
            self._set_foot("设置修改后立即生效并自动保存", pal["dim"])
        # 临时提示优先显示几秒（否则上面这行会立刻把它盖掉）
        hold = getattr(self, "_foot_hold", None)
        if hold is not None:
            if time.time() < hold[2]:
                self._set_foot(hold[0], hold[1])
            else:
                self._foot_hold = None
        self._refresh_save()
        if self.page == "device":
            self._update_default_label()

    def _set_live_text(self, key, item, text):
        """实时数值只在真的变化时写回。

        Tk 的 itemconfigure 即使值没变也会标记重绘；界面 4~5 次/秒的空更新
        会让一个「什么都不做」的窗口也吃掉几个点 CPU。
        """
        if item is None:
            return
        cache = self._live
        if cache.get(key) == text:
            return
        cache[key] = text
        cv = self._painter.cv if self._painter else None
        if cv is None:
            return
        try:
            cv.itemconfigure(item, text=text)
        except Exception:
            pass

    def _set_mark(self, lufs):
        if not self.meter or self.m_mark is None:
            return
        x = round(self._mmap(lufs) - 2, 1)
        if self._live.get("mark") == x:
            return
        self._live["mark"] = x
        try:
            self._painter.cv.coords(self.m_mark, x, self.meter[2] - 10)
        except Exception:
            pass

    def _set_foot(self, text, color, hold=0.0):
        if hold:
            # 临时提示（"已应用…"）：状态栏每 220ms 会被实时数值改写，
            # 不留一会儿的话用户根本看不到。
            self._foot_hold = (text, color, time.time() + float(hold))
        if self._live.get("foot") == (text, color):
            return
        self._live["foot"] = (text, color)
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
        g = self.guard.status() or {}
        line = "系统默认输出：%s" % (g.get("default") or "—")
        if g.get("error"):
            line += "　⚠ %s" % g["error"]
        elif g.get("rdp"):
            line += "　· 远程桌面：不接管"
        elif g.get("virtual"):
            line += "　（虚拟声卡 · 正常）"
        if self._live.get("default") == line:
            return
        self._live["default"] = line
        try:
            self._painter.cv.itemconfigure(
                self.m_default, text=line,
                fill=self.pal["err"] if g.get("error") else self.pal["dim"])
        except Exception:
            pass

    def _poll(self):
        """220ms 一次的刷新链：单次异常绝不能打断 after()，否则界面就永久冻住。"""
        try:
            self._poll_once()
        except Exception:
            pass
        try:
            self.root.after(220, self._poll)
        except Exception:
            pass

    def _poll_once(self):
        try:
            while True:
                cmd = self.cmds.get_nowait()
                if cmd == "show":
                    self.show()
                elif cmd == "quit":
                    self.quit()
                    return
                elif cmd == "refresh":
                    # 外部（托盘 / 设备守护）改了配置：整页重画一次把开关、数值同步过来。
                    # 正在拖控件时先跳过，等松手后的下一轮再说，免得把控件拆掉。
                    if self._active is None:
                        self.render()
        except Exception:
            pass
        # 主题检查降频：读注册表每 220ms 一次没必要，2 秒足够跟手
        now = time.time()
        if now - getattr(self, "_last_theme_check", 0.0) > 2.0:
            self._last_theme_check = now
            try:
                if theme.system_dark() != self.dark:
                    self.apply_theme()
            except Exception:
                pass
        self._follow_devices()
        self._follow_config()
        # 设备列表变了：正在拖控件就先挂着，松手（_active 变 None）后再重绘，
        # 否则滑块会被从手底下拆掉、弹回旧值
        if self._devices_dirty and self._active is None:
            self._devices_dirty = False
            if self.page == "device":
                self.render()
        self._update_live()

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
        self._devices_dirty = True      # 由 _poll_once 在合适时机重绘

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
        old = self.config.get(key, None)
        self.config.set(key, value)
        # 手动改了预设定义里的任何一项 → 这一档不再"成立"，立刻切到「自定义」。
        # 只挪高亮和说明文字，不整页重绘：拖滑块时重建页面会把正在拖的控件拆掉。
        if key in PRESET_KEYS and old != value:
            self._mark_preset_custom()
        self._mark_change()

    # ---- 音量均衡预设 ----
    def _preset_values_match(self, spec):
        """配置里的值与某组预设是否一致（忽略总开关 enabled）。"""
        if not spec:
            return False
        cfg = self.config
        for k in PRESET_KEYS:
            if k not in spec:
                continue
            want, cur = spec[k], cfg.get(k, None)
            if isinstance(want, str):
                if str(cur) != want:
                    return False
            else:
                try:
                    if abs(float(cur) - float(want)) > 1e-6:
                        return False
                except (TypeError, ValueError):
                    return False
        return True

    def _preset_index(self):
        """当前该高亮哪一档。

        以配置里的 loud_preset 为准（预设名 / "custom"）；早先的配置没有这个键，
        退化成按数值反推，保证升级上来的用户看到的还是原来那一档。
        """
        cfg = self.config
        sel = str(cfg.get("loud_preset", "") or "")
        if sel == "custom":
            return 0
        for i, (name, spec, _desc) in enumerate(LOUD_PRESETS):
            if name == sel:
                return i if self._preset_values_match(spec) else 0
        if sel:                      # 预设列表改过 / 名字不认识
            return 0
        for i, (_name, spec, _desc) in enumerate(LOUD_PRESETS):
            if self._preset_values_match(spec):
                return i
        return 0

    def _mark_preset_custom(self):
        """手动微调后把状态切到「自定义」（值本身不动）。"""
        if str(self.config.get("loud_preset", "") or "") == "custom":
            return
        self.config.set("loud_preset", "custom")
        seg = self.m_preset_seg
        if seg is not None:
            try:
                seg.index = 0
                seg._place()
            except Exception:
                pass
        if self.m_preset_desc is not None and self._painter is not None:
            try:
                self._painter.cv.itemconfigure(self.m_preset_desc,
                                               text=LOUD_PRESETS[0][2])
            except Exception:
                pass

    def _on_preset(self, i):
        name, spec, _desc = LOUD_PRESETS[i]
        if spec:
            for k, v in spec.items():
                self.config.set(k, v)
            self.config.set("loud_preset", name)
        else:
            # 「自定义」= 显式的"我不跟预设"，不写任何参数，但要记住这个选择
            self.config.set("loud_preset", "custom")
        self._mark_change()
        # 重画一次把各字段的数值/禁用态刷新到新预设（延后到本事件处理完再重建）
        self.root.after(10, self.render)

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

    def _on_rdp(self, v):
        self._set("rdp_takeover", bool(v))
        self._set_foot("远程桌面中也会接管声音" if v else
                       "远程桌面中不再接管声音（声音原样直通）",
                       self.pal["sub"], hold=3.0)

    def _on_winvol(self, v):
        self._set("use_windows_volume", bool(v))
        self.root.after(10, lambda: self.engine.request_reload())

    def _on_winvol_ref(self, v):
        self._set("winvol_ref", float(v) / 100.0)

    def _on_winvol_max(self, v):
        self._set("winvol_max_db", float(v))

    def _on_autostart(self, on):
        ok = autostart.set_enabled(bool(on))
        self._set_foot("开机自启已开启" if ok and on else
                       ("开机自启已关闭" if ok else "开机自启设置失败"),
                       self.pal["sub"] if ok else self.pal["err"], hold=4.0)
        if not ok:
            # 写注册表失败时开关已经翻过去了，不重画会一直显示错误的状态
            self.root.after(10, self.render)

    def _on_block(self, v):
        """缓冲改动要重建会话才生效；拖动时别每格都重连，停手 0.6 秒再应用。"""
        self._set("block_ms", float(v))
        # 延迟估算就写在滑块下面，拖的时候不跟着变会让人以为没生效
        if self.m_lat is not None and self._painter is not None:
            try:
                self._painter.cv.itemconfigure(
                    self.m_lat,
                    text="当前端到端延迟约 %.0f ms（采集/队列 2 块 + 设备缓冲）"
                         % _latency_ms(float(v)))
            except Exception:
                pass
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
        self._set_foot("已应用缓冲设置，正在重连…", self.pal["warn"], hold=4.0)

    # ---- 名单 ----
    def _fill_list(self):
        lb = getattr(self, "m_list", None)
        if lb is None:
            return
        try:
            keep = {lb.get(i) for i in lb.curselection()}
            lb.delete(0, "end")
            items = sorted(self.config.get("mute_games", []) or [], key=str.lower)
            for i, g in enumerate(items):
                lb.insert("end", g)
                if g in keep:
                    lb.selection_set(i)
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
        self._set("mute_games", sorted(games, key=str.lower))
        self.var_newgame.set("")
        self._fill_list()

    def _del_game(self):
        lb = getattr(self, "m_list", None)
        if lb is None:
            return
        sel = list(lb.curselection())
        if not sel:
            return
        picked = {lb.get(i) for i in sel}
        games = [g for g in (self.config.get("mute_games", []) or [])
                 if g not in picked]
        self._set("mute_games", sorted(games, key=str.lower))
        # 核心进程每 0.3 秒按新名单 tick 一次，被删掉的程序会自动还原音量
        self._fill_list()

    def _reset_games(self):
        from config import DEFAULTS
        self._set("mute_games", sorted(DEFAULTS["mute_games"], key=str.lower))
        self._fill_list()

    # ---- 设备 ----
    def _rescan(self, silent=False):
        try:
            self._inputs = self.engine.list_inputs()
            self._outputs = self.engine.list_outputs()
        except Exception:
            self._inputs, self._outputs = [], []

    def _rescan_click(self):
        # 真正让核心重新枚举一次设备（状态文件里的列表平时 15 秒才更新一次），
        # 界面这边只是等它刷新后把下拉框重建出来
        try:
            ipc.set_event(ipc.EV_RESCAN)
        except Exception:
            pass
        self._rescan()
        self.render()
        self.root.after(450, self._rescan_again)

    def _rescan_again(self):
        before = (list(self._inputs), list(self._outputs))
        self._rescan()
        if (list(self._inputs), list(self._outputs)) != before:
            self.render()

    def _apply_device(self):
        new_in = self.var_input.get()
        new_out = self.var_output.get()
        if (new_in == (self.config.get("input_device", "") or "")
                and new_out == (self.config.get("output_device", "") or "")):
            # 选的是同一个设备：没必要白白断一次声音
            self._set_foot("设备没有变化", self.pal["sub"], hold=2.0)
            return
        self.config.update({
            "input_device": new_in,
            "output_device": new_out,
        })
        self.engine.request_reload()
        self._mark_change()
        self._set_foot("已应用设备设置，正在重连…", self.pal["warn"], hold=4.0)

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
