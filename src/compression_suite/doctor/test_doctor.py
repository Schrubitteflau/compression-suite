"""CLI tests for doctor — the pinned exiftool runs for real, unless the tools folder is redirected.

The configuration folder is redirected to a temporary folder (`XDG_CONFIG_HOME`), never the user's.
"""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.utils.pinned_tools import load_manifest

runner = CliRunner()

KEY = "doctor-key-0123456789abcdef"
EXIFTOOL_VERSION = load_manifest()["exiftool"].version


@pytest.fixture
def config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty configuration folder, in place of the user's."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


def _write_config(config_home: Path, content: str, mode: int = 0o600) -> Path:
    """Write compression-suite's configuration file in `config_home`, with these permissions."""
    path = config_home / "compression-suite" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    return path


def _fake_exiftool(tools_dir: Path, version: str) -> Path:
    """Install in `tools_dir`, where the pinned exiftool is expected, a script printing `version`."""
    executable = tools_dir / f"exiftool-{EXIFTOOL_VERSION}" / "exiftool"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!/bin/sh\necho {version}\n")
    executable.chmod(0o755)
    return executable


def _invoke(*args: str) -> Result:
    return runner.invoke(app, ["doctor", *args])


def _report(result: Result) -> dict:
    """The doctor report of a `--json` run: `result` on success, `error.details` on failure."""
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["command"] == "doctor"
    return document["result"] if document["ok"] else document["error"]["details"]


def _by_name(entries: list[dict]) -> dict[str, dict]:
    return {entry["name"]: entry for entry in entries}


# --- Pinned tools ----------------------------------------------------------------


def test_nominal_run_reports_the_pinned_exiftool(config_home: Path):
    result = _invoke("--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    exiftool = _by_name(_report(result)["pinned_tools"])["exiftool"]
    assert exiftool["expected_version"] == EXIFTOOL_VERSION
    assert exiftool["found_version"] == EXIFTOOL_VERSION
    assert exiftool["status"] == "ok"
    assert Path(exiftool["path"]).is_file()
    assert exiftool["used_by"] == ["compress-image", "inspect"]


def test_missing_pinned_tool_fails_with_the_report(
    config_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tmp_path / "tools"))
    result = _invoke("--json")
    assert result.exit_code == 30, result.output
    document = json.loads(result.stdout)
    assert document["ok"] is False
    assert document["error"]["code"] == "tool_missing"
    assert "just install-tools" in document["error"]["message"]
    report = _report(result)
    assert report["tool"] == "exiftool"
    assert report["expected_version"] == EXIFTOOL_VERSION
    assert report["install_command"] == "just install-tools"
    exiftool = _by_name(report["pinned_tools"])["exiftool"]
    assert exiftool["status"] == "missing"
    assert exiftool["found_version"] is None
    assert exiftool["path"] == str(tmp_path / "tools" / f"exiftool-{EXIFTOOL_VERSION}" / "exiftool")
    assert {"system_dependencies", "tinify_api_key"} <= set(report)


def test_pinned_tool_of_another_version_is_a_version_mismatch(
    config_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _fake_exiftool(tmp_path / "tools", "1.0")
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tmp_path / "tools"))
    result = _invoke("--json")
    assert result.exit_code == 30, result.output
    exiftool = _by_name(_report(result)["pinned_tools"])["exiftool"]
    assert exiftool["status"] == "version_mismatch"
    assert exiftool["found_version"] == "1.0"


def test_missing_pinned_tool_fails_without_json(config_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tmp_path / "tools"))
    result = _invoke()
    assert result.exit_code == 30, result.output
    assert "exiftool" in result.stdout
    assert "missing" in result.stdout
    assert "just install-tools" in result.stderr


# --- System dependencies ---------------------------------------------------------


def test_system_dependencies_are_reported_with_their_commands(config_home: Path):
    result = _invoke("--json")
    assert result.exit_code == 0, result.output
    dependencies = _by_name(_report(result)["system_dependencies"])
    assert list(dependencies) == ["perl", "ffmpeg", "jpegoptim"]
    assert dependencies["perl"]["used_by"] == ["compress-image", "inspect"]
    assert dependencies["ffmpeg"]["used_by"] == ["extract-unique-frames", "reassemble-video"]
    assert dependencies["jpegoptim"]["used_by"] == ["reduce-jpeg-size"]
    perl = dependencies["perl"]
    assert perl["status"] == "ok"
    assert perl["path"] == shutil.which("perl")
    assert perl["found_version"][0].isdigit()


def test_missing_system_dependency_does_not_fail(config_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    perl = shutil.which("perl")
    assert perl is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "perl").symlink_to(perl)
    monkeypatch.setenv("PATH", str(bin_dir))  # Perl only: exiftool still runs, ffmpeg and jpegoptim are missing
    result = _invoke("--json")
    assert result.exit_code == 0, result.output
    dependencies = _by_name(_report(result)["system_dependencies"])
    assert dependencies["perl"]["path"] == str(bin_dir / "perl")
    for name in ("ffmpeg", "jpegoptim"):
        assert (dependencies[name]["found_version"], dependencies[name]["path"]) == (None, None)
        assert dependencies[name]["status"] == "missing"


# --- Tinify key --------------------------------------------------------------------


def test_missing_key_does_not_fail(config_home: Path):
    result = _invoke("--json")
    assert result.exit_code == 0, result.output
    assert _report(result)["tinify_api_key"] == {
        "configured": False,
        "source": "none",
        "config_path": str(config_home / "compression-suite" / "config.toml"),
        "permissions_ok": None,
    }


def test_present_key_is_reported_but_never_shown(config_home: Path):
    path = _write_config(config_home, f'tinify_api_key = "{KEY}"\n')
    json_run = _invoke("--json")
    human_run = _invoke()
    for result in (json_run, human_run):
        assert result.exit_code == 0, result.output
        assert KEY not in result.stdout
        assert KEY not in result.stderr
    assert _report(json_run)["tinify_api_key"] == {
        "configured": True,
        "source": "config_file",
        "config_path": str(path),
        "permissions_ok": True,
    }


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602, 0o610, 0o601])
def test_key_open_to_others_has_bad_permissions(config_home: Path, mode: int):
    path = _write_config(config_home, f'tinify_api_key = "{KEY}"\n', mode)
    result = _invoke("--json")
    assert result.exit_code == 0, result.output
    key = _report(result)["tinify_api_key"]
    assert key["configured"] is True
    assert key["permissions_ok"] is False
    human = _invoke()
    assert f"chmod 600 {path}" in human.stdout
    assert KEY not in human.stdout + human.stderr


def test_unreachable_configuration_folder_counts_as_no_key(config_home: Path):
    path = _write_config(config_home, f'tinify_api_key = "{KEY}"\n')
    path.parent.chmod(0o000)  # the file can't be reached, not even stat-ed
    try:
        result = _invoke("--json")
    finally:
        path.parent.chmod(0o700)
    assert result.exit_code == 0, result.output
    key = _report(result)["tinify_api_key"]
    assert (key["configured"], key["source"], key["permissions_ok"]) == (False, "none", None)


@pytest.mark.parametrize("content", ['tinify_api_key = ""\n', f'tinify_api_key = "{KEY}\n'])
def test_file_without_usable_key_is_not_configured(config_home: Path, content: str):
    _write_config(config_home, content)
    result = _invoke("--json")
    assert result.exit_code == 0, result.output
    key = _report(result)["tinify_api_key"]
    assert key["configured"] is False
    assert key["source"] == "none"
    assert key["permissions_ok"] is True
    assert KEY not in result.stdout + result.stderr


# --- Human output -------------------------------------------------------------------


def test_human_report_names_every_tool_and_the_key_file(config_home: Path):
    result = _invoke()
    assert result.exit_code == 0, result.output
    for name in ("exiftool", EXIFTOOL_VERSION, "perl", "ffmpeg", "jpegoptim", "reduce-jpeg-size"):
        assert name in result.stdout
    assert str(config_home / "compression-suite" / "config.toml") in result.stdout
