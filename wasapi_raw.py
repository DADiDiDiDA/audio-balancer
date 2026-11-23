"""WASAPI 原始模式（AUDCLNT_STREAMOPTIONS_RAW）播放器。

为什么需要它：
  共享模式的音频流在到达驱动前会经过 SFX/MFX/EFX 三层 APO。本机耳机端点上挂了
      NahimicAPO4.dll        （Nahimic 虚拟环绕）
      voiceclaritycpuapo.dll （Voice Clarity：把人声提出来、强压缩、加强中频）
      RltkAPOU642.dll        （Realtek APO）
  对音乐来说这等于"把歌当语音处理"——听感就是能听出歌词但音质彻底变成电音。
  设了 RAW 之后，数据跳过整条 APO 链，直达驱动，与 C++ 播放器走独占时的听感一致。

  这是 Windows 10 1703+ 的官方机制，不需要改注册表，也不独占设备。

用法：
    with RawPlayer(device_name, 48000, 2) as p:
        p.play(frames_2d_float32)
"""
import ctypes
import time
from ctypes import (POINTER, Structure, byref, c_int, c_int64, c_uint16,
                    c_uint32, c_void_p)

import numpy as np
from comtypes import COMMETHOD, CLSCTX_ALL, GUID, HRESULT, IUnknown

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_NOPERSIST = 0x00080000
AUDCLNT_STREAMOPTIONS_RAW = 0x2
AUDIO_STREAM_CATEGORY_MEDIA = 11
REFTIMES_PER_SEC = 10_000_000
BUFFER_MS = 200
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


class WAVEFORMATEX(Structure):
    # Windows 头文件里这两个结构都是 pack(1)：WAVEFORMATEX 正好 18 字节。
    # 不打包的话 ctypes 会按 4 字节对齐补 2 字节，后面 SubFormat 就整体错位。
    _pack_ = 1
    _fields_ = [("wFormatTag", c_uint16),
                ("nChannels", c_uint16),
                ("nSamplesPerSec", c_uint32),
                ("nAvgBytesPerSec", c_uint32),
                ("nBlockAlign", c_uint16),
                ("wBitsPerSample", c_uint16),
                ("cbSize", c_uint16)]


class WAVEFORMATEXTENSIBLE(Structure):
    _pack_ = 1
    _fields_ = [("Format", WAVEFORMATEX),
                ("wValidBitsPerSample", c_uint16),
                ("dwChannelMask", c_uint32),
                ("SubFormat", ctypes.c_ubyte * 16)]


class AudioClientProperties(Structure):
    _fields_ = [("cbSize", c_uint32),
                ("bIsOffload", c_int),
                ("eCategory", c_int),
                ("Options", c_int)]


class IAudioRenderClient(IUnknown):
    _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["in"], c_uint32, "NumFramesRequested"),
                  (["out"], POINTER(c_void_p), "ppData")),
        COMMETHOD([], HRESULT, "ReleaseBuffer",
                  (["in"], c_uint32, "NumFramesWritten"),
                  (["in"], c_uint32, "dwFlags")),
    ]


class IAudioClient2(IUnknown):
    _iid_ = GUID("{726778CD-F60A-4eda-82DE-E47610CD78AA}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], c_int, "ShareMode"),
                  (["in"], c_uint32, "StreamFlags"),
                  (["in"], c_int64, "hnsBufferDuration"),
                  (["in"], c_int64, "hnsPeriodicity"),
                  (["in"], c_void_p, "pFormat"),
                  (["in"], c_void_p, "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize",
                  (["out"], POINTER(c_uint32), "p")),
        COMMETHOD([], HRESULT, "GetStreamLatency",
                  (["out"], POINTER(c_int64), "p")),
        COMMETHOD([], HRESULT, "GetCurrentPadding",
                  (["out"], POINTER(c_uint32), "p")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], c_int, "ShareMode"),
                  (["in"], c_void_p, "pFormat"),
                  (["out"], POINTER(c_void_p), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat",
                  (["out"], POINTER(c_void_p), "pp")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(c_int64), "a"),
                  (["out"], POINTER(c_int64), "b")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "h")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(c_void_p), "ppv")),
        COMMETHOD([], HRESULT, "IsOffloadCapable",
                  (["in"], c_int, "cat"), (["out"], POINTER(c_int), "p")),
        COMMETHOD([], HRESULT, "SetClientProperties", (["in"], c_void_p, "p")),
        COMMETHOD([], HRESULT, "GetBufferSizeLimits",
                  (["in"], c_void_p, "pf"), (["in"], c_int, "ev"),
                  (["out"], POINTER(c_int64), "a"),
                  (["out"], POINTER(c_int64), "b")),
    ]


def _find_device(name):
    from pycaw.pycaw import AudioUtilities
    candidates = []
    for d in AudioUtilities.GetAllDevices():
        try:
            if int(getattr(d.state, "value", d.state)) != 1:
                continue
        except Exception:
            continue
        fn = d.FriendlyName or ""
        if fn == name:
            return d
        if name and (name in fn or fn in name):
            candidates.append(d)
    return candidates[0] if candidates else None


# KSDATAFORMAT_SUBTYPE_IEEE_FLOAT：{00000003-0000-0010-8000-00AA00389B71}
_FLOAT_SUBFORMAT = bytes([0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00,
                          0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71])


def describe_mix_format(pf):
    """读取 WAVEFORMATEX，返回 (是否 float32, 声道数, 采样率, 位深, formatTag)。"""
    fmt = ctypes.cast(pf, POINTER(WAVEFORMATEX)).contents
    tag = int(fmt.wFormatTag)
    is_float = tag == WAVE_FORMAT_IEEE_FLOAT
    if tag == WAVE_FORMAT_EXTENSIBLE and int(fmt.cbSize) >= 22:
        ext = ctypes.cast(pf, POINTER(WAVEFORMATEXTENSIBLE)).contents
        is_float = bytes(ext.SubFormat) == _FLOAT_SUBFORMAT
    return (is_float, int(fmt.nChannels), int(fmt.nSamplesPerSec),
            int(fmt.wBitsPerSample), tag)


class RawPlayer:
    """最小可用的 RAW 播放器：只支持 2 声道 float32，格式取设备混音格式。"""

    def __init__(self, device_name, samplerate=48000, channels=2, buffer_ms=BUFFER_MS):
        self.device_name = device_name
        self.samplerate = samplerate
        self.channels = channels
        self.buffer_ms = buffer_ms
        self._client = None
        self._render = None
        self._bufsize = 0
        self._started = False
        self.used_raw = False
        self.mix_format = None      # (is_float, channels, rate, bits, tag)
        # 欠载诊断：starves = 进 play() 时缓冲已空（真的欠载）；min_pad_ms = 入口最小剩余
        self.starves = 0
        self.writes = 0
        self.min_pad_ms = 1e9

    def open(self):
        dev = _find_device(self.device_name)
        if dev is None:
            raise RuntimeError("找不到输出设备：%s" % self.device_name)

        client = dev._dev.Activate(IAudioClient2._iid_, CLSCTX_ALL, None)
        client = client.QueryInterface(IAudioClient2)

        # 先尝试 RAW；不支持时降级为普通共享模式（至少还能出声）
        try:
            props = AudioClientProperties(cbSize=ctypes.sizeof(AudioClientProperties),
                                          bIsOffload=0,
                                          eCategory=AUDIO_STREAM_CATEGORY_MEDIA,
                                          Options=AUDCLNT_STREAMOPTIONS_RAW)
            client.SetClientProperties(byref(props))
            self.used_raw = True
        except Exception:
            self.used_raw = False

        pf = client.GetMixFormat()
        try:
            is_float, ch, rate, bits, tag = describe_mix_format(pf)
            self.mix_format = (is_float, ch, rate, bits, tag)
            # 我们只按 float32 / 立体声 / 48k 写裸内存。设备混音格式对不上就直接放弃
            # RAW（交给 soundcard 共享模式做转换）：以前不检查，遇到 24bit 或 6 声道
            # 的端点会把一块 float32 直接塞进去 —— 声音变调、变噪。
            if not is_float or bits != 32 or ch != self.channels or rate != self.samplerate:
                raise RuntimeError(
                    "混音格式不适合 RAW 直写（%.0f/%.0f bit/%d Hz/%d ch）"
                    % (float(is_float), bits, rate, ch))
            hr = client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                                   AUDCLNT_STREAMFLAGS_NOPERSIST,
                                   self.buffer_ms * (REFTIMES_PER_SEC // 1000),
                                   0, pf, None)
        finally:
            try:
                ctypes.windll.ole32.CoTaskMemFree(pf)
            except Exception:
                pass
        if hr != 0:
            raise RuntimeError("Initialize 失败：0x%08X" % (hr + 2 ** 32))

        self._bufsize = int(client.GetBufferSize())
        ptr = client.GetService(IAudioRenderClient._iid_)
        self._render = ctypes.cast(ptr, POINTER(IAudioRenderClient))
        self._client = client
        client.Start()
        self._started = True
        return self

    def play(self, data):
        """写入一帧数组 (frames, channels) float32；阻塞直到全部写进缓冲。"""
        if self._client is None:
            raise RuntimeError("播放器未打开")
        arr = np.ascontiguousarray(data, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr[:, None]
        frames = arr.shape[0]
        pos = 0
        deadline = time.time() + 5.0
        # 入口剩余量：这才是"欠载"判据（缓冲写满只是正常的流控）
        try:
            pad0_ms = int(self._client.GetCurrentPadding()) * 1000.0 / float(self.samplerate)
            if self.writes > 0 and pad0_ms < self.min_pad_ms:
                self.min_pad_ms = pad0_ms
            if pad0_ms <= 0.0 and self.writes > 0:
                self.starves += 1       # 首次调用时缓冲本来就是空的，不算欠载
            self.writes += 1
        except Exception:
            pass
        while pos < frames:
            pad = int(self._client.GetCurrentPadding())
            avail = self._bufsize - pad
            left = frames - pos
            if avail < left and left <= self._bufsize:
                # 缓冲还没腾出「一整块」的空间：一次睡够，别 3ms 醒一次。
                # 每次 sleep 本身约 40µs CPU（线程切换），26ms 缓冲 + 10ms 块
                # 的情况下原来要醒 3~4 次，光这一项就吃掉 1% 以上 CPU。
                # 只在缓冲满（会欠载的反面）时才会走到这里，睡多久都不会丢音。
                if time.time() > deadline:
                    return          # 别死等，丢这一块也比卡死强
                need = left - max(avail, 0)
                time.sleep(min(max(need / float(self.samplerate), 0.001), 0.02))
                continue
            if avail <= 0:
                if time.time() > deadline:
                    return
                time.sleep(0.003)
                continue
            take = min(avail, left)
            dest = self._render.GetBuffer(take)
            chunk = arr[pos:pos + take]
            # 直接拿 numpy 的裸指针 memmove：原来 chunk.tobytes() 每次都多分配
            # 一份 ~4KB 的字节串，100 块/秒就是纯垃圾
            ctypes.memmove(dest, chunk.ctypes.data, take * self.channels * 4)
            self._render.ReleaseBuffer(take, 0)
            pos += take
            deadline = time.time() + 5.0

    def close(self):
        try:
            if self._client is not None and self._started:
                self._client.Stop()
        except Exception:
            pass
        self._started = False
        self._render = None
        self._client = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False
