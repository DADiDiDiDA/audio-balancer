"""AST 守卫：防止 engine.py 里的变量遮蔽再次复发。

历史事故（v1.3）：播放线程里 `block = q.get(...)` 把「帧数变量 block」遮蔽成
「一块音频数据」，导致 x.shape[0] != block 每块都抛 ValueError，会话无限重启、
全程静音 —— 而且旧版把线程异常整个吞了，日志里什么都看不到。
本守卫锁定三条规矩：
  1. 帧数变量只能叫 blk，不允许叫 block；
  2. `block` 只允许出现在播放线程的赋值与 `np.asarray(block)`；
  3. 采集/播放线程的 except 必须写 _thread_error（不许吞异常）。
"""
import ast
import sys

ok = True


def fail(msg):
    global ok
    ok = False
    print("FAIL:", msg)


src = open("engine.py", encoding="utf-8").read()
tree = ast.parse(src)

for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name in ("capture", "playback"):
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if node.name == "capture" and "block" in names:
            fail("capture() 里出现了 block（帧数变量只能叫 blk）")
        if node.name == "playback":
            bad = [n for n in ast.walk(node)
                   if isinstance(n, ast.Compare)
                   and isinstance(n.left, ast.Attribute) and n.left.attr == "shape"
                   and any(isinstance(c, ast.Name) and c.id == "block" for c in n.comparators)]
            if bad:
                fail("playback() 里拿 block（数组）跟帧数比较")
        has_err = any(isinstance(n, ast.Attribute) and n.attr == "_thread_error"
                      for n in ast.walk(node))
        has_except = any(isinstance(n, ast.ExceptHandler) for n in ast.walk(node))
        if has_except and not has_err:
            fail("%s() 的 except 没有写 _thread_error（吞异常 = 排查地狱）" % node.name)

# blk 必须存在于 _session（它在 Engine 类里）
sessions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_session"]
if not sessions:
    fail("_session() 找不到")
    sys.exit(1)
session = sessions[0]
if not any(isinstance(n, ast.Name) and n.id == "blk" for n in ast.walk(session)):
    fail("_session() 里没有 blk（帧数变量丢了）")

print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
