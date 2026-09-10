"""Stdlib tests for the fork action; no compiler or installed PyInstaller required."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("fresh_build", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class FreshBuildTests(unittest.TestCase):
    def test_native_variants(self):
        self.assertEqual(build.expected_bootloaders("Linux"), {"run", "run_d"})
        self.assertEqual(build.expected_bootloaders("Darwin"), {"run", "run_d", "runw", "runw_d"})
        self.assertEqual(build.expected_bootloaders("Windows"), {"run.exe", "run_d.exe", "runw.exe", "runw_d.exe"})
        with self.assertRaises(RuntimeError):
            build.expected_bootloaders("unsupported")

    def test_actions_output_rejects_newlines(self):
        with self.assertRaises(ValueError):
            build.write_actions_value("GITHUB_OUTPUT", "manifest", "path\ninjection")

    def test_verify_hashes_and_reject_incomplete_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "PyInstaller"
            directory = package / "bootloader/Linux-64bit-intel"
            directory.mkdir(parents=True)
            hashes = {}
            for name in ("run", "run_d"):
                (directory / name).write_bytes(name.encode())
                hashes[name] = hashlib.sha256(name.encode()).hexdigest()
            data = {"schema_version": 1, "system": "Linux", "pyinstaller_platform": "Linux-64bit-intel",
                    "pyinstaller_version": "6.22.2", "bootloaders_sha256": hashes}
            manifest = Path(temporary) / "manifest.json"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with mock.patch.object(build, "installed_package", return_value=package), \
                 mock.patch.object(build, "capture", return_value=json.dumps(["Linux-64bit-intel", "6.22.2"])), \
                 mock.patch.object(build.platform, "system", return_value="Linux"):
                self.assertEqual(build.verify_manifest(manifest), data)
                (directory / "run_d").unlink()
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    build.verify_manifest(manifest)
                data["bootloaders_sha256"] = {}
                manifest.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "exactly all"):
                    build.verify_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
