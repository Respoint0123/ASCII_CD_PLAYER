"""P0④：解压安装与桌面启动。

用临时 HOME 跑真实的 install.sh / 启动器，覆盖带空格的解压路径、重复安装、
PATH 的显式选择、缺 Kitty 且无 TTY 时的失败方式，以及卸载。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import build_dist  # noqa: E402


class TestInstaller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dist = pathlib.Path(cls._tmp.name) / "unpacked dir" / "dist"
        # 测试只关心安装逻辑；真实字体 6.5 MB 的拷贝/gzip 没必要跑。
        with mock.patch.object(build_dist, "FONT_CANDIDATES", ()):
            build_dist.build(cls.dist)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = pathlib.Path(home.name)
        self.bin = self.home / ".local" / "bin"
        self.data = self.home / ".local" / "share"
        self.app_dir = self.data / "asciicd"
        self.desktop = self.data / "applications" / "asciicd.desktop"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ASCIICD_") and not k.startswith("XDG_")}
        env.update({
            "HOME": str(self.home),
            "XDG_BIN_HOME": str(self.bin),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "SHELL": "/bin/bash",
            "ASCIICD_SKIP_DEP_CHECK": "1",
        })
        self.env = env

    def install(self, *args: str, env: dict | None = None):
        return subprocess.run(["bash", str(self.dist / "install.sh"), *args],
                              env=env or self.env, capture_output=True, text=True,
                              timeout=120)

    def test_install_uses_stable_dir_and_quotes_exec(self):
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        for name in ("asciicd.pyz", "asciicd", "install.sh", "LICENSE"):
            self.assertTrue((self.app_dir / name).is_file(), f"缺 {name}")
        self.assertTrue((self.app_dir / "fonts").is_dir())
        link = self.bin / "asciicd"
        self.assertTrue(link.is_symlink())
        self.assertEqual(pathlib.Path(os.readlink(link)), self.app_dir / "asciicd")

        desktop = self.desktop.read_text(encoding="utf-8")
        self.assertIn(f'Exec="{self.bin / "asciicd"}"', desktop,
                      "Exec 必须按桌面规范加引号")
        self.assertIn("Terminal=false", desktop)
        self.assertFalse((self.home / ".bashrc").exists(),
                         "默认不能改 shell 配置")

    def test_add_path_is_opt_in_and_idempotent(self):
        rc = self.home / ".bashrc"
        rc.write_text("# 用户自己的配置\n", encoding="utf-8")

        self.install("--add-path")
        self.install("--add-path")
        text = rc.read_text(encoding="utf-8")
        self.assertIn("# 用户自己的配置", text)
        self.assertEqual(text.count("# >>> asciicd PATH >>>"), 1,
                         "重复安装不能叠加 PATH 段")

        self.install("--uninstall")
        text = rc.read_text(encoding="utf-8")
        self.assertNotIn("asciicd PATH", text)
        self.assertIn("# 用户自己的配置", text)

    def test_reinstall_from_archive_dir(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)
        self.assertTrue((self.app_dir / "asciicd.pyz").is_file())

    def test_upgrade_keeps_user_files_in_install_dir(self):
        """#26：升级只换程序文件，用户后放进安装目录的东西保留原样。"""
        self.assertEqual(self.install().returncode, 0)
        notes = self.app_dir / "notes.txt"
        notes.write_text("user data", encoding="utf-8")
        extra = self.app_dir / "presets" / "mine.json"
        extra.parent.mkdir()
        extra.write_text('{"theme": "amber"}', encoding="utf-8")
        program = self.app_dir / "asciicd.pyz"
        program.write_bytes(b"stale build")     # 模拟旧版本的程序文件

        proc = self.install()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(notes.read_text(encoding="utf-8"), "user data")
        self.assertEqual(extra.read_text(encoding="utf-8"), '{"theme": "amber"}')
        self.assertEqual(program.read_bytes(),
                         (self.dist / "asciicd.pyz").read_bytes(),
                         "程序文件要换成新包里的")
        self.assertTrue((self.app_dir / ".asciicd-install").is_file())

    def test_install_into_existing_empty_dir(self):
        """空目录里没有别人的东西，不该被当成冲突挡下来。"""
        self.app_dir.mkdir(parents=True)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.app_dir / "asciicd.pyz").is_file())
        self.assertTrue((self.app_dir / ".asciicd-install").is_file())

    def test_install_refuses_foreign_targets_and_keeps_them(self):
        """#24：三个目标都可能是用户自己的文件，认不清就别动手。"""
        self.app_dir.mkdir(parents=True)
        mine = self.app_dir / "notes.txt"
        mine.write_text("user data", encoding="utf-8")
        self.bin.mkdir(parents=True)
        foreign_cmd = self.bin / "asciicd"
        foreign_cmd.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        self.desktop.parent.mkdir(parents=True)
        foreign_menu = "[Desktop Entry]\nName=My Tool\n"
        self.desktop.write_text(foreign_menu, encoding="utf-8")

        proc = self.install()

        self.assertEqual(proc.returncode, 1, "已有同名文件时不能硬装")
        for path in (self.app_dir, foreign_cmd, self.desktop):
            self.assertIn(str(path), proc.stderr, "冲突路径要列出来")
        self.assertIn("没有改动任何文件", proc.stderr)
        self.assertEqual(mine.read_text(encoding="utf-8"), "user data")
        self.assertEqual(foreign_cmd.read_text(encoding="utf-8"),
                         "#!/bin/sh\necho mine\n")
        self.assertEqual(self.desktop.read_text(encoding="utf-8"), foreign_menu)
        self.assertFalse((self.app_dir / ".asciicd-install").exists())

    def test_upgrade_from_install_without_marker(self):
        """0.1.0 的安装目录没有归属标记，也不能因此挡住升级。"""
        self.assertEqual(self.install().returncode, 0)
        marker = self.app_dir / ".asciicd-install"
        self.assertTrue(marker.is_file())
        marker.unlink()

        proc = self.install()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(marker.is_file(), "升级后要补上归属标记")
        self.assertTrue((self.app_dir / "asciicd.pyz").is_file())

    def test_legacy_install_with_user_files_can_still_upgrade(self):
        """0.1.0 的安装 + 用户后来放的文件，也要能升级并保留那些文件。"""
        self.assertEqual(self.install().returncode, 0)
        (self.app_dir / ".asciicd-install").unlink()
        notes = self.app_dir / "notes.txt"
        notes.write_text("user data", encoding="utf-8")

        proc = self.install()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(notes.read_text(encoding="utf-8"), "user data")
        self.assertTrue((self.app_dir / ".asciicd-install").is_file())

    def test_uninstall_keeps_targets_that_are_not_ours(self):
        """#24：卸载只清本应用装的东西，同名文件原样保留。"""
        self.assertEqual(self.install().returncode, 0)
        mine = "[Desktop Entry]\nName=My Tool\n"
        self.desktop.write_text(mine, encoding="utf-8")   # 用户自己改过菜单项

        proc = self.install("--uninstall")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.app_dir.exists())
        self.assertFalse((self.bin / "asciicd").exists())
        self.assertEqual(self.desktop.read_text(encoding="utf-8"), mine,
                         "不是 asciicd 的菜单项不能删")
        self.assertIn("原样保留", proc.stderr)

        self.app_dir.mkdir(parents=True)
        stray = self.app_dir / "notes.txt"
        stray.write_text("user data", encoding="utf-8")
        self.assertEqual(self.install("--uninstall").returncode, 0)
        self.assertEqual(stray.read_text(encoding="utf-8"), "user data",
                         "不是 asciicd 的目录不能删")

    def test_upgrade_keeps_custom_fonts(self):
        """升级只换安装器发的字体文件，用户自己加进 fonts/ 的字体保留。"""
        self.assertEqual(self.install().returncode, 0)
        fonts = self.app_dir / "fonts"
        mine = fonts / "MyFont.ttf"
        mine.write_bytes(b"custom font")
        nested = fonts / "extra" / "second.otf"
        nested.parent.mkdir()
        nested.write_bytes(b"another font")
        old_shipped = fonts / "PixelMplus12-Regular.ttf"
        old_shipped.write_bytes(b"old shipped font")   # 旧安装里的随包字体

        proc = self.install()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(mine.read_bytes(), b"custom font")
        self.assertEqual(nested.read_bytes(), b"another font")
        self.assertFalse(old_shipped.exists(), "随包字体会被新包替换")
        self.assertTrue((fonts / "SHA256SUMS").is_file(), "新包的字体文档在位")

    def test_uninstall_keeps_custom_fonts(self):
        """卸载只删随包字体，用户字体留着；只剩自定义字体的目录也能装回来。"""
        self.assertEqual(self.install().returncode, 0)
        mine = self.app_dir / "fonts" / "MyFont.ttf"
        mine.write_bytes(b"custom font")

        proc = self.install("--uninstall")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(mine.read_bytes(), b"custom font")
        self.assertFalse((self.app_dir / "asciicd.pyz").exists())
        self.assertFalse((self.app_dir / "fonts" / "SHA256SUMS").exists(),
                         "随包字体文档要删掉")
        self.assertIn("原样保留", proc.stderr)

        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.app_dir / "asciicd.pyz").is_file())
        self.assertEqual(mine.read_bytes(), b"custom font")

    def test_uninstall_keeps_files_added_to_install_dir(self):
        """安装目录是我们的，但用户后来在里面放的文件也不能一起删。"""
        self.assertEqual(self.install().returncode, 0)
        stray = self.app_dir / "notes.txt"
        stray.write_text("user data", encoding="utf-8")

        proc = self.install("--uninstall")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stray.read_text(encoding="utf-8"), "user data")
        for name in ("asciicd.pyz", "asciicd", "install.sh",
                     ".asciicd-install", "fonts"):
            self.assertFalse((self.app_dir / name).exists(), f"{name} 应该删掉")
        self.assertIn("原样保留", proc.stderr)
        self.assertFalse((self.bin / "asciicd").exists())
        self.assertFalse(self.desktop.exists(), "自己的菜单项照常删")

    def test_install_and_uninstall_with_space_paths(self):
        env = dict(self.env)
        bin_dir = self.home / "my bin"
        data_home = self.home / "my data"
        env["XDG_BIN_HOME"] = str(bin_dir)
        env["XDG_DATA_HOME"] = str(data_home)
        app_dir = data_home / "asciicd"
        desktop = data_home / "applications" / "asciicd.desktop"

        proc = self.install(env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((app_dir / "asciicd.pyz").is_file())
        self.assertTrue((app_dir / ".asciicd-install").is_file())
        self.assertEqual(pathlib.Path(os.readlink(bin_dir / "asciicd")),
                         app_dir / "asciicd")
        self.assertIn(f'Exec="{bin_dir / "asciicd"}"',
                      desktop.read_text(encoding="utf-8"))

        proc = self.install(env=env)           # 空格路径下重复安装
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.install("--uninstall", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(app_dir.exists())
        self.assertFalse((bin_dir / "asciicd").exists())
        self.assertFalse(desktop.exists())

    def test_uninstall_after_unpack_dir_is_gone(self):
        unpack = self.home / "some unpack dir"
        unpack.mkdir()
        subprocess.run(["cp", "-a", f"{self.dist}/.", str(unpack)], check=True)
        proc = subprocess.run(["bash", str(unpack / "install.sh")],
                              env=self.env, capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        subprocess.run(["rm", "-rf", str(unpack)], check=True)

        proc = subprocess.run(["bash", str(self.app_dir / "install.sh"),
                               "--uninstall"],
                              env=self.env, capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.app_dir.exists())
        self.assertFalse((self.bin / "asciicd").exists())
        self.assertFalse(self.desktop.exists())

    def test_launcher_keeps_cli_in_calling_terminal(self):
        self.install()
        env = dict(self.env, ASCIICD_DRY_RUN="1")
        proc = subprocess.run([str(self.bin / "asciicd"), "--version"],
                              env=env, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("asciicd.pyz --version", proc.stdout)
        self.assertNotIn("kitty", proc.stdout,
                         "--version 不该为它开新窗口")

    def test_menu_launch_without_kitty_or_terminal_fails_fast(self):
        self.install()
        mini = self.home / "mini-bin"
        mini.mkdir()
        for name in ("python3", "bash", "dirname", "readlink", "mkdir"):
            tool = subprocess.run(["bash", "-lc", f"command -v {name}"],
                                  capture_output=True, text=True).stdout.strip()
            (mini / name).symlink_to(tool)

        proc = subprocess.run([str(self.bin / "asciicd")],
                              env={"HOME": str(self.home),
                                   "PATH": str(mini),
                                   "XDG_CACHE_HOME": str(self.home / ".cache")},
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 1,
                         "无 TTY、无 Kitty、无终端时必须报错退出")
        self.assertIn("没有找到可用的终端", proc.stderr)


if __name__ == "__main__":
    unittest.main()
