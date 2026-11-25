# -*- coding: utf-8 -*-
"""v1.8.3 回归：配置不再被另一进程旧快照覆盖 + 静音恢复的所有路径。

用法：python verify_persist.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import muter as M  # noqa: E402
from config import Config  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def disk(p):
    return json.load(open(p, encoding="utf-8"))


print("== 1) 核心退出不覆盖界面刚改的设置 ==")
d = tempfile.mkdtemp(prefix="ab_persist_")
p = os.path.join(d, "config.json")
core = Config(p)
ui = Config(p)
ui.set("target_lufs", -23.0)
ui.set("pre_gain_db", -6.0)
ui.set("low_knee_db", 40.0)
ui.save_now()
core.save_now()                     # 核心退出：内存里还是启动时的旧值
v = disk(p)
check("目标响度保住", v["target_lufs"] == -23.0, v["target_lufs"])
check("处理前降低保住", v["pre_gain_db"] == -6.0, v["pre_gain_db"])
check("低电平拐点保住", v["low_knee_db"] == 40.0, v["low_knee_db"])

print("== 2) 界面退出不覆盖核心改的设备 ==")
ui2 = Config(p)
core2 = Config(p)
core2.set("output_device", "扬声器 (QCY H3S)")
core2.save_now()
ui2.save_now()                      # 界面的旧快照里还是耳机
v = disk(p)
check("输出设备以核心为准", v["output_device"] == "扬声器 (QCY H3S)", v["output_device"])
check("界面没碰过的响度还在", v["target_lufs"] == -23.0, v["target_lufs"])

print("== 3) 只合并自己改过的键 ==")
a = Config(p)
b = Config(p)
a.set("mute_enabled", False)
b.set("mute_bg_volume", 0.15)
b.save_now()
a.save_now()
v = disk(p)
check("a 的改动在", v["mute_enabled"] is False)
check("b 的改动也在", abs(v["mute_bg_volume"] - 0.15) < 1e-9, v["mute_bg_volume"])

print("== 4) 重启后读回的就是最后写入的值 ==")
fresh = Config(p)
check("重启后目标响度一致", fresh.get("target_lufs") == -23.0, fresh.get("target_lufs"))
check("重启后设备一致", fresh.get("output_device") == "扬声器 (QCY H3S)")
check("重启后静音开关一致", fresh.get("mute_enabled") is False)


# ---------------- 静音恢复 ----------------
class FakeVol:
    def __init__(self, v): self.v = v
    def GetMasterVolume(self): return self.v
    def SetMasterVolume(self, v, ctx=None): self.v = v


class FakeSession:
    def __init__(self, name, pid, vol, ident=None):
        self.ProcessId = pid
        self.DisplayName = name
        self.InstanceIdentifier = ident or ("id-" + name)
        self.SimpleAudioVolume = FakeVol(vol)

        class _P:
            def name(self_): return name
        self.Process = _P()


class FakeCfg:
    def __init__(self, d, path=""): self.d = d; self.path = path
    def snapshot(self): return dict(self.d)
    def get(self, k, default=None): return self.d.get(k, default)


def make_muter(games, cfg_path=""):
    cfg = {"mute_enabled": True, "mute_games": list(games), "mute_ignore": [],
           "mute_listed_in_background": True, "mute_others_in_game": False,
           "mute_fg_volume": 1.0, "mute_bg_volume": 0.0, "mute_easing_ms": 200}
    m = M.Muter(FakeCfg(cfg, cfg_path))
    m._self_pid = 999999
    return m, cfg


def pump(m, s, n=10, base=0.0, step=1.0):
    for i in range(n):
        m._last_enum = base + i * step
        m._sessions = [s] if s else []
        m.tick(base + i * step)


live_pid = os.getpid()
M.foreground_info = lambda: ("other.exe", 424242)

print("== 5) 删除名单后恢复音量 ==")
m, cfg = make_muter(["foo.exe"])
s = FakeSession("foo.exe", live_pid, 1.0)
pump(m, s, base=0)
check("前台不在时先被静音", s.SimpleAudioVolume.v < 0.01, s.SimpleAudioVolume.v)
cfg["mute_games"] = []
pump(m, s, base=100)
check("删除后恢复到原音量", abs(s.SimpleAudioVolume.v - 1.0) < 0.01, s.SimpleAudioVolume.v)

print("== 6) 会话标识变化（程序重开）后仍能恢复 ==")
m, cfg = make_muter(["foo.exe"])
s = FakeSession("foo.exe", live_pid, 1.0, "old")
pump(m, s, base=0)
s2 = FakeSession("foo.exe", live_pid, 0.0, "new")
cfg["mute_games"] = []
pump(m, s2, base=100)
check("新会话按 pid+名字认出并恢复", abs(s2.SimpleAudioVolume.v - 1.0) < 0.01,
      s2.SimpleAudioVolume.v)

print("== 7) 会话暂时消失再回来仍能恢复 ==")
m, cfg = make_muter(["foo.exe"])
s = FakeSession("foo.exe", live_pid, 1.0)
pump(m, s, base=0)
cfg["mute_games"] = []
pump(m, None, base=100)          # 枚举空档
pump(m, s, base=200)
check("空档后恢复", abs(s.SimpleAudioVolume.v - 1.0) < 0.01, s.SimpleAudioVolume.v)

print("== 8) 用户手动调过音量时以用户为准 ==")
m, cfg = make_muter(["foo.exe"])
s = FakeSession("foo.exe", live_pid, 1.0)
pump(m, s, base=0)
s.SimpleAudioVolume.v = 0.35      # 用户自己拖到 35%
pump(m, s, base=100)
cfg["mute_games"] = []
pump(m, s, base=200)
check("删除后保留用户的 35%", abs(s.SimpleAudioVolume.v - 0.35) < 0.01,
      s.SimpleAudioVolume.v)

print("== 9) 关闭总开关立即恢复 ==")
m, cfg = make_muter(["foo.exe"])
s = FakeSession("foo.exe", live_pid, 1.0)
pump(m, s, base=0)
cfg["mute_enabled"] = False
pump(m, s, base=100)
check("关开关后恢复", abs(s.SimpleAudioVolume.v - 1.0) < 0.01, s.SimpleAudioVolume.v)

print("== 10) 渐变时长按真实时间生效 ==")
m, cfg = make_muter(["foo.exe"])
cfg["mute_easing_ms"] = 1000
s = FakeSession("foo.exe", live_pid, 1.0)
m._last_enum = 0
m._sessions = [s]
m.tick(0.0)                       # 第一拍只记时间，不改变音量
check("第一拍不动音量", abs(s.SimpleAudioVolume.v - 1.0) < 1e-9, s.SimpleAudioVolume.v)
m.tick(0.1)                       # 100ms 后应只走约 10%
check("1s 渐变在 100ms 时只走一小段", 0.85 < s.SimpleAudioVolume.v < 0.95,
      s.SimpleAudioVolume.v)
for i in range(2, 60):           # 约 5.8 秒，5 个时间常数后应基本到位
    t = round(i * 0.1, 2)
    m._last_enum = t             # 别让假会话被 3 秒一次的重新枚举清掉
    m._sessions = [s]
    m.tick(t)
check("渐变大约 5 个时间常数后收敛到位", s.SimpleAudioVolume.v < 0.02,
      s.SimpleAudioVolume.v)

print("== 11) 前台时不覆盖用户自己调的音量 ==")
m, cfg = make_muter(["game.exe"])
M.foreground_info = lambda: ("game.exe", live_pid)
s = FakeSession("game.exe", live_pid, 0.30)
pump(m, s, base=0)
check("前台 100% 时保持用户的 30%", abs(s.SimpleAudioVolume.v - 0.30) < 0.01,
      s.SimpleAudioVolume.v)

m, cfg = make_muter(["game.exe"])
cfg["mute_fg_volume"] = 0.5
s = FakeSession("game.exe", live_pid, 0.30)
pump(m, s, base=0, n=30)
check("前台配 50% 时按用户的 30% 缩放", abs(s.SimpleAudioVolume.v - 0.15) < 0.02,
      s.SimpleAudioVolume.v)

m, cfg = make_muter(["game.exe"])
M.foreground_info = lambda: ("other.exe", 424242)
s = FakeSession("game.exe", live_pid, 0.30)
pump(m, s, base=0)
check("后台先压到 0", s.SimpleAudioVolume.v < 0.01, s.SimpleAudioVolume.v)
M.foreground_info = lambda: ("game.exe", live_pid)
pump(m, s, base=100, n=30)
check("回前台恢复成 30% 而不是 100%", abs(s.SimpleAudioVolume.v - 0.30) < 0.01,
      s.SimpleAudioVolume.v)

print("== 12) 热重载不覆盖本进程未落盘的改动 ==")
d2 = tempfile.mkdtemp(prefix="ab_dirty_")
p2 = os.path.join(d2, "config.json")
a = Config(p2)
a.save_now()                 # 先让磁盘上有一份完整配置
a.set("output_device", "新耳机")   # 本进程改了但还没落盘
# 模拟界面进程此刻写入了另一份配置（不含新耳机）
a2 = Config(p2)
a2.set("target_lufs", -21.0)
a2.save_now()
a.load()
check("未落盘的设备改动没被热重载覆盖",
      a.get("output_device") == "新耳机", a.get("output_device"))
check("别的键仍跟着磁盘更新", a.get("target_lufs") == -21.0, a.get("target_lufs"))

M.foreground_info = lambda: ("other.exe", 424242)

print("== 13) 残留的 0 不算用户音量（前台必须能出声）==")
M.foreground_info = lambda: ("mumunxdevice.exe", live_pid)
m, cfg = make_muter(["mumunxdevice.exe"])
s = FakeSession("mumunxdevice.exe", live_pid, 0.0)   # Windows 记住了上次被压的 0
pump(m, s, base=0)
check("前台残留 0 会恢复到可听音量", s.SimpleAudioVolume.v > 0.9,
      s.SimpleAudioVolume.v)

m, cfg = make_muter(["mumunxdevice.exe"])
s = FakeSession("mumunxdevice.exe", live_pid, 0.0)
M.foreground_info = lambda: ("other.exe", 424242)
pump(m, s, base=0)
M.foreground_info = lambda: ("mumunxdevice.exe", live_pid)
pump(m, s, base=100)
check("后台静音后回前台一定有声", s.SimpleAudioVolume.v > 0.9,
      s.SimpleAudioVolume.v)

print("== 14) 重启均衡器后仍能认出「那个 0 是我们压的」==")
d3 = tempfile.mkdtemp(prefix="ab_state_")
state = os.path.join(d3, "mute_state.json")
with open(state, "w", encoding="utf-8") as f:
    json.dump({"volumes": {"mumunxdevice.exe": 0.7}}, f)
m, cfg = make_muter(["mumunxdevice.exe"], os.path.join(d3, "config.json"))
s = FakeSession("mumunxdevice.exe", live_pid, 0.0)
M.foreground_info = lambda: ("mumunxdevice.exe", live_pid)
pump(m, s, base=0)
check("按状态文件恢复成用户原来的 0.7", abs(s.SimpleAudioVolume.v - 0.7) < 0.02,
      s.SimpleAudioVolume.v)

# 状态文件本身：压过就写、恢复后不会被清空
m, cfg = make_muter(["foo.exe"], os.path.join(d3, "config.json"))
M.foreground_info = lambda: ("other.exe", 424242)
s = FakeSession("foo.exe", live_pid, 0.45)
m._last_save = 0.0
pump(m, s, base=1000, n=6)
saved = json.load(open(state, encoding="utf-8"))["volumes"]
check("被压过的程序原音量写进状态文件", abs(saved.get("foo.exe", 0) - 0.45) < 0.01,
      saved)
cfg["mute_games"] = []
pump(m, s, base=2000, n=6)
saved2 = json.load(open(state, encoding="utf-8"))["volumes"]
check("恢复后状态文件里的记录不丢", "foo.exe" in saved2, saved2)
check("恢复成 0.45", abs(s.SimpleAudioVolume.v - 0.45) < 0.02, s.SimpleAudioVolume.v)

print("== 15) 原音量永远不记成 0 ==")
m, cfg = make_muter(["foo.exe"])
M.foreground_info = lambda: ("other.exe", 424242)
s = FakeSession("foo.exe", live_pid, 0.0)      # 已经是 0
pump(m, s, base=0)
recs = list(m._orig.values())
check("会话是 0 时不把 0 当原值", all(r[0] > 0.9 for r in recs) or not recs, recs)
M.foreground_info = lambda: ("foo.exe", live_pid)
pump(m, s, base=100)
check("切前台后有声音", s.SimpleAudioVolume.v > 0.9, s.SimpleAudioVolume.v)

M.foreground_info = lambda: ("other.exe", 424242)

print()
print(f"通过 {PASS} · 失败 {FAIL}")
sys.exit(1 if FAIL else 0)
