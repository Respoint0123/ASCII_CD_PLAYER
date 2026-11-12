#!/usr/bin/env python3
"""asciicd —— 控制台 CD-DA 播放器：渐进抓轨 + 字符可视化。

    python3 asciicd.py                 # 播光驱里的 CD
    python3 asciicd.py --fake          # 无光驱演示（合成音乐）
    python3 asciicd.py --layout mini   # 极简 3–5 行布局
    python3 asciicd.py --theme jpop    # phosphor / amber / jpop / modern
"""
from __future__ import annotations

import argparse
import os
import sys

from config import VERSION


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="asciicd",
        description="控制台 CD-DA 播放器：边抓轨边播放，带频谱/示波器可视化",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--device", default=None, help="光驱设备，如 /dev/sr0（默认自动找）")
    p.add_argument("--track", type=int, default=None, help="从第几轨开始")
    p.add_argument("--fake", action="store_true", help="不碰光驱，用合成音乐演示")
    p.add_argument("--speed", type=float, default=8.0, help="--fake 时模拟的抓轨倍速")
    p.add_argument("--theme", default=None,
                   help="phosphor / amber / jpop / modern；不填就用上次退出的配色（初始 phosphor）")
    p.add_argument("--minimal", action="store_true", help="极简布局（同 --layout mini）")
    p.add_argument("--paused", action="store_true",
                   help="启动后停在停止态（开机自启用）；按空格才开始抓轨/播放")
    p.add_argument("--wait-disc", action="store_true",
                   help="没光盘时停在等待画面，插入后进入播放器；数据 CD/DVD 或读不出的盘会明确提示，换回音频 CD 后继续（是否开播由 --on-insert 决定）")
    p.add_argument("--layout", default="dash", choices=["dash", "mini"],
                   help="dash=仪表盘（默认）, mini=极简顶栏")
    p.add_argument("--volume", type=float, default=0.85, help="初始音量 0–1.5")
    p.add_argument("--cache-dir", default=None, help="抓轨缓存目录")
    p.add_argument("--no-metadata", action="store_true", help="不联网查曲名")
    p.add_argument("--fast-rip", action="store_true",
                   help="用 cdparanoia -Y（少一遍校验，首字节 2.6s vs 6.9s，烂盘慎用）")
    p.add_argument("--rip-mode", choices=["disc", "track", "next"], default=None,
                   help="抓轨范围：disc=整盘预抓；track=只抓正在播的这一轨；"
                        "next=本轨+下一轨（默认取 config.RIP_MODE）")
    p.add_argument("--on-eject", choices=["stop"], default=None,
                   help="兼容保留：开仓一律立即停歌")
    p.add_argument("--on-insert", choices=["play", "pause"], default=None,
                   help="插盘/启动：play=自动开始抓轨并播放；pause=停在停止态等空格"
                        "（默认 pause，取 config.INSERT_ACTION）")
    p.add_argument("--list-devices", action="store_true", help="列出光驱后退出")
    p.add_argument("--check-deps", action="store_true",
                   help="检查运行依赖（numpy / cdparanoia / pw-cat / kitty）后退出")
    p.add_argument("--version", action="version",
                   version=f"asciicd {VERSION}",
                   help="打印版本后退出")
    p.add_argument("--selftest", action="store_true", help="无终端自测（开发用）")
    return p


def _launcher() -> str:
    """用户实际拿来启动的那个入口：源码是 asciicd.py，分发包是 asciicd/.pyz。"""
    name = os.path.basename(sys.argv[0] or "") or "asciicd"
    return f"python3 {name}" if name.endswith(".py") else name


def _missing_dep(exc: ImportError) -> int:
    """缺 Python 依赖时别甩 traceback，给出可执行的下一步。"""
    missing = getattr(exc, "name", None) or "依赖"
    print(f"缺少运行依赖：{missing}", file=sys.stderr)
    if missing == "numpy":
        print("安装 NumPy：pacman -S python-numpy 或 apt install python3-numpy，",
              file=sys.stderr)
        print("也可以用 pip install numpy（注意别装进系统 Python）。", file=sys.stderr)
    print(f"运行 `{_launcher()} --check-deps` 查看完整清单和安装命令。",
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("需要 Python 3.10+", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)

    if args.list_devices:
        from cdda.device import find_devices
        devs = find_devices()
        print("\n".join(devs) if devs else "(没有找到 /dev/sr* 光驱)")
        return 0

    if args.check_deps:
        from services.deps import run_check
        return run_check(fake=args.fake)

    if args.selftest:
        try:
            from app import selftest
        except ImportError as exc:
            return _missing_dep(exc)
        return selftest()

    if not args.fake:
        from cdda.device import default_device
        if args.device is None:
            args.device = default_device()
            if args.device is None:
                print("没有找到光驱（/dev/sr*）。\n"
                      "  · 用 --fake 看演示\n"
                      "  · 或用 --device /dev/sr0 指定", file=sys.stderr)
                return 2

    try:
        from app import App
    except ImportError as exc:
        return _missing_dep(exc)
    return App(args).run()


if __name__ == "__main__":
    sys.exit(main())
