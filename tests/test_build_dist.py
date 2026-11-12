"""P0②：构建脚本的目录安全 —— 只更新自己管理的文件，绝不误删用户目录。

测试里把字体候选清空：字体拷贝 + gzip 压缩占构建时间的大头，而这些用例
只关心「动了哪些文件」。真实字体打包由 `make build` 和 P0-5 的 CI 覆盖。
"""
from __future__ import annotations

import hashlib
import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import build_dist  # noqa: E402
import check_dist  # noqa: E402


class TestBuildOutputSafety(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(build_dist, "FONT_CANDIDATES", ())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_build_creates_marker_and_all_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "dist"
            info = build_dist.build(out)

            self.assertTrue((out / build_dist.MARKER).is_file())
            for name in ("asciicd.pyz", "asciicd", "asciicd.desktop",
                         "install.sh", "README.txt"):
                self.assertTrue((out / name).is_file(), f"缺 {name}")
            self.assertTrue((out / build_dist.ARCHIVE_NAME).is_file())
            self.assertTrue((out / "LICENSE").is_file())
            self.assertEqual(info["archive"].parent, out,
                             "归档必须跟着 --out 走，不能写回默认 dist/")
            self.assertEqual(build_dist.ARCHIVE_NAME,
                             f"asciicd-v{build_dist.VERSION}-linux.tar.gz")
            with tarfile.open(info["archive"]) as tar:
                roots = {name.split("/")[0] for name in tar.getnames()}
            self.assertEqual(roots, {build_dist.PACKAGE_NAME},
                             "归档根目录带版本，且只有一个顶层目录")

    def test_rebuild_preserves_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "dist"
            out.mkdir()
            build_dist.build(out)
            (out / "user-note.txt").write_text("keep", encoding="utf-8")
            (out / "asciicd-bundle.tar.gz").write_bytes(b"old archive")

            build_dist.build(out)          # 重复构建：旧归档要清掉

            self.assertFalse((out / "asciicd-bundle.tar.gz").exists(),
                             "旧版归档名残留会混淆 release 上传")
            self.assertEqual((out / "user-note.txt").read_text(encoding="utf-8"),
                             "keep")

    def test_refuses_foreign_nonempty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "dist"
            out.mkdir()
            (out / "important.txt").write_text("do not delete", encoding="utf-8")

            with self.assertRaises(build_dist.BuildError):
                build_dist.build(out)

            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["important.txt"])
            self.assertEqual((out / "important.txt").read_text(encoding="utf-8"),
                             "do not delete")

    def test_adopts_legacy_build_dir_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "dist"
            out.mkdir()
            (out / "asciicd.pyz").write_bytes(b"old")     # 旧版构建产物，无标记
            (out / "install.sh").write_text("old", encoding="utf-8")
            (out / "fonts").mkdir()

            build_dist.build(out)

            self.assertTrue((out / build_dist.MARKER).is_file())
            self.assertGreater((out / "asciicd.pyz").stat().st_size, 1000,
                               "旧产物应被新的 zipapp 替换")

    def test_refuses_symlink_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / "real"
            real.mkdir()
            link = pathlib.Path(tmp) / "link"
            link.symlink_to(real)

            with self.assertRaises(build_dist.BuildError):
                build_dist.build(link)
            self.assertEqual(list(real.iterdir()), [],
                             "软链接指向的目录一点都不能动")

    def test_ships_project_and_font_licenses(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "dist"
            build_dist.build(out)

            self.assertIn("MIT License", (out / "LICENSE").read_text(encoding="utf-8"),
                          "项目 LICENSE 必须随包")
            for name in ("LICENSE-PixelMplus12.txt", "LICENSE-Unifont.txt",
                         "SHA256SUMS", "README.md"):
                self.assertTrue((out / "fonts" / name).is_file(),
                                f"字体目录缺 {name}")

    def test_font_checksums_match_pinned_hashes(self):
        fonts = ROOT / "assets" / "fonts"
        lines = [line for line in
                 (fonts / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        self.assertTrue(lines)
        for line in lines:
            digest, name = line.split()
            actual = hashlib.sha256((fonts / name).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, f"{name} 与锁定的 SHA-256 不一致")

    def test_font_licenses_are_complete(self):
        fonts = ROOT / "assets" / "fonts"
        unifont = (fonts / "LICENSE-Unifont.txt").read_text(encoding="utf-8")
        self.assertNotIn("<dates>", unifont, "OFL 里不能留版权占位符")
        self.assertIn("Roman Czyborra", unifont)
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", unifont)
        pixel = (fonts / "LICENSE-PixelMplus12.txt").read_text(encoding="utf-8")
        self.assertIn("itouhiro", pixel)
        self.assertIn("M+ FONTS PROJECT", pixel)

    def test_version_is_single_sourced(self):
        from config import VERSION

        self.assertEqual(VERSION, "0.1.0")
        self.assertEqual(build_dist.VERSION, VERSION)
        self.assertEqual(build_dist.pyproject_version(), VERSION,
                         "pyproject.toml 与 config.VERSION 必须一致")

    def test_check_dist_accepts_fresh_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "dist"
            build_dist.build(out)

            errors = (check_dist.layout_errors(out, require_fonts=False)
                      + check_dist.archive_errors(out, require_fonts=False))
            self.assertEqual(errors, [])

    def test_check_dist_reports_missing_license(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "dist"
            build_dist.build(out)
            (out / "LICENSE").unlink()

            errors = check_dist.layout_errors(out, require_fonts=False)
            self.assertTrue(any("LICENSE" in e for e in errors))

    def test_failure_leaves_existing_output_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "dist"
            out.mkdir()
            (out / "user-note.txt").write_text("keep", encoding="utf-8")
            (out / "asciicd.pyz").write_bytes(b"previous build")

            with mock.patch.object(build_dist, "build_pyz",
                                   side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    build_dist.build(out)

            self.assertEqual((out / "asciicd.pyz").read_bytes(), b"previous build",
                             "生成失败时不能先清掉上一轮产物")
            self.assertEqual((out / "user-note.txt").read_text(encoding="utf-8"),
                             "keep")


if __name__ == "__main__":
    unittest.main()
