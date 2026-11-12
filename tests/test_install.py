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

    def install(self, *args: str):
        return subprocess.run(["bash", str(self.dist / "install.sh"), *args],
                              env=self.env, capture_output=True, text=True,
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
