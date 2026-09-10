"""Build this checkout's bootloaders, never a cached upstream PyInstaller wheel.

Requires Python 3.10+, pip, Git, and a native C toolchain. Run in a disposable
checkout. The caller supplies its own Python (and Tcl/Tk for GUI applications).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import sysconfig
import tempfile
from datetime import datetime, timezone
import zipfile


def run(command: list[str], **kwargs) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(command: list[str], **kwargs) -> str:
    return subprocess.check_output(command, text=True, **kwargs).strip()


def installed_package() -> Path:
    # -I excludes the working directory and PYTHONPATH: do not accidentally
    # verify the source checkout instead of the installed package.
    return Path(capture([sys.executable, "-I", "-c",
                         "import PyInstaller; print(PyInstaller.__file__)"])).parent


def expected_bootloaders(system: str) -> set[str]:
    if system not in {"Windows", "Linux", "Darwin"}:
        raise RuntimeError(f"Unsupported build platform: {system}")
    names = {"run", "run_d"}
    if system in {"Windows", "Darwin"}:
        names |= {"runw", "runw_d"}
    return {name + ".exe" for name in names} if system == "Windows" else names


def verify_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    system = platform.system()
    if data.get("schema_version") != 1 or data.get("system") != system:
        raise RuntimeError("Wrong fresh-build manifest schema or operating system")
    installed = installed_package()
    installed_identity = json.loads(capture([
        sys.executable, "-I", "-c",
        "import json, PyInstaller; print(json.dumps([PyInstaller.PLATFORM, PyInstaller.__version__]))",
    ]))
    if installed_identity != [data.get("pyinstaller_platform"), data.get("pyinstaller_version")]:
        raise RuntimeError("Installed PyInstaller platform/version differs from the fresh build")
    hashes = data.get("bootloaders_sha256")
    if not isinstance(hashes, dict) or set(hashes) != expected_bootloaders(system):
        raise RuntimeError("Manifest must contain exactly all expected native bootloaders")
    for name, expected in hashes.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeError(f"Invalid bootloader SHA-256: {name}")
        actual = installed / "bootloader" / data["pyinstaller_platform"] / name
        if not actual.is_file() or digest(actual) != expected:
            raise RuntimeError(f"Installed PyInstaller bootloader changed or is missing: {actual}")
    return data


def write_actions_value(variable: str, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError("Action output values cannot contain line breaks")
    destination = os.environ.get(variable)
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("build-tools"))
    parser.add_argument("--verify-manifest", type=Path)
    args = parser.parse_args()
    if args.verify_manifest:
        verify_manifest(args.verify_manifest.resolve())
        print("Installed bootloaders match the fresh-build manifest.")
        return
    if args.source is None:
        parser.error("--source is required when building")
    if struct.calcsize("P") != 8:
        raise RuntimeError("This action supports native 64-bit builds only")
    source, output = args.source.resolve(), args.output.resolve()
    if not (source / "bootloader/waf").is_file():
        raise RuntimeError(f"Not a full PyInstaller source checkout: {source}")
    if output == source or source.is_relative_to(output) or output.is_relative_to(source):
        raise RuntimeError("Output and source directories must not contain one another")
    output.mkdir(parents=True, exist_ok=True)
    if list(output.glob("*.whl")) or (output / "fresh-pyinstaller.json").exists():
        raise RuntimeError(f"Use a fresh output directory, not {output}")

    source_commit = capture(["git", "-C", str(source), "rev-parse", "HEAD"])
    system = platform.system()
    if system not in {"Windows", "Linux", "Darwin"}:
        raise RuntimeError(f"Unsupported build platform: {system}")
    # Query the source without requiring its not-yet-installed Windows runtime
    # dependencies (pywin32-ctypes). This is the same mode used by hatch_build.py.
    probe_env = os.environ.copy()
    probe_env["_PYINSTALLER_SETUP"] = "1"
    native_platform, source_version = json.loads(capture(
        [sys.executable, "-c", "import json, PyInstaller; print(json.dumps([PyInstaller.PLATFORM, PyInstaller.__version__]))"],
        cwd=source, env=probe_env,
    ))
    native_dir = source / "PyInstaller/bootloader" / native_platform
    names = sorted(expected_bootloaders(system))
    previous = {name: digest(native_dir / name) for name in names if (native_dir / name).is_file()}

    # Clean Waf's object cache AND remove shipped executables. A failed compiler
    # cannot fall through to an old precompiled bootloader.
    run([sys.executable, "waf", "distclean"], cwd=source / "bootloader")
    for name in names:
        (native_dir / name).unlink(missing_ok=True)
    waf_args = ["--no-universal2"] if system == "Darwin" else ["--target-arch=64bit"]
    run([sys.executable, "waf", "all", *waf_args], cwd=source / "bootloader")
    for name in names:
        if not (native_dir / name).is_file():
            raise RuntimeError(f"Compiler did not create {native_dir / name}")
    bootloader_hashes = {name: digest(native_dir / name) for name in names}

    # Package exactly the binaries above. Compilation is explicit, so do not
    # compile for a second time inside the wheel build hook.
    env = os.environ.copy()
    env.pop("PYINSTALLER_COMPILE_BOOTLOADER", None)
    env.pop("PYINSTALLER_BOOTLOADER_WAF_ARGS", None)
    env["PYI_PLATFORM"] = native_platform
    env["PYI_WHEEL_TAG"] = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    run([sys.executable, "-m", "pip", "wheel", "--verbose", "--no-cache-dir",
         "--no-deps", "--wheel-dir", str(output), str(source)], env=env)
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one built wheel, found: {wheels}")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        for name, expected in bootloader_hashes.items():
            entry = f"PyInstaller/bootloader/{native_platform}/{name}"
            if hashlib.sha256(archive.read(entry)).hexdigest() != expected:
                raise RuntimeError(f"Wheel contains a different bootloader: {entry}")
    run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--force-reinstall", str(wheel)], env=env)
    manifest = output / "fresh-pyinstaller.json"
    metadata = {
        "schema_version": 1,
        "pyinstaller_version": source_version,
        "smoke_build_options": ["--onefile", "--noupx"],
        "source_commit": source_commit,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "system": system,
        "machine": platform.machine(),
        "python": sys.version,
        "pyinstaller_platform": native_platform,
        "waf_arguments": ["distclean", "all", *waf_args],
        "wheel": wheel.name,
        "wheel_sha256": digest(wheel),
        "bootloaders_sha256": bootloader_hashes,
        "previous_bootloaders_sha256": previous,
        "runner_image": os.environ.get("ImageOS"),
        "runner_image_version": os.environ.get("ImageVersion"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "note": "Fresh compilation does not promise different bytes or absence of antivirus false positives.",
    }
    manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    verify_manifest(manifest)

    # Exercise the installed package and its console bootloader outside the fork.
    with tempfile.TemporaryDirectory(prefix="fresh-pyi-test-") as directory:
        work = Path(directory)
        (work / "hello.py").write_text("print('fresh-bootloader-ok')\n", encoding="utf-8")
        run([sys.executable, "-I", "-m", "PyInstaller", "--noconfirm", "--clean",
             "--onefile", "--noupx", "hello.py"], cwd=work)
        executable = work / "dist" / ("hello.exe" if system == "Windows" else "hello")
        result = capture([str(executable)], cwd=work, timeout=90)
        if result != "fresh-bootloader-ok":
            raise RuntimeError(f"Bootloader smoke test failed: {result!r}")

    write_actions_value("GITHUB_OUTPUT", "wheel", str(wheel))
    write_actions_value("GITHUB_OUTPUT", "manifest", str(manifest))
    write_actions_value("GITHUB_ENV", "FRESH_PYINSTALLER_MANIFEST", str(manifest))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"### Fresh PyInstaller: {native_platform}\n\nSource commit: `{source_commit}`\n\n")
            stream.write(f"Wheel: `{wheel.name}`\n\n")
            for name, value in bootloader_hashes.items():
                stream.write(f"- `{name}`: `{value}`\n")
    print(f"Verified fresh PyInstaller; provenance: {manifest}")


if __name__ == "__main__":
    main()
