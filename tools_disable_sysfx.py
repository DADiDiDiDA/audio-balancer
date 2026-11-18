"""关闭「耳机」端点的系统音效链（Nahimic/Realtek APO），并重启音频服务生效。

PKEY_AudioEndpoint_Disable_SysFx = {1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5
  1 = 关闭系统效果（音频数据直达驱动，与 C++ 播放器走独占/无处理时的听感一致）
  0 = 启用（默认）

仅修改耳机端点，不改扬声器；并把原值写入备份文件便于恢复。
"""
import json
import os
import subprocess
import sys
import time
import winreg

sys.path.insert(0, ".")

from pycaw.pycaw import AudioUtilities

KEY = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5"
BASE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
BACKUP = "disable_sysfx_backup.json"


def find_headphone():
    for d in AudioUtilities.GetAllDevices():
        if int(getattr(d.state, "value", d.state)) == 1 and "耳机" in (d.FriendlyName or ""):
            return d
    return None


def main():
    dev = find_headphone()
    if dev is None:
        print("找不到耳机端点")
        return
    print("端点 =", dev.FriendlyName)
    print("id   =", dev.id)

    # pycaw 给的是端点 ID（{0.0.0.00000000}.{guid}），注册表子键名是纯 GUID
    guid = dev.id.split("}.")[-1] if "}." in dev.id else dev.id
    print("guid =", guid)
    path = r"%s\%s\Properties" % (BASE, guid)
    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                         winreg.KEY_READ | winreg.KEY_WRITE)

    old = None
    try:
        old = winreg.QueryValueEx(key, KEY)[0]
    except FileNotFoundError:
        old = None
    print("原值 =", old)

    with open(BACKUP, "w", encoding="utf-8") as f:
        json.dump({"endpoint": dev.FriendlyName, "id": dev.id, "old": old}, f,
                  ensure_ascii=False, indent=2)
    print("已备份到", BACKUP)

    winreg.SetValueEx(key, KEY, 0, winreg.REG_DWORD, 1)
    winreg.CloseKey(key)
    print("已设置 Disable_SysFx = 1")

    # 重启音频服务让端点重建
    print("重启音频服务…")
    for cmd in (["net", "stop", "Audiosrv", "/y"],
                ["net", "stop", "AudioEndpointBuilder", "/y"],
                ["net", "start", "AudioEndpointBuilder"],
                ["net", "start", "Audiosrv"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        print("  %-40s rc=%d %s" % (" ".join(cmd), r.returncode,
                                    (r.stdout or r.stderr).strip().splitlines()[-1:][0] if (r.stdout or r.stderr).strip() else ""))
        time.sleep(1.0)


main()
