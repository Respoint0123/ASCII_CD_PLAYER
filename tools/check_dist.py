#!/usr/bin/env python3
"""验收分发包：文件、权限、字体、许可、归档、CLI 版本。

CI 在 `tools/build_dist.py` 之后调用；本地也可以 `make build && make check-dist`：

    python3 tools/check_dist.py [--out dist] [--skip-cli]
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from build_dist import ARCHIVE_NAME, LEGACY_ARCHIVES, PACKAGE_NAME  # noqa: E402
from config import VERSION  # noqa: E402

REQUIRED_FILES = ("asciicd.pyz", "asciicd", "asciicd.desktop",
                  "install.sh", "README.txt", "LICENSE")
REQUIRED_FONT_FILES = ("PixelMplus12-Regular.ttf", "unifont.otf",
                       "LICENSE-PixelMplus12.txt", "LICENSE-Unifont.txt",
                       "README.md", "SHA256SUMS")
EXECUTABLES = ("asciicd", "asciicd.pyz", "install.sh")
ZIPAPP_MODULES = ("config.py", "textfmt.py", "app.py", "cdda/cache.py",
                  "services/metadata.py")


def _readable(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def layout_errors(out: Path, require_fonts: bool = True) -> list[str]:
    """目录里该有的东西是否齐全、权限对不对、许可与字体校验和是否一致。"""
    errors: list[str] = []
    for name in REQUIRED_FILES:
        if not (out / name).is_file():
            errors.append(f"缺少 {name}")
    if "MIT License" not in _readable(out / "LICENSE"):
        errors.append("LICENSE 不像 MIT 许可文本")
    for name in EXECUTABLES:
        path = out / name
        if path.is_file() and not path.stat().st_mode & 0o111:
            errors.append(f"{name} 没有可执行位")
    for name in LEGACY_ARCHIVES:
        if (out / name).exists():
            errors.append(f"残留旧版归档 {name}")

    pyz = out / "asciicd.pyz"
    if pyz.is_file():
        try:
            with zipfile.ZipFile(pyz) as zf:
                names = set(zf.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            errors.append(f"asciicd.pyz 不是合法 zipapp：{exc}")
        else:
            for module in ZIPAPP_MODULES:
                if module not in names:
                    errors.append(f"zipapp 里缺 {module}")

    if not require_fonts:
        return errors
    fonts = out / "fonts"
    for name in REQUIRED_FONT_FILES:
        if not (fonts / name).is_file():
            errors.append(f"fonts/ 里缺 {name}")
    sums = fonts / "SHA256SUMS"
    for line in _readable(sums).splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        target = fonts / name
        if not target.is_file():
            errors.append(f"SHA256SUMS 里的 {name} 不存在")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            errors.append(f"{name} 与 SHA256SUMS 不一致")
    return errors


def archive_errors(out: Path, require_fonts: bool = True) -> list[str]:
    """归档存在、根目录带版本、内容和目录里一致。"""
    archive = out / ARCHIVE_NAME
    if not archive.is_file():
        return [f"缺少归档 {ARCHIVE_NAME}（预期根目录 {PACKAGE_NAME}/）"]
    errors: list[str] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
    except (OSError, tarfile.TarError) as exc:
        return [f"{ARCHIVE_NAME} 读不了：{exc}"]
    prefix = PACKAGE_NAME + "/"
    for name in REQUIRED_FILES:
        if prefix + name not in names:
            errors.append(f"归档里缺 {prefix}{name}")
    if require_fonts:
        for name in REQUIRED_FONT_FILES:
            if f"{prefix}fonts/{name}" not in names:
                errors.append(f"归档里缺 {prefix}fonts/{name}")
    # tar 会把顶层目录本身也列成 `asciicd-v0.1.0-linux`（末尾没有 /）
    if any(n != PACKAGE_NAME and not n.startswith(prefix) for n in names):
        errors.append(f"归档根目录不是 {prefix}")
    if f"{prefix}{ARCHIVE_NAME}" in names:
        errors.append("归档把自己也打进去了")
    return errors


def cli_errors(pyz: Path) -> list[str]:
    """解出来的 zipapp 必须能报版本、给帮助、通过依赖自检。"""
    cases = [
        (["--help"], None),
        (["--version"], f"asciicd {VERSION}"),
        (["--check-deps", "--fake"], "可以运行"),
    ]
    errors: list[str] = []
    for args, expected in cases:
        try:
            proc = subprocess.run([sys.executable, str(pyz), *args],
                                  capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"asciicd.pyz {' '.join(args)} 跑不起来：{exc}")
            continue
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            errors.append(f"asciicd.pyz {' '.join(args)} 退出码 "
                          f"{proc.returncode}（应为 0）")
        if expected and expected not in output:
            errors.append(f"asciicd.pyz {' '.join(args)} 输出里没有 {expected!r}")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--skip-cli", action="store_true",
                    help="只查文件/归档，不执行 zipapp（CI 以外的快速检查）")
    args = ap.parse_args(argv)
    out = Path(args.out).expanduser()

    errors = layout_errors(out) + archive_errors(out)
    if not args.skip_cli and not errors:
        errors += cli_errors(out / "asciicd.pyz")
    if errors:
        for err in errors:
            print(f"✘ {err}", file=sys.stderr)
        print(f"\n{len(errors)} 项检查失败（{out}）", file=sys.stderr)
        return 1
    print(f"✔ {out} 通过：文件/权限/字体/许可/归档/CLI 版本 "
          f"（asciicd {VERSION}，{ARCHIVE_NAME}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
