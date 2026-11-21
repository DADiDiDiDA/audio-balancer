"""验证「远程桌面默认不接管」：不碰真实音频设备，只跑逻辑。

1) session 探测在本机应为「本机会话」，blocked() 随 takeover 变化；
2) Engine 在 blocked 时不打开任何设备，状态是 paused=rdp，且能随开关恢复；
3) DeviceGuard 交还默认输出时挑的设备（只读计算，不改系统默认）；
4) Config 的 DEFAULTS 里有 rdp_takeover 且默认 False（老配置也取到 False）。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import session
import config as config_mod
from config import Config, DEFAULTS
from engine import Engine
from device_guard import DeviceGuard

FAIL = []


def check(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))
    if not cond:
        FAIL.append(name)


print("== 1. session ==")
check("session.console_detached() 返回 bool", isinstance(session.console_detached(), bool))
check("session.blocked(True) 恒为 False", session.blocked(True) is False)
print("   describe:", session.describe(), "| connect_state=", session.connect_state())

print("== 2. config ==")
check("DEFAULTS 有 rdp_takeover", "rdp_takeover" in DEFAULTS)
check("默认 False", DEFAULTS.get("rdp_takeover") is False)
tmp = tempfile.mkdtemp()
old = os.path.join(tmp, "old.json")
with open(old, "w", encoding="utf-8") as f:
    json.dump({"enabled": True}, f)          # 老配置：没有这个键
c = Config(path=old)
check("老配置读出来是 False", c.get("rdp_takeover") is False, c.get("rdp_takeover"))

print("== 3. engine 暂停 ==")
cfg = Config(path=os.path.join(tmp, "e.json"))
calls = {"open": 0}
orig_blocked = session.blocked
session.blocked = lambda takeover=False: (not takeover)   # 假装在远程桌面


def boom(*a, **k):
    calls["open"] += 1
    raise RuntimeError("不该打开设备")


eng = Engine(cfg)
eng._pick_input = boom
eng._pick_output = boom
eng.start()
t0 = time.time()
while time.time() - t0 < 2.0 and not eng.status.get("paused"):
    time.sleep(0.05)
check("暂停状态 paused=rdp", eng.status.get("paused") == "rdp", eng.status.get("paused"))
check("running=False", eng.status.get("running") is False)
check("没有打开任何设备", calls["open"] == 0, calls)
check("error 为空", not eng.status.get("error"), eng.status.get("error"))

cfg.set("rdp_takeover", True)     # 打开「接管远程桌面」→ 应该立刻尝试建链
t0 = time.time()
while time.time() - t0 < 2.5 and calls["open"] == 0:
    time.sleep(0.05)
check("打开接管后开始建链", calls["open"] > 0, calls["open"])
check("paused 已清掉", eng.status.get("paused") is None, eng.status.get("paused"))
eng.stop()

print("== 4. 运行中被挤掉 ==")
cfg2 = Config(path=os.path.join(tmp, "e2.json"))
cfg2.set("rdp_takeover", False)
state = {"blocked": False}
session.blocked = lambda takeover=False: (not takeover) and state["blocked"]
eng2 = Engine(cfg2)
opened = {"n": 0}


def boom2(*a, **k):
    opened["n"] += 1
    raise RuntimeError("假设备")
eng2._pick_input = boom2
eng2._pick_output = boom2
eng2._rdp_blocked = lambda c: session.blocked(bool(c.get("rdp_takeover", False)))
eng2.start()
time.sleep(0.5)
check("先行运行（blocked=False）会尝试建链", opened["n"] > 0, opened["n"])
state["blocked"] = True
time.sleep(1.2)
check("拔掉后进入 paused=rdp", eng2.status.get("paused") == "rdp", eng2.status.get("paused"))
eng2.stop()

session.blocked = orig_blocked

print("== 5. guard 交还目标（只读） ==")
g = DeviceGuard(cfg, None)
tgt = g._release_target()
check("能挑出交还的真实设备", tgt is not None, tgt[1] if tgt else None)
check("挑的不是虚拟声卡", tgt is None or "cable" not in tgt[1].lower(), tgt[1] if tgt else None)
g._pre = tgt
check("优先用接管前的设备", g._release_target() == tgt)
print("   guard.status():", g.status())

print()
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), FAIL))
    sys.exit(1)
print("全部通过")
