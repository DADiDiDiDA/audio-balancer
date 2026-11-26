# -*- coding: utf-8 -*-
"""进程名 → 这到底是什么程序。

静音名单里只有一串 StarRail.exe / MuMuNxDevice.exe，过一阵就忘了谁是谁。
优先查运行中进程 exe 的文件版本信息（ProductName / FileDescription），
拿不到就退回「exe 所在目录名」；没在运行就标「未运行」。
结果带 20 秒缓存：渲染界面时不必每次都遍历一遍进程表。
"""
import ctypes
import os
import time
from ctypes import wintypes

import psutil

TTL = 20.0
_cache = {"at": 0.0, "map": {}}

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


def _scan():
    out = {}
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
        except Exception:
            continue
        if not name or name in out:
            continue
        desc = ""
        if exe:
            # FileDescription 通常就是「MuMu模拟器」「微信」这种友好名；
            # ProductName 常常是「Microsoft® Windows® Operating System」这类通用串
            desc = (_version_string(exe, "FileDescription")
                    or _version_string(exe, "ProductName"))
            if not desc:
                desc = os.path.basename(os.path.dirname(exe)) or ""
        out[name] = {"desc": desc[:60], "path": exe}
    return out


def describe(names, force=False):
    """{进程名小写: {"text": 说明, "path": exe 路径}}；没运行 text = 「未运行」。"""
    names = [str(n).strip().lower() for n in (names or []) if str(n).strip()]
    now = time.time()
    if force or now - _cache["at"] > TTL:
        try:
            _cache["map"] = _scan()
        except Exception:
            _cache["map"] = {}
        _cache["at"] = now
    out = {}
    for n in names:
        rec = _cache["map"].get(n)
        if not rec:
            out[n] = {"text": "未运行", "path": ""}
        else:
            out[n] = {"text": rec["desc"] or "已运行", "path": rec["path"]}
    return out
