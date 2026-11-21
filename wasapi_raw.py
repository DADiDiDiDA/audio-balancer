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
from ctypes import POINTER, Structure, byref, c_int, c_int64, c_uint32, c_void_p

from comtypes import COMMETHOD, CLSCTX_ALL, GUID, HRESULT, IUnknown

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_NOPERSIST = 0x00080000
AUDCLNT_STREAMOPTIONS_RAW = 0x2
AUDIO_STREAM_CATEGORY_MEDIA = 11
REFTIMES_PER_SEC = 10_000_000
BUFFER_MS = 200


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
        hr = client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                               AUDCLNT_STREAMFLAGS_NOPERSIST,
                               self.buffer_ms * (REFTIMES_PER_SEC // 1000),
                               0, pf, None)
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
        import numpy as np
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
            if avail <= 0:
                if time.time() > deadline:
                    return          # 别死等，丢这一块也比卡死强
                time.sleep(0.003)
                continue
            take = min(avail, frames - pos)
            dest = self._render.GetBuffer(take)
            chunk = arr[pos:pos + take]
            ctypes.memmove(dest, chunk.tobytes(), take * self.channels * 4)
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
