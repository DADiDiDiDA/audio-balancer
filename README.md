# 音频均衡器（AudioBalancer）

一个常驻托盘的 Windows 音频工具：**响度均衡 + 自动静音**，一个程序、一个托盘、一个界面。

> 起因：看视频 / 打游戏时声音忽大忽小（爆炸吓人、对话听不清），
> 商业方案 AudioBalance 很好用但是闭源收费，于是自己用 Python 写了一个。

## 架构（v1.4）

```
音频均衡器.exe           核心进程：引擎 + 静音 + 设备守护 + 托盘，无窗口、无 Tk
音频均衡器.exe --ui      UI 进程：只跑界面，通过 JSON 状态文件 / 命名事件与核心通信
```

- 双击 exe：启动核心并拉起 UI；双击第二个 exe：只把已有 UI 叫到前台
- 开机自启 `--minimized`：**只启动核心**，不创建 UI 进程，省内存
- 关掉 UI 窗口：UI 进程真的退出；核心继续处理音频；托盘「打开主界面」再拉起
- 核心每 0.2s 原子写 `.audiobalancer_status.json`，UI 只读；UI 写 `config.json`，核心监视 mtime 热重载

## 功能

**响度均衡（BS.1770）**

- 从 VB-CABLE 虚拟声卡回环采集系统声音，按 ITU-R BS.1770 测量节目响度，
  再把响度送进目标区间，经软限幅后输出到耳机/扬声器。
- 两种处理方式：
  - **自动电平**（默认）：目标是「区间」而不是绝对值。低于范围就提上来（可限最大提升），
    高于范围就压下去（可限最大衰减），落在范围内完全不动 —— 不会对每一帧都追目标，听感稳。
  - **仅限制**：只压峰值，安静内容原样通过，绝不放大底噪。
- 处理前降低：给限幅器留余量，峰值老顶天花板的内容先降几 dB 再处理。
- **WASAPI RAW 输出**：绕过 Nahimic / Voice Clarity / Realtek APO 等"语音美化"音效，
  避免音乐被当成人声压缩成电音（本项目的原始动机）。

**自动静音（挂机静音）**

- 名单里的程序切到后台（最小化 / 挂机）→ 自动静音它自己（默认开）。
- 名单里的程序在前台 → 把其它程序压低（默认关，可选）。
- 只动会话音量，退出或关闭开关时按原值恢复；绝不碰系统提示音和 audiodg。

**自动接管默认输出**

- 在 Windows 里把默认输出换成耳机 → 本程序自动把它接成输出设备，
  再把系统默认改回虚拟声卡。下次换耳机只要在 Windows 里点一下。

**其它**

- 缓冲可调：默认 20ms，端到端延迟约 80ms（可在「音频设备」里调回 100ms 换稳定）
- **Windows 音量控制**：回环采集不受 CABLE 的音量滑块影响，引擎会主动读取它作为最终输出增益；
  启动时把 CABLE 设为 100%，之后直接用系统音量滑块调大小。音量是对数曲线：
  20% = 正常音量、100% = +10dB（都可调），低段细腻不再一拖就没声。
- 亚克力（毛玻璃）界面，跟随系统深色 / 浅色；左侧导航 + 卡片式设置。
- 任何改动立即生效并自动保存（右下角显示 保存中… / 已保存，仍可手动点）。
- 单实例：重复启动会把旧实例的窗口叫到前台，而不是再开一个。

## 音频链路

```
播放器 → CABLE Input（系统默认输出）
       → 本程序从 CABLE 回环采集
       → 去直流 + BS.1770 响度测量（测量在增益之前，闭环天然稳定）
       → 增益（自动电平 / 仅限制） + 软限幅
       → WASAPI RAW → 耳机
```

## 使用前提

1. Windows 10 / 11
2. 安装 [VB-CABLE](https://vb-audio.com/Cable/) 虚拟声卡
3. 系统默认输出设为「CABLE Input / CABLE In 16ch」

## 构建

```bash
pip install numpy soundcard pycaw comtypes psutil pillow pystray pyinstaller
python -m PyInstaller 音频均衡器.spec --noconfirm --clean
```

## 目录

```
src/
  main.py          核心入口：单实例、托盘、调度线程、配置热重载、状态发布
  ui_main.py       UI 进程入口：状态代理 + 按需启动
  ipc.py           状态文件 / 命名事件
  engine.py        音频引擎：采集 → 测量 → 增益+音量曲线 → 限幅 → RAW 播放
  loudness.py      K 加权 + 滑动 LUFS
  dsp.py           直流阻塞等
  wasapi_raw.py    IAudioClient2 + AUDCLNT_STREAMOPTIONS_RAW 播放器
  muter.py         自动静音（会话音量）
  device_guard.py  默认输出接管（IPolicyConfig）
  theme.py         亚克力 + 深浅色 + DPI
  ui_kit.py        自绘控件（开关 / 滑块 / 分段 / 按钮）
  gui.py           界面
  config.py        配置读写（原子写入 + 防抖自动保存）
```

## License

MIT
