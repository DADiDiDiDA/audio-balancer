"""预设/自定义模式的回归验证（离屏，不碰声音）。

覆盖这次报的问题：
  1. 点「自定义」后高亮必须留在自定义（不需要先改数值）；
  2. 点某个预设后高亮到该预设，且参数真的写进配置；
  3. 手动改任一参数 → 自动变成自定义；
  4. 已经是某预设时再点它一次 → 参数被重新套用（Segment 必须重发回调）。
"""
import os
import queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import theme
theme.set_dpi_aware()
import tkinter as tk

from config import Config
from gui import LOUD_PRESETS, AppWindow

here = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(here, "_verify_preset_cfg.json")

fails = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


class FakeEngine:
    status = {"running": True, "error": None, "paused": None, "lufs": -24.0,
              "gain_db": 2.0, "out_mode": "raw", "win_vol": 0.3}
    def list_inputs(self): return []
    def list_outputs(self): return []
    def request_reload(self): pass
    def start(self): pass
    def stop(self): pass


class FakeMuter:
    in_game = False
    foreground = ""
    controlled = 0
    muted = []
    def restore(self): pass


class FakeGuard:
    def status(self): return {"default": "", "virtual": "", "error": None, "rdp": False}
    def stop(self): pass


if os.path.exists(CFG):
    os.remove(CFG)
theme.system_dark = lambda: False
cfg = Config(path=CFG)
root = tk.Tk()
root.geometry("940x790+40+40")
app = AppWindow(root, cfg, FakeEngine(), FakeMuter(), queue.Queue(), guard=FakeGuard())


def pump(sec=0.25):
    t0 = time.time()
    while time.time() - t0 < sec:
        root.update()
        time.sleep(0.02)


pump(0.3)
print("初始：loud_preset=%r 高亮=%d" % (cfg.get("loud_preset"), app._preset_index()))

# 1) 默认值正好等于「均衡」→ 高亮 2
check(app._preset_index() == 2, "默认参数应显示为「均衡」")

# 2) 点「自定义」→ 必须停在自定义（这是用户报的 bug）
app._on_preset(0)
pump()
check(app._preset_index() == 0, "点「自定义」后高亮必须停在自定义")
check(cfg.get("loud_preset") == "custom", "「自定义」要写进配置（重启后还在）")
check(cfg.get("target_lufs") == -18.0, "点自定义不应改动任何参数")

# 3) 再点「均衡」→ 回到预设
app._on_preset(2)
pump()
check(app._preset_index() == 2, "点「均衡」后高亮到均衡")
check(cfg.get("loud_preset") == "均衡", "配置里记录预设名")

# 4) 点「夜间」→ 参数被整套写入
app._on_preset(4)
pump()
check(app._preset_index() == 4, "点「夜间」后高亮到夜间")
check(abs(float(cfg.get("target_lufs")) + 23.0) < 1e-6, "夜间把目标响度写成 −23")
check(int(cfg.get("gain_attack_ms")) == 40, "夜间把跟随速度写成 40ms")

# 5) 手动微调 → 自动变自定义
app._on_target(-25.0)
pump(0.1)
check(app._preset_index() == 0, "改目标响度后应自动变自定义")
check(cfg.get("loud_preset") == "custom", "自动切自定义也要落进配置")

# 6) 已经是「夜间」的数值时再点一次「夜间」→ 必须重新套用（Segment 重发回调）
cfg.set("loud_preset", "夜间")
cfg.set("target_lufs", -25.0)          # 手动漂了
pump(0.1)
seg = app.m_preset_seg
hits = []
orig_pick = seg.on_pick
seg.on_pick = lambda i: hits.append(i)   # 只验证控件是否重发回调
cx = seg.x + 3 + seg.slot * (4 + 0.5)
seg._press(cx)
seg._press(cx)
seg.on_pick = orig_pick                   # 还原，后面的点击测试还要用真回调
check(hits == [4, 4], "重复点同一档也要重发回调（否则改完参数点不回来）")

# 7) 老配置（没有 loud_preset）→ 按数值反推
cfg.set("loud_preset", "")
for k, v in LOUD_PRESETS[1][1].items():
    cfg.set(k, v)
check(app._preset_index() == 1, "没有记录时按数值反推（老配置兼容）")

# 8) 真的用鼠标点（走命中测试 + 重绘），而不是直接调回调
class Ev:
    def __init__(self, x, y):
        self.x, self.y = x, y


def click_chip(i):
    seg = app.m_preset_seg
    cx = seg.x + 3 + seg.slot * (i + 0.5)
    app._press(Ev(cx, seg.y + seg.h / 2.0))
    pump(0.3)


app._goto("loudness")
pump(0.3)
click_chip(0)
check(app._preset_index() == 0 and cfg.get("loud_preset") == "custom",
      "鼠标点「自定义」生效（走完整命中测试）")
click_chip(3)
check(app._preset_index() == 3 and cfg.get("loud_preset") == "电影",
      "鼠标点「电影」生效")
click_chip(0)
check(app._preset_index() == 0, "再从预设点回「自定义」仍然生效")

app.hide()
root.destroy()
print("\n%s（%d 项失败）" % ("全部通过" if not fails else "有失败", len(fails)))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
