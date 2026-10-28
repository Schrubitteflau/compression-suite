"""Tests for pinned tools: the manifest, the resolver and the install, against a local archive."""

import hashlib
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.pinned_tools import PinnedTool, install_tool, load_manifest, resolve_tool


def _fake_tool_archive(path: Path, version: str) -> str:
    """Write a .tar.gz holding `fake-1.0/fake`, a shell script printing `version`; return its SHA-256."""
    script = f"#!/bin/sh\necho {version}\n".encode()
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("fake-1.0/fake")
        info.size = len(script)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(script))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_tool(archive: Path, sha256: str) -> PinnedTool:
    return PinnedTool(
        name="fake",
        version="1.0",
        url=archive.as_uri(),
        sha256=sha256,
        archive_root="fake-1.0",
        executable="fake",
        version_args=("--version",),
        version_pattern=r"^(\S+)$",
    )


def test_manifest_pins_exiftool_to_a_versioned_sourceforge_archive():
    exiftool = load_manifest()["exiftool"]
    assert exiftool.url == f"https://downloads.sourceforge.net/project/exiftool/Image-ExifTool-{exiftool.version}.tar.gz"
    assert len(exiftool.sha256) == 64


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
