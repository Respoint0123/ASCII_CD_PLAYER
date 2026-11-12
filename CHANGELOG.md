# Changelog

## v0.1.0 — 2026-11-12

首个公开版本：Linux 终端里的 CD-DA 播放器，边抓轨边播放。

### 功能

- 渐进抓轨：`cdparanoia` 在后台抓轨，缓存够一段就开播；默认预抓本轨 + 下一轨。
- 实时可视化：64 bin 频谱、瀑布图、Braille 示波器、立体声电平表。
- 两套布局（dash / mini）、四套配色（phosphor / amber / jpop / modern），主题选择会被记住。
- 车载 CD 机风格的状态机：开机面板、开仓、无盘、数据盘 / DVD、换盘、读盘失败都有明确画面。
- 曲名：MusicBrainz 精确 disc ID，查不到时用逐轨时长校验过的 TOC 回退；另有 CD-TEXT 和本地缓存兜底。
- 缓存安全：缓存目录带归属标记和每盘文件锁；同时开两个实例时，后启动的使用隔离缓存，不会删除别人的 PCM。
- `--fake`：不需要光驱和光盘，用合成音乐演示；`--selftest` 可做无终端实声自检。
- `install.sh`：复制到 `~/.local/share/asciicd`，安装命令软链和桌面菜单项，支持 `--add-path` 与 `--uninstall`。

### 运行要求

- Linux、Python 3.10+
- NumPy
- PipeWire（`pw-cat`）和运行中的用户 PipeWire 会话
- 播放实体 CD：音频光驱 + `cdparanoia`
- 可选：Kitty（独立点阵字体窗口）

### 已验证

- 开发期间在 Arch Linux + CF-SV8 内置光驱上实盘验证（抓轨、Seek、Stop、换盘、CD-TEXT、MusicBrainz）。
- Kitty 独立窗口与 PixelMplus12 / GNU Unifont 点阵字体。
- Python 3.10 与 3.14 的单元测试、构建产物验收和伪终端冒烟。

### 已知限制

- 仅 Linux；只支持 CD-DA 音频轨，数据盘 / DVD 会显示 UNSUPPORTED。
- 没有 PipeWire 会话时不会出声（`--fake` 也一样）。
- 曲目列表、MPRIS、Windows、无缝切轨、迷你播放动画留待后续版本。
