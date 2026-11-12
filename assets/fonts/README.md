# 随包字体

| 文件 | 版本 | 来源 | 许可 |
|---|---|---|---|
| `PixelMplus12-Regular.ttf` | 2013.0602 | [itouhiro/PixelMplus](https://github.com/itouhiro/PixelMplus)（M+ BITMAP FONTS 轮廓化） | M+ FONT LICENSE（`LICENSE-PixelMplus12.txt`） |
| `unifont.otf` | GNU Unifont 16.0.04 | https://unifoundry.com/unifont/ · [GNU 发行文件](https://ftp.gnu.org/gnu/unifont/unifont-16.0.04/unifont-16.0.04.otf) | SIL OFL 1.1，或 GPLv2+（含字体嵌入例外）；随包按 OFL 分发（`LICENSE-Unifont.txt`） |

校验和（`sha256sum -c SHA256SUMS` 在本目录执行）：

| 文件 | SHA-256 |
|---|---|
| `PixelMplus12-Regular.ttf` | `02f19467ea7cc235cc06c570b7f6c3b0a12a6f682bc8d74c43f2d323d97bcd12` |
| `unifont.otf` | `0e3981ab552231b5a2a870f2b61741903a4bf25c23ef5aeb05fdced1b3c7af4d` |

`unifont.otf` 与 GNU 上游 `unifont-16.0.04.otf` 字节一致（SHA-256 已比对）。
PixelMplus12 的版权声明为：

    Copyright (C) 2013 itouhiro
    Copyright (C) 2002-2013 M+ FONTS PROJECT

Unifont 的版权声明（来自字体 name 表，已填入 `LICENSE-Unifont.txt` 的 OFL 一节）：

    Copyright © 1998-2025 Roman Czyborra, Paul Hardy, Qianqian Fang,
    Andrew Miller, Johnnie Weaver, David Corbett, Nils Moskopp,
    Rebecca Bettencourt, Ho-Seok Ee, et al.

两个字体只被 `dist/` 启动器通过私有 `FONTCONFIG_FILE` 使用，不会安装到系统字体目录，
也不适用本项目的 MIT 许可。
