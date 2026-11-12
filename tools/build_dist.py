#!/usr/bin/env python3
"""把 asciicd 打成独立分发包。

产物 dist/：
    asciicd.pyz          单文件 zipapp（整个程序，含所有模块）
    asciicd              启动器：开一个**自己的** kitty 窗口，临时配置里用点阵字体
    asciicd.desktop      桌面菜单项
    fonts/               点阵字体（PixelMplus12 + Unifont）
    install.sh           安装到 ~/.local/bin + 桌面菜单
    README.txt

用法：python3 tools/build_dist.py [--out dist]
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import VERSION  # noqa: E402  （版本号的唯一来源）

PACKAGE_NAME = f"asciicd-v{VERSION}-linux"     # 归档根目录名
ARCHIVE_NAME = f"{PACKAGE_NAME}.tar.gz"
LEGACY_ARCHIVES = ("asciicd-bundle.tar.gz",)     # 旧版归档名，重建时顺手清掉
PACKAGES = ("cdda", "audio", "dsp", "ui", "services")
TOP_FILES = ("config.py", "textfmt.py", "prefs.py", "app_state.py",
             "app.py", "asciicd.py")
FONT_CANDIDATES = (
    "PixelMplus12-Regular.ttf",
    "unifont.otf",
)
# 字体许可、来源与校验和：必须跟字体一起走，不能只留在源码树里。
FONT_DOCS = (
    "LICENSE-PixelMplus12.txt",
    "LICENSE-Unifont.txt",
    "README.md",
    "SHA256SUMS",
)

# 构建产物清单：--out 已存在时，只更新这些名字，别的文件一概不碰。
MARKER = ".asciicd-dist"          # 输出目录归属标记
MANAGED_FILES = ("asciicd.pyz", "asciicd", "asciicd.desktop",
                 "install.sh", "README.txt", "LICENSE")
MANAGED_DIRS = ("fonts",)
MANAGED = frozenset(MANAGED_FILES + MANAGED_DIRS
                    + (MARKER, ARCHIVE_NAME) + LEGACY_ARCHIVES)


class BuildError(RuntimeError):
    """输出目录不安全/不可用：宁可失败，也不删用户的文件。"""


def pyproject_version() -> str:
    """从 pyproject.toml 里读版本；3.10 没有 tomllib，正则足够。"""
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def check_version() -> None:
    """源码、CLI、zipapp、包名、归档名都用同一个版本号。"""
    declared = pyproject_version()
    if declared != VERSION:
        raise BuildError(
            f"版本不一致：config.VERSION={VERSION!r}，"
            f"pyproject.toml={declared or '（读不到）'!r}；两处改成同一个再打包。")

LAUNCHER = r"""#!/usr/bin/env bash
# asciicd 启动器：在自己的窗口里用点阵字体（PixelMplus12）跑，不改动你的任何配置。
# 从桌面菜单启动且没有 kitty 时，会借一个终端；都没有就报错退出，不把 TUI
# 丢进没有 TTY 的环境里空转。
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
APP="$HERE/asciicd.pyz"
FONTS="$HERE/fonts"
DRY_RUN="${ASCIICD_DRY_RUN:-0}"

run_here() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "# 将要执行：python3 $APP $*"
        exit 0
    fi
    exec python3 "$APP" "$@"
}

# --help / --version / --check-deps 这类一次性命令留在当前终端，别为它开新窗口
for arg in "$@"; do
    case "$arg" in
        -h|--help|--version|--check-deps|--list-devices|--selftest)
            run_here "$@"
            ;;
    esac
done

# 显式要求就地运行（当前终端里、用你当前字体）
if [ "${ASCIICD_IN_PLACE:-0}" = "1" ]; then
    run_here "$@"
fi

if command -v kitty >/dev/null 2>&1; then
    # 字体只对这次启动的 kitty 可见：用一个私有 fontconfig，不污染系统/浏览器
    CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/asciicd"
    mkdir -p "$CACHE/fontcache"
    FONTCONF="$CACHE/fonts.conf"
    {
        echo '<?xml version="1.0"?>'
        echo '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">'
        echo '<fontconfig>'
        echo '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
        echo "  <dir>$FONTS</dir>"
        echo "  <cachedir>$CACHE/fontcache</cachedir>"
        echo '</fontconfig>'
    } > "$FONTCONF"

    # 只在这个窗口里覆盖字体。PixelMplus12 是等宽点阵字体（CJK 齐全），
    # 默认 12pt：1 像素 ≈ 终端 1 像素，点阵不会被插值糊掉；ASCIICD_FONT_SIZE 可调。
    FONT_SIZE="${ASCIICD_FONT_SIZE:-12}"
    CMD=(env FONTCONFIG_FILE="$FONTCONF" kitty
         -o font_family=PixelMplus12 -o font_size="$FONT_SIZE"
         --class asciicd --title asciicd python3 "$APP" "$@")

    if [ "$DRY_RUN" = "1" ]; then
        echo "# 将要执行：${CMD[*]}"
        exit 0
    fi
    "${CMD[@]}" &
    WIN_PID=$!
    wait "$WIN_PID"
    exit 0
fi

# 没有 kitty 但本来就在终端里：就地跑，用当前字体
if [ -t 0 ] && [ -t 1 ]; then
    run_here "$@"
fi

# 从桌面菜单启动、又没有 kitty：必须借一个终端，否则 TUI 会挂在无 TTY 环境
open_terminal() {
    local term
    for term in x-terminal-emulator gnome-terminal konsole alacritty foot wezterm xterm; do
        command -v "$term" >/dev/null 2>&1 || continue
        if [ "$DRY_RUN" = "1" ]; then
            echo "# 将要执行：$term ... python3 $APP"
            exit 0
        fi
        case "$term" in
            gnome-terminal) exec "$term" -- python3 "$APP" "$@" ;;
            wezterm)        exec "$term" start -- python3 "$APP" "$@" ;;
            *)              exec "$term" -e python3 "$APP" "$@" ;;
        esac
    done
    echo "asciicd：没有找到可用的终端（kitty 或其他终端模拟器）。" >&2
    echo "  装一个 kitty，或用 ASCIICD_IN_PLACE=1 在已有终端里运行。" >&2
    exit 1
}
open_terminal
"""

DESKTOP = """[Desktop Entry]
Type=Application
Version=1.0
Name=asciicd
Name[zh_CN]=asciicd 字符 CD 播放器
Comment=Console CD-DA player with character-art spectrum
Comment[zh_CN]=控制台 CD 播放器，字符频谱/声谱可视化
Exec=@EXEC@
Terminal=false
Icon=media-optical
Categories=AudioVideo;Audio;Player;
Keywords=cd;audio;player;terminal;tui;
"""

INSTALL = r"""#!/usr/bin/env bash
# 安装 asciicd：复制到固定目录，装命令软链和桌面菜单项。
# 默认不改 shell 配置；要把 ~/.local/bin 加进 PATH，显式传 --add-path。
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/asciicd"
DESKTOP_DIR="$DATA_HOME/applications"
DESKTOP_FILE="$DESKTOP_DIR/asciicd.desktop"
PATH_MARK_BEGIN="# >>> asciicd PATH >>>"
PATH_MARK_END="# <<< asciicd PATH <<<"

usage() {
    cat <<'USAGE'
用法：./install.sh [--add-path] [--uninstall]
  （默认）       复制到 ~/.local/share/asciicd，装命令软链和桌面菜单项
  --add-path     把 ~/.local/bin 追加进当前 shell 的 rc（带标记，可撤销）
  --uninstall    删除程序、命令、菜单项，以及 --add-path 加的 PATH 段
USAGE
}

ADD_PATH=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --add-path)  ADD_PATH=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "未知参数：$arg" >&2; usage >&2; exit 2 ;;
    esac
done

remove_path_block() {
    local rc="$1"
    [ -f "$rc" ] || return 0
    if grep -qF "$PATH_MARK_BEGIN" "$rc"; then
        sed -i "\|^# >>> asciicd PATH >>>$|,\|^# <<< asciicd PATH <<<$|d" "$rc"
        echo "PATH：已从 $rc 删掉 asciicd 的配置段。"
    fi
}

if [ "$UNINSTALL" = "1" ]; then
    if [ -L "$BIN_DIR/asciicd" ]; then
        target="$(readlink -f "$BIN_DIR/asciicd" 2>/dev/null || true)"
        case "$target" in "$APP_DIR"/*) rm -f "$BIN_DIR/asciicd" ;; esac
    fi
    rm -f "$DESKTOP_FILE"
    remove_path_block "$HOME/.bashrc"
    remove_path_block "$HOME/.zshrc"
    if [ "$APP_DIR" = "$DATA_HOME/asciicd" ]; then
        rm -rf -- "$APP_DIR"
    fi
    echo "已卸载 asciicd：$APP_DIR、$BIN_DIR/asciicd、$DESKTOP_FILE"
    exit 0
fi

if [ "$HERE" = "$APP_DIR" ]; then
    echo "这是安装目录本身；重装请用新发布包里的 install.sh（卸载用 ./install.sh --uninstall）。" >&2
    exit 1
fi
if [ ! -f "$HERE/asciicd.pyz" ]; then
    echo "✘ $HERE 里没有 asciicd.pyz；请在解压后的发布包里运行 install.sh。" >&2
    exit 1
fi

mkdir -p "$BIN_DIR" "$DESKTOP_DIR"

# 固定目录：解压目录以后可以删/移动，安装不会断链
rm -rf -- "$APP_DIR"
mkdir -p "$APP_DIR"
# install.sh / .desktop 也放进固定目录：解压包删掉后仍能从这里卸载
for item in asciicd.pyz asciicd install.sh asciicd.desktop LICENSE README.txt; do
    if [ -e "$HERE/$item" ]; then cp -a "$HERE/$item" "$APP_DIR/"; fi
done
if [ -d "$HERE/fonts" ]; then cp -a "$HERE/fonts" "$APP_DIR/fonts"; fi
chmod +x "$APP_DIR/asciicd" "$APP_DIR/asciicd.pyz"

ln -sfn "$APP_DIR/asciicd" "$BIN_DIR/asciicd"

# .desktop 的 Exec 必须按规范引用：路径里有空格/引号/`$`/`%` 都得出得去
exec_path="$BIN_DIR/asciicd"
escaped="$exec_path"
escaped="${escaped//\\/\\\\}"
escaped="${escaped//\"/\\\"}"
escaped="${escaped//\$/\\\$}"
escaped="${escaped//\`/\\\`}"
escaped="${escaped//%/%%}"
desktop="$(cat "$HERE/asciicd.desktop")"
desktop="${desktop//@EXEC@/\"$escaped\"}"
printf '%s\n' "$desktop" > "$DESKTOP_FILE"

if [ "$ADD_PATH" = "1" ]; then
    case "${SHELL##*/}" in
        zsh)  RC="$HOME/.zshrc" ;;
        bash) RC="$HOME/.bashrc" ;;
        *)    RC="" ;;
    esac
    if [ -z "$RC" ]; then
        echo "PATH：当前 shell 是 ${SHELL:-未知}，请自行把 $BIN_DIR 加进 PATH。"
    elif grep -qF "$PATH_MARK_BEGIN" "$RC" 2>/dev/null; then
        echo "PATH：$RC 里已有 asciicd 的配置段，跳过。"
    else
        {
            echo ""
            echo "$PATH_MARK_BEGIN"
            echo "case \":\$PATH:\" in *\":$BIN_DIR:\"*) ;; *) export PATH=\"$BIN_DIR:\$PATH\" ;; esac"
            echo "$PATH_MARK_END"
        } >> "$RC"
        echo "PATH：已往 $RC 追加配置段（带标记，--uninstall 会删掉），重开终端生效。"
    fi
else
    echo "PATH：没有修改 shell 配置。想直接敲 'asciicd'，重开终端或运行 ./install.sh --add-path。"
fi

echo "已安装："
echo "  程序      $APP_DIR/asciicd.pyz"
echo "  命令      $BIN_DIR/asciicd"
echo "  菜单项    $DESKTOP_FILE"
echo
echo "直接运行： asciicd          （有 kitty 就开独立点阵字体窗口）"
echo "当前终端：ASCIICD_IN_PLACE=1 asciicd"
echo "卸载：     $APP_DIR/install.sh --uninstall    （解压包删了也能用）"
echo

# 安装后立刻自检：分发包只带程序本体和字体，不捆绑系统依赖
echo "运行前依赖检查："
if [ "${ASCIICD_SKIP_DEP_CHECK:-0}" = "1" ]; then
    echo "已跳过（ASCIICD_SKIP_DEP_CHECK=1）。"
elif python3 "$APP_DIR/asciicd.pyz" --check-deps; then
    echo "依赖齐全，可以运行了。"
else
    echo "还有必需依赖没装好；按上面的提示装完再运行。"
fi
"""

README = """asciicd — 控制台 CD-DA 播放器（字符频谱 / 声谱图）
====================================================

安装（复制到 ~/.local/share/asciicd，只写用户目录）：
    ./install.sh              # 安装；默认不改 shell 配置
    ./install.sh --add-path   # 顺便把 ~/.local/bin 加进 PATH（带标记，可撤销）
    ./install.sh --uninstall  # 卸载程序、命令、菜单项和 PATH 配置段

运行：
    asciicd                  # 打开一个独立的、自带点阵字体的终端窗口
    asciicd --theme jpop     # 配色：phosphor / amber / jpop / modern
    ASCIICD_IN_PLACE=1 asciicd   # 就在当前终端里跑（用你当前字体）
    python3 asciicd.pyz --fake   # 不插盘看效果

常用参数：
    --device /dev/sr0   光驱      --track N     从第几轨开始
    --layout dash|mini  界面布局  --fast-rip    首段开播更快
    --theme NAME        覆盖配色（不填 = 沿用上次退出时选的，按 t 切换并自动记住）
    --paused --wait-disc  开机自启用：先停在停止态、没盘停在等待画面（配合 Hyprland 窗口规则做默认界面）
    --on-eject continue|stop  弹盘行为：继续播缓存 / 立即停（默认 continue）
    --on-insert play|pause    插盘行为：自动开播 / 等空格（默认 play）
    --version                 打印版本（asciicd 0.1.0）

常用按键：
    space 播放/暂停   s 停止   h 帮助   l/m 布局   t 主题   q 退出

许可：
    项目代码：MIT（见包内 LICENSE）。
    字体：PixelMplus12 用 M+ FONT LICENSE；GNU Unifont 16.0.04 为双许可
    （SIL OFL 1.1 / GPLv2+，含字体嵌入例外），本包按 OFL 分发。
    完整文本、来源和 SHA-256 在 fonts/ 下（LICENSE-*.txt、README.md、SHA256SUMS）。

依赖（分发包**只带程序本体和字体**，下面这些需要系统里自备）：
    python3 >= 3.10   Arch: pacman -S python
    numpy             Arch: pacman -S python-numpy（必需；包里不捆绑，也不修改系统 Python）
    cdparanoia        Arch: pacman -S cdparanoia（抓轨 + TOC；只跑 --fake 可跳过）
    pw-cat            Arch: pacman -S pipewire（播放必需；--fake 也要）
    kitty             Arch: pacman -S kitty（可选：独立窗口 + 点阵字体；ASCIICD_IN_PLACE=1 跳过）
    python-gobject    Arch: pacman -S python-gobject（可选；MPRIS 尚未接线）

运行前自检（缺什么会给出安装命令）：
    ./asciicd --check-deps
    python3 asciicd.pyz --check-deps
    python3 asciicd.pyz --check-deps --fake   # 按 --fake 模式检查（cdparanoia 可选）

音乐元数据来自 MusicBrainz（可 --no-metadata 关）。
字体：PixelMplus12（OFL）与 GNU Unifont（GPL+font exception），随包附带，只在本程序的
      窗口里通过私有 fontconfig 生效，不装进系统字体目录。
"""


def collect_sources() -> list[Path]:
    files = [ROOT / f for f in TOP_FILES]
    for pkg in PACKAGES:
        files.extend(sorted(p for p in (ROOT / pkg).rglob("*.py")))
    return [f for f in files if f.is_file()]


def build_pyz(target: Path) -> None:
    sources = collect_sources()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("__main__.py",
                    "import sys\nfrom asciicd import main\nsys.exit(main())\n")
        for src in sources:
            zf.write(src, arcname=str(src.relative_to(ROOT)))
    target.write_bytes(b"#!/usr/bin/env python3\n" + buf.getvalue())
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build_fonts(dest: Path) -> list[str]:
    """字体随包走，不装进系统字体目录（否则会影响浏览器等通用字体匹配）。

    许可文本和校验和一并发过去：只有字体没有许可，等于没有分发权。
    """
    dest.mkdir(parents=True, exist_ok=True)
    found = []
    for name in FONT_CANDIDATES:
        hit = ROOT / "assets" / "fonts" / name
        if hit.is_file():
            shutil.copy2(hit, dest / name)
            found.append(name)
    for name in FONT_DOCS:
        src = ROOT / "assets" / "fonts" / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    return found


def looks_like_our_output(out: Path) -> bool:
    """输出目录是不是构建器自己建的？

    带标记的直接认；旧版本留下的 dist/ 没有标记，只有「一个陌生名字都没有」
    才接管 —— 和缓存目录的归属判断同一套逻辑。
    """
    marker = out / MARKER
    if marker.is_file() and not marker.is_symlink():
        return True
    try:
        with os.scandir(out) as entries:
            for entry in entries:
                if entry.name not in MANAGED:
                    return False
    except OSError:
        return False
    return True


def _remove_managed(path: Path) -> None:
    """删除一个构建产物；软链接只摘链接，绝不跟到目标目录里删东西。"""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except FileNotFoundError:
        pass


def prepare_output(out: Path) -> None:
    """确保 --out 可以安全写入，并清掉上一轮的构建产物。"""
    if out.is_symlink():
        raise BuildError(f"输出目录是软链接，拒绝覆盖：{out}")
    if out.exists() and not out.is_dir():
        raise BuildError(f"输出路径已存在且不是目录：{out}")
    if out.exists():
        try:
            with os.scandir(out) as entries:
                has_entries = any(True for _ in entries)
        except OSError as exc:
            raise BuildError(f"无法读取输出目录：{out}（{exc}）") from exc
        if has_entries and not looks_like_our_output(out):
            raise BuildError(
                f"输出目录非空，且不是本构建器创建的：{out}\n"
                "  为免误删，这里不会动它；换一个 --out，或手动清空该目录。")
        for name in (MANAGED_FILES + MANAGED_DIRS
                     + (ARCHIVE_NAME,) + LEGACY_ARCHIVES):
            _remove_managed(out / name)
    else:
        out.mkdir(parents=True)
    try:
        (out / MARKER).write_text(
            "asciicd build_dist 管理的输出目录；由 tools/build_dist.py 重新生成\n",
            encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"无法写入输出目录：{out}（{exc}）") from exc


def _populate(dest: Path) -> list[str]:
    """在临时目录里生成全部产物；任何一步失败都不碰 --out。"""
    license_src = ROOT / "LICENSE"
    if not license_src.is_file():
        raise BuildError("缺少项目 LICENSE：先补根目录 LICENSE 再打包")
    shutil.copy2(license_src, dest / "LICENSE")
    build_pyz(dest / "asciicd.pyz")
    launcher = dest / "asciicd"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)
    (dest / "asciicd.desktop").write_text(DESKTOP, encoding="utf-8")
    inst = dest / "install.sh"
    inst.write_text(INSTALL, encoding="utf-8")
    inst.chmod(0o755)
    (dest / "README.txt").write_text(README, encoding="utf-8")
    return build_fonts(dest / "fonts")


def build(out: Path) -> dict:
    check_version()
    out = out.expanduser()
    staging_root = Path(tempfile.mkdtemp(prefix="asciicd-build-"))
    staging = staging_root / PACKAGE_NAME
    try:
        staging.mkdir()
        fonts = _populate(staging)          # 先在临时目录里做完
        prepare_output(out)                 # 确认目标安全后，才动它
        for entry in sorted(staging.iterdir()):
            target = out / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
        archive = out / ARCHIVE_NAME
        shutil.make_archive(str(archive)[:-len(".tar.gz")], "gztar",
                            root_dir=str(staging_root), base_dir=PACKAGE_NAME)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return {
        "out": out,
        "archive": archive,
        "package": PACKAGE_NAME,
        "version": VERSION,
        "fonts": fonts,
        "modules": len(collect_sources()),
        "pyz": (out / "asciicd.pyz").stat().st_size,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"),
                    help="构建输出目录（默认 dist/；非本构建器创建的目录会被拒绝）")
    args = ap.parse_args()

    try:
        info = build(Path(args.out))
    except BuildError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"✘ 构建失败：{exc}", file=sys.stderr)
        return 1

    out = info["out"]
    size_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    print(f"✔ {out}/")
    print(f"  asciicd.pyz      {info['pyz'] / 1e3:.0f} KB（{info['modules']} 个模块）")
    print(f"  fonts/           {', '.join(info['fonts']) if info['fonts'] else '（无）'}")
    print("  install.sh / asciicd / asciicd.desktop / README.txt")
    print(f"  合计 {size_mb:.1f} MB；打包 {info['archive'].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
