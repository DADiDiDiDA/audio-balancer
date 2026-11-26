# -*- coding: utf-8 -*-
"""进程名 → 这到底是什么程序。

静音名单里只有一串 StarRail.exe / MuMuNxDevice.exe，过一阵就忘了谁是谁。
优先查运行中进程 exe 的文件版本信息（ProductName / FileDescription），
拿不到就退回「exe 所在目录名」；没在运行就标「未运行」。
结果带 20 秒缓存：渲染界面时不必每次都遍历一遍进程表。
"""
import ctypes
import json
import os
import time
from ctypes import wintypes

import psutil

TTL = 20.0
_cache = {"at": 0.0, "map": {}}
_known = {}          # 进程名 -> {"path": exe, "desc": 说明}：见过一次就记住
_known_path = ""
_known_dirty = False

_ver = None


def _api():
    global _ver
    if _ver is None:
        v = ctypes.windll.version
        v.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,
                                              ctypes.POINTER(wintypes.DWORD)]
        v.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        v.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                          wintypes.DWORD, ctypes.c_void_p]
        v.GetFileVersionInfoW.restype = wintypes.BOOL
        v.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                     ctypes.POINTER(ctypes.c_void_p),
                                     ctypes.POINTER(wintypes.UINT)]
        v.VerQueryValueW.restype = wintypes.BOOL
        _ver = v
    return _ver


def _version_string(path, key="ProductName"):
    """读 exe 的版本资源字符串；读不到返回空串。"""
    try:
        api = _api()
        dummy = wintypes.DWORD(0)
        size = api.GetFileVersionInfoSizeW(path, ctypes.byref(dummy))
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr = ctypes.c_void_p()
        ln = wintypes.UINT(0)
        if not api.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                                  ctypes.byref(ptr), ctypes.byref(ln)):
            return ""
        words = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD))
        for i in range(max(1, ln.value // 4)):
            lang, cp = words[i * 2], words[i * 2 + 1]
            sub = "\\StringFileInfo\\%04x%04x\\%s" % (lang, cp, key)
            p2 = ctypes.c_void_p()
            l2 = wintypes.UINT(0)
            if api.VerQueryValueW(buf, sub, ctypes.byref(p2), ctypes.byref(l2)) and l2.value:
                text = ctypes.wstring_at(p2, l2.value).replace("\x00", "").strip()
                if text:
                    return text
        return ""
    except Exception:
        return ""


def _known_file(folder):
    return os.path.join(folder, "procdesc_cache.json") if folder else ""


def _load_known(folder):
    global _known, _known_path
    p = _known_file(folder)
    if not p or p == _known_path:
        return
    _known_path = p
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _known = {str(k).lower(): v for k, v in data.items()
                      if isinstance(v, dict)}
    except Exception:
        _known = {}


def _save_known():
    """记住见过的 exe：程序没启动时也能显示它是什么（最多 5 分钟写一次）。"""
    global _known_dirty
    if not _known_dirty or not _known_path:
        return
    now = time.time()
    if now - getattr(_save_known, "_at", 0.0) < 300.0:
        return
    _save_known._at = now
    try:
        tmp = _known_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_known, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _known_path)
        _known_dirty = False
    except Exception:
        pass


def _scan(wanted=None):
    """遍历进程表；只给 wanted 里的进程读版本资源。

    读 exe 的文件属性要走一次 Windows 版本 API，机器上开着两百个进程时
    逐个读能让「打开自动静音页」卡上一两百毫秒。名单通常只有二十来条，
    所以只查需要的那些。
    """
    wanted = {str(n).lower() for n in (wanted or [])}
    out = {}
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
        except Exception:
            continue
        if not name or name in out:
            continue
        if wanted and name not in wanted:
            continue
        desc = ""
        if exe:
            # FileDescription 通常就是「MuMu模拟器」「微信」这种友好名；
            # ProductName 常常是「Microsoft® Windows® Operating System」这类通用串
            desc = (_version_string(exe, "FileDescription")
                    or _version_string(exe, "ProductName"))
            if not desc:
                desc = os.path.basename(os.path.dirname(exe)) or ""
        rec = {"desc": desc[:60], "path": exe}
        out[name] = rec
        if exe and _known.get(name, {}).get("path") != exe:
            _known[name] = rec
            globals()["_known_dirty"] = True
    return out


def describe(names, force=False, cache_dir=""):
    """{进程名小写: {"text": 说明, "path": exe 路径}}。

    正在跑的进程优先；没在跑就翻「以前见过」的记录（exe 路径还在就连版本说明
    一起复用），实在没有才显示「未运行」。
    """
    names = [str(n).strip().lower() for n in (names or []) if str(n).strip()]
    _load_known(cache_dir)
    now = time.time()
    # 只扫「这一批名字」：名单增删后能立刻认出新加的程序，
    # 又不会因为没在运行的程序反复重扫（它们也记进 scanned）。
    fresh = set(names) <= _cache.get("scanned", set())
    if force or not fresh or now - _cache["at"] > TTL:
        try:
            _cache["map"] = _scan(names)
        except Exception:
            _cache["map"] = {}
        _cache["at"] = now
        _cache["scanned"] = set(names)
    out = {}
    for n in names:
        rec = _cache["map"].get(n)
        if rec:
            out[n] = {"text": rec["desc"] or "已运行", "path": rec["path"]}
            continue
        old = _known.get(n) or {}
        desc = str(old.get("desc") or "").strip()
        path = str(old.get("path") or "")
        if not desc and path and os.path.exists(path):
            desc = (_version_string(path, "FileDescription")
                    or _version_string(path, "ProductName")
                    or os.path.basename(os.path.dirname(path)) or "")[:60]
            if desc:
                old["desc"] = desc
                globals()["_known_dirty"] = True
        out[n] = {"text": desc or "未运行", "path": path}
    _save_known()
    return out
