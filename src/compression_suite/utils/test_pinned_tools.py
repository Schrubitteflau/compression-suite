"""Tests for pinned tools: the manifest, the resolver and the install, against a local archive."""

import hashlib
import io
import shutil
import subprocess
import tarfile
import threading
import urllib.request
from pathlib import Path
from typing import Literal

import pytest

from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.pinned_tools import PinnedTool, install_tool, load_manifest, resolve_tool, tools_dir


def _fake_tool_archive(
    path: Path, version: str, executables: tuple[str, ...] = ("fake",), mode: Literal["w:gz", "w:xz"] = "w:gz"
) -> str:
    """Write a tar archive holding, under `fake-1.0/`, shell scripts printing `version`; return its SHA-256."""
    script = f"#!/bin/sh\necho {version}\n".encode()
    with tarfile.open(path, mode) as tar:
        for executable in executables:
            info = tarfile.TarInfo(f"fake-1.0/{executable}")
            info.size = len(script)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(script))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_tool(archive: Path, sha256: str, name: str = "fake", executable: str = "fake", **kwargs) -> PinnedTool:
    return PinnedTool(
        name=name,
        version="1.0",
        url=archive.as_uri(),
        sha256=sha256,
        archive_root="fake-1.0",
        executable=executable,
        version_args=("--version",),
        version_pattern=r"^(\S+)$",
        **kwargs,
    )


def test_manifest_pins_exiftool_to_a_versioned_sourceforge_archive():
    exiftool = load_manifest()["exiftool"]
    assert (
        exiftool.url == f"https://downloads.sourceforge.net/project/exiftool/Image-ExifTool-{exiftool.version}.tar.gz"
    )
    assert len(exiftool.sha256) == 64


def test_manifest_pins_ffmpeg_and_ffprobe_to_one_versioned_static_build():
    manifest = load_manifest()
    ffmpeg, ffprobe = manifest["ffmpeg"], manifest["ffprobe"]
    assert (ffprobe.url, ffprobe.sha256, ffprobe.version) == (ffmpeg.url, ffmpeg.sha256, ffmpeg.version)
    # A month-end autobuild, kept two years by BtbN, never `latest` (replaced at every build)
    assert ffmpeg.url.startswith("https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-")
    assert ffmpeg.url.endswith(".tar.xz")
    assert len(ffmpeg.sha256) == 64


def test_pinned_ffmpeg_has_the_encoders_of_the_video_presets():
    encoders = subprocess.run(
        [str(resolve_tool("ffmpeg")), "-hide_banner", "-encoders"], capture_output=True, check=True, text=True
    ).stdout
    names = {line.split()[1] for line in encoders.splitlines() if len(line.split()) > 1}
    assert {"libx264", "libx265", "libopus"} <= names


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe"])
def test_resolve_ffmpeg_tools_never_looks_at_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str):
    monkeypatch.setenv("PATH", str(tmp_path))
    path = resolve_tool(name)
    assert path.name == name
    assert path.is_relative_to(tools_dir())


def test_resolve_exiftool_gives_the_installed_pinned_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    perl = shutil.which("perl")
    assert perl is not None
    (tmp_path / "perl").symlink_to(perl)
    monkeypatch.setenv("PATH", str(tmp_path))  # only Perl in PATH: the resolver must not look for exiftool there
    path = resolve_tool("exiftool")
    assert path.parent.name == f"exiftool-{load_manifest()['exiftool'].version}"


def test_resolve_missing_tool_names_the_install_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tmp_path))
    with pytest.raises(CommandError) as excinfo:
        resolve_tool("exiftool")
    error = excinfo.value
    assert isinstance(error, CommandError)
    assert error.code is ErrorCode.TOOL_MISSING
    assert error.details["tool"] == "exiftool"
    assert error.details["install_command"] == "just install-tools"


def test_install_extracts_a_verified_archive(tmp_path: Path):
    archive = tmp_path / "fake.tar.gz"
    tool = _fake_tool(archive, _fake_tool_archive(archive, "1.0"))
    root = tmp_path / "tools"
    executable = install_tool(tool, root)
    assert executable == root / "fake-1.0" / "fake"
    assert sorted(p.name for p in root.iterdir()) == ["fake-1.0"]


def test_install_accepts_a_tar_xz_archive(tmp_path: Path):
    """Characterises a criterion of #19 the install already met: tarfile detects xz on the content."""
    archive = tmp_path / "fake.tar.xz"
    tool = _fake_tool(archive, _fake_tool_archive(archive, "1.0", mode="w:xz"))
    executable = install_tool(tool, tmp_path / "tools")
    assert executable == tmp_path / "tools" / "fake-1.0" / "fake"


def test_tools_of_one_archive_share_a_single_install(tmp_path: Path):
    archive = tmp_path / "suite.tar.xz"
    sha256 = _fake_tool_archive(archive, "1.0", ("bin/one", "bin/two"), mode="w:xz")
    one = _fake_tool(archive, sha256, name="one", executable="bin/one", package="suite")
    two = _fake_tool(archive, sha256, name="two", executable="bin/two", package="suite")
    root = tmp_path / "tools"
    assert install_tool(one, root) == root / "suite-1.0" / "bin" / "one"
    archive.unlink()  # the second tool is already there: nothing is downloaded again
    assert install_tool(two, root) == root / "suite-1.0" / "bin" / "two"
    assert sorted(p.name for p in root.iterdir()) == ["suite-1.0"]


def _stale_temp_folder(root: Path, name: str) -> None:
    """What an install killed mid-download leaves: a hidden temporary folder holding part of the archive."""
    (root / name).mkdir(parents=True)
    (root / name / "archive").write_bytes(b"partial download")


def test_install_removes_the_temp_folders_of_an_interrupted_install(tmp_path: Path):
    archive = tmp_path / "suite.tar.xz"
    sha256 = _fake_tool_archive(archive, "1.0", ("bin/one",), mode="w:xz")
    root = tmp_path / "tools"
    _stale_temp_folder(root, ".suite-k3j9x2ab")
    _stale_temp_folder(root, ".one-p0q8z1x7")  # left by an install that still named it after the tool
    install_tool(_fake_tool(archive, sha256, name="one", executable="bin/one", package="suite"), root)
    assert sorted(p.name for p in root.iterdir()) == ["suite-1.0"]


def test_install_of_a_present_tool_still_removes_stale_temp_folders(tmp_path: Path):
    archive = tmp_path / "fake.tar.gz"
    tool = _fake_tool(archive, _fake_tool_archive(archive, "1.0"))
    root = tmp_path / "tools"
    install_tool(tool, root)
    _stale_temp_folder(root, ".fake-a1b2c3d4")
    install_tool(tool, root)
    assert sorted(p.name for p in root.iterdir()) == ["fake-1.0"]


def test_install_keeps_the_temp_folders_of_another_tool(tmp_path: Path):
    archive = tmp_path / "suite.tar.xz"
    sha256 = _fake_tool_archive(archive, "1.0", ("bin/one",), mode="w:xz")
    root = tmp_path / "tools"
    _stale_temp_folder(root, ".suite-normalize-k3j9x2ab")  # a tool whose name starts with the package's
    install_tool(_fake_tool(archive, sha256, name="one", executable="bin/one", package="suite"), root)
    assert sorted(p.name for p in root.iterdir()) == [".suite-normalize-k3j9x2ab", "suite-1.0"]


def test_concurrent_installs_on_one_folder_both_succeed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """`just qa` and `just test` run together: the second install must not sweep the first one's download."""
    archive = tmp_path / "suite.tar.xz"
    sha256 = _fake_tool_archive(archive, "1.0", ("bin/one",), mode="w:xz")
    tool = _fake_tool(archive, sha256, name="one", executable="bin/one", package="suite")
    root = tmp_path / "tools"
    first_downloading, release_first = threading.Event(), threading.Event()
    real_urlopen = urllib.request.urlopen

    def urlopen(url, timeout):
        if not first_downloading.is_set():  # the first install stops mid-download until released
            first_downloading.set()
            assert release_first.wait(10)
        return real_urlopen(url, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    errors: list[BaseException] = []

    def install() -> None:
        try:
            install_tool(tool, root)
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=install)
    first.start()
    assert first_downloading.wait(10)
    second = threading.Thread(target=install)
    second.start()
    second.join(0.5)  # time for the second install to reach its sweep, or to wait for the first one
    release_first.set()
    first.join(10)
    second.join(10)
    assert errors == [], [repr(e) for e in errors]
    assert sorted(p.name for p in root.iterdir()) == ["suite-1.0"]


def test_install_refuses_an_archive_with_another_checksum(tmp_path: Path):
    archive = tmp_path / "fake.tar.gz"
    _fake_tool_archive(archive, "1.0")
    tool = _fake_tool(archive, "0" * 64)
    root = tmp_path / "tools"
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        install_tool(tool, root)
    assert list(root.iterdir()) == []


def test_install_refuses_a_tool_reporting_another_version(tmp_path: Path):
    archive = tmp_path / "fake.tar.gz"
    tool = _fake_tool(archive, _fake_tool_archive(archive, "0.9"))
    with pytest.raises(RuntimeError, match="reports version 0.9"):
        install_tool(tool, tmp_path / "tools")
