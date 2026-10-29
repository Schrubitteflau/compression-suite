"""CLI tests for the Tinify key of compress-image: `--api-key`, else the configuration file — Tinify is faked.

The configuration folder is redirected to a temporary folder (`XDG_CONFIG_HOME`), never the user's.
"""

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import Result
from PIL import Image
from tinify import ClientError
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

FILE_KEY = "file-key-0123456789abcdef"
FLAG_KEY = "flag-key-fedcba9876543210"


@pytest.fixture
def config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty configuration folder, in place of the user's."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


def _write_config(config_home: Path, content: str, mode: int = 0o600) -> Path:
    path = config_home / "compression-suite" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    return path


def _jpeg(tmp_path: Path) -> Path:
    path = tmp_path / "photo.jpg"
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (53, 132, 228)).save(buf, format="JPEG")
    path.write_bytes(buf.getvalue())
    return path


def _fake_tinify(output: bytes, error: BaseException | None = None):
    """Patch tinify so the compressed result is `output`, or the call raises `error`."""
    mock = MagicMock()
    mock.key = None
    mock.compression_count = 42
    source = mock.from_file.return_value
    source.to_buffer.return_value = output
    source.to_buffer.side_effect = error
    return patch("compression_suite.compress_image.main.tinify", mock)


def _compress(tmp_path: Path, *args: str, error: BaseException | None = None) -> tuple[Result, MagicMock]:
    """Run compress-image --json on a small JPEG, without --api-key unless `args` gives it."""
    src = _jpeg(tmp_path)
    with _fake_tinify(src.read_bytes(), error) as fake:
        result = runner.invoke(
            app, ["compress-image", str(src), "--output", str(tmp_path / "out.jpg"), "--json", *args],
        )
    return result, fake


def test_key_is_read_from_the_configuration_file(config_home: Path, tmp_path: Path):
    _write_config(config_home, f'tinify_api_key = "{FILE_KEY}"\n')
    result, fake = _compress(tmp_path)
    assert result.exit_code == 0, result.output
    assert fake.key == FILE_KEY
    assert fake.from_file.called


def test_api_key_flag_wins_over_the_file(config_home: Path, tmp_path: Path):
    _write_config(config_home, f'tinify_api_key = "{FILE_KEY}"\n')
    result, fake = _compress(tmp_path, "--api-key", FLAG_KEY)
    assert result.exit_code == 0, result.output
    assert fake.key == FLAG_KEY


def test_configuration_folder_defaults_to_home_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(home / ".config", f'tinify_api_key = "{FILE_KEY}"\n')
    result, fake = _compress(tmp_path)
    assert result.exit_code == 0, result.output
    assert fake.key == FILE_KEY


@pytest.mark.parametrize("content", [None, "", 'other = "x"\n', 'tinify_api_key = ""\n', "tinify_api_key = 42\n"])
def test_no_key_is_api_key_missing_before_tinify(config_home: Path, tmp_path: Path, content: str | None):
    if content is not None:
        _write_config(config_home, content)
    result, fake = _compress(tmp_path)
    assert result.exit_code == 13, result.output
    error = json.loads(result.stdout)["error"]
    expected_path = str(config_home / "compression-suite" / "config.toml")
    assert error["code"] == "api_key_missing"
    assert error["details"] == {"config_path": expected_path}
    assert expected_path in error["message"]
    assert not fake.from_file.called
    assert not (tmp_path / "out.jpg").exists()


def test_existing_output_is_reported_before_a_missing_key(config_home: Path, tmp_path: Path):
    (tmp_path / "out.jpg").write_bytes(b"old")
    result, _ = _compress(tmp_path)
    assert result.exit_code == 24, result.output
    assert json.loads(result.stdout)["error"]["code"] == "output_exists"


def test_unparsable_file_is_reported_without_its_content(config_home: Path, tmp_path: Path):
    path = _write_config(config_home, f'tinify_api_key = "{FILE_KEY}\n')  # unterminated string
    result, fake = _compress(tmp_path)
    assert result.exit_code == 13, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "api_key_missing"
    assert str(path) in error["message"]
    assert FILE_KEY not in result.output
    assert not fake.from_file.called


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_file_readable_by_others_gives_a_warning(config_home: Path, tmp_path: Path, mode: int):
    path = _write_config(config_home, f'tinify_api_key = "{FILE_KEY}"\n', mode)
    result, _ = _compress(tmp_path)
    assert result.exit_code == 0, result.output
    assert "Warning" in result.stderr
    assert str(path) in result.stderr
    assert "chmod 600" in result.stderr


def test_private_file_gives_no_warning(config_home: Path, tmp_path: Path):
    _write_config(config_home, f'tinify_api_key = "{FILE_KEY}"\n')
    result, _ = _compress(tmp_path)
    assert result.exit_code == 0, result.output
    assert "Warning" not in result.stderr


def test_key_never_appears_in_any_output(config_home: Path, tmp_path: Path):
    _write_config(config_home, f'tinify_api_key = "{FILE_KEY}"\n', 0o644)
    ok, _ = _compress(tmp_path, "--verbose")
    (tmp_path / "out.jpg").unlink()
    refused, _ = _compress(tmp_path, error=ClientError("Credentials are invalid", "Unauthorized", 401))
    assert ok.exit_code == 0, ok.output
    assert refused.exit_code == 11, refused.output
    for result in (ok, refused):
        assert FILE_KEY not in result.stdout
        assert FILE_KEY not in result.stderr
