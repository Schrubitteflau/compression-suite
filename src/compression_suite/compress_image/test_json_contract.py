"""CLI tests for the compress-image JSON contract and stable error codes — Tinify is mocked, exiftool runs for real."""

import io
import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tinify
from PIL import Image
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.utils.pinned_tools import load_manifest

runner = CliRunner()

EXIFTOOL_VERSION = load_manifest()["exiftool"].version

FTYP_HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"


def _image_bytes(fmt: str, size: tuple[int, int] = (32, 24)) -> bytes:
    """Encode a small image with Pillow, with an EXIF Make tag when the format allows it."""
    image = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "TestCamera"  # Make
    buf = io.BytesIO()
    if fmt in ("JPEG", "PNG", "WEBP"):
        image.save(buf, format=fmt, exif=exif)
    else:
        image.save(buf, format=fmt)
    return buf.getvalue()


def _fake_tinify(output: bytes | None = None, error: BaseException | None = None):
    """Patch tinify so the compressed result is `output` (or the input itself), or raises `error`."""
    mock = MagicMock()
    mock.compression_count = 42

    def make_source(data: bytes):
        source = MagicMock()
        if error is not None:
            source.to_buffer.side_effect = error
        else:
            source.to_buffer.return_value = output if output is not None else data
        return source

    mock.from_file.side_effect = lambda path: make_source(Path(path).read_bytes())
    mock.from_buffer.side_effect = make_source
    return patch("compression_suite.compress_image.main.tinify", mock)


def _invoke(*args: str, input: bytes | None = None):
    return runner.invoke(app, ["compress-image", *args, "--api-key", "test-key"], input=input)


def _json(result) -> dict:
    return json.loads(result.stdout)


def _assert_error(result, code: str, exit_code: int) -> dict:
    assert result.exit_code == exit_code, result.output
    doc = _json(result)
    assert doc["schema_version"] == 1
    assert doc["ok"] is False
    assert doc["command"] == "compress-image"
    assert "result" not in doc
    assert doc["error"]["code"] == code
    assert isinstance(doc["error"]["message"], str) and doc["error"]["message"]
    assert isinstance(doc["error"]["details"], dict)
    return doc["error"]


def _assert_tinify_not_called(fake: MagicMock):
    assert not fake.from_file.called
    assert not fake.from_buffer.called


# --- Success -----------------------------------------------------------------


def test_success_envelope_and_result(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    out = tmp_path / "out" / "photo.jpg"
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(out), "--json")

    assert result.exit_code == 0, result.output
    doc = _json(result)
    assert doc["schema_version"] == 1
    assert doc["ok"] is True
    assert doc["command"] == "compress-image"
    assert "error" not in doc
    res = doc["result"]
    input_mtime = res["input"].pop("mtime")
    assert isinstance(input_mtime, str)
    assert res["output"].pop("mtime") == input_mtime
    assert res["input"] == {"path": str(src), "format": "jpeg", "size_bytes": src.stat().st_size}
    assert res["output"] == {"path": str(out), "format": "jpeg", "size_bytes": out.stat().st_size}
    assert res["tinify"] == {"compressions_this_month": 42}


def test_format_is_detected_on_content_not_extension(tmp_path: Path):
    src = tmp_path / "screenshot.jpg"
    src.write_bytes(_image_bytes("PNG"))
    out = tmp_path / "out.png"
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(out), "--json")
    assert result.exit_code == 0, result.output
    res = _json(result)["result"]
    assert res["input"]["format"] == "png"
    assert res["output"]["format"] == "png"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_image_bytes("WEBP"), "webp"),
        (_image_bytes("AVIF"), "avif"),
    ],
)
def test_other_supported_formats(tmp_path: Path, data: bytes, expected: str):
    src = tmp_path / "image.bin"
    src.write_bytes(data)
    out = tmp_path / "out.bin"
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(out), "--metadata", "strip", "--json")
    assert result.exit_code == 0, result.output
    assert _json(result)["result"]["input"]["format"] == expected


def test_output_written_even_when_larger_than_original(tmp_path: Path):
    original = _image_bytes("JPEG")
    src = tmp_path / "photo.jpg"
    src.write_bytes(original)
    bigger = original + b"\0" * 5000
    out = tmp_path / "out.jpg"
    with _fake_tinify(output=bigger):
        result = _invoke(str(src), "--output", str(out), "--metadata", "strip", "--json")
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == bigger
    res = _json(result)["result"]
    assert res["output"]["size_bytes"] > res["input"]["size_bytes"]


def test_stdin_with_json_reports_null_input_path(tmp_path: Path):
    out = tmp_path / "out.jpg"
    with _fake_tinify():
        result = _invoke("--output", str(out), "--json", input=_image_bytes("JPEG"))
    assert result.exit_code == 0, result.output
    assert _json(result)["result"]["input"]["path"] is None


def test_atomic_write_leaves_no_temporary_file(tmp_path: Path):
    src = tmp_path / "src" / "photo.jpg"
    src.parent.mkdir()
    src.write_bytes(_image_bytes("JPEG"))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(out_dir / "photo.jpg"), "--json")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out_dir.iterdir()) == ["photo.jpg"]


def test_overwrite_replaces_existing_output(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    out = tmp_path / "out.jpg"
    out.write_bytes(b"old")
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(out), "--overwrite", "--json")
    assert result.exit_code == 0, result.output
    assert out.read_bytes()[:2] == b"\xff\xd8"


def test_works_with_only_perl_in_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The pinned exiftool is used, never one from PATH, and jpegoptim is not needed: only Perl is."""
    perl = shutil.which("perl")
    assert perl is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "perl").symlink_to(perl)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("exiftool") is None
    assert shutil.which("jpegoptim") is None

    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    assert result.exit_code == 0, result.output
    assert _json(result)["ok"] is True


# --- Errors detected before Tinify -------------------------------------------


def test_input_not_found(tmp_path: Path):
    with _fake_tinify() as fake:
        result = _invoke(str(tmp_path / "missing.jpg"), "--output", str(tmp_path / "out.jpg"), "--json")
    _assert_error(result, "input_not_found", 23)
    _assert_tinify_not_called(fake)


def test_input_is_a_directory(tmp_path: Path):
    with _fake_tinify() as fake:
        result = _invoke(str(tmp_path), "--output", str(tmp_path / "out.jpg"), "--json")
    _assert_error(result, "input_not_found", 23)
    _assert_tinify_not_called(fake)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_input_unreadable(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    src.chmod(0)
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    _assert_error(result, "input_unreadable", 26)
    _assert_tinify_not_called(fake)


@pytest.mark.parametrize(
    ("data", "detected"),
    [
        (_image_bytes("GIF"), "gif"),
        (FTYP_HEIC, "heic"),
        (b"%PDF-1.7\n", "pdf"),
        (b"just some text", "unknown"),
    ],
)
def test_unsupported_format(tmp_path: Path, data: bytes, detected: str):
    src = tmp_path / "disguised.jpg"
    src.write_bytes(data)
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    error = _assert_error(result, "unsupported_format", 21)
    assert error["details"] == {"detected_format": detected}
    _assert_tinify_not_called(fake)


def test_file_too_large(tmp_path: Path):
    src = tmp_path / "huge.jpg"
    size = 15 * 1024 * 1024 + 1
    with src.open("wb") as f:
        f.write(b"\xff\xd8\xff\xe0")
        f.truncate(size)
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    error = _assert_error(result, "file_too_large", 22)
    assert error["details"] == {"size_bytes": size, "limit_bytes": 15 * 1024 * 1024}
    _assert_tinify_not_called(fake)


def test_file_too_large_from_stdin(tmp_path: Path):
    data = b"\xff\xd8\xff\xe0" + b"\0" * (15 * 1024 * 1024)
    with _fake_tinify() as fake:
        result = _invoke("--output", str(tmp_path / "out.jpg"), "--json", input=data)
    error = _assert_error(result, "file_too_large", 22)
    assert error["details"]["size_bytes"] == len(data)
    _assert_tinify_not_called(fake)


def test_output_exists(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    out = tmp_path / "out.jpg"
    out.write_bytes(b"existing")
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(out), "--json")
    _assert_error(result, "output_exists", 24)
    _assert_tinify_not_called(fake)
    assert out.read_bytes() == b"existing"


def test_json_requires_output(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--json")
    _assert_error(result, "usage_error", 2)
    _assert_tinify_not_called(fake)


def test_pinned_exiftool_missing_gives_tool_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tmp_path / "no-tools"))
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    error = _assert_error(result, "tool_missing", 30)
    assert error["details"]["tool"] == "exiftool"
    assert error["details"]["install_command"] == "just install-tools"
    assert "just install-tools" in error["message"]
    _assert_tinify_not_called(fake)


def test_pinned_exiftool_with_wrong_version_gives_tool_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools_dir = tmp_path / "tools"
    fake_dir = tools_dir / f"exiftool-{EXIFTOOL_VERSION}"
    fake_dir.mkdir(parents=True)
    fake_exiftool = fake_dir / "exiftool"
    fake_exiftool.write_text("#!/bin/sh\necho 11.88\n")
    fake_exiftool.chmod(0o755)
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tools_dir))
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    error = _assert_error(result, "tool_missing", 30)
    assert error["details"]["tool"] == "exiftool"
    assert "11.88" in error["message"]
    _assert_tinify_not_called(fake)


def _truncated(fmt: str) -> bytes:
    """A Pillow image of a known format, cut in half as by an interrupted synchronisation."""
    data = _image_bytes(fmt, size=(128, 96))
    return data[: len(data) // 2]


@pytest.mark.parametrize(("fmt", "detected"), [("JPEG", "jpeg"), ("PNG", "png"), ("WEBP", "webp"), ("AVIF", "avif")])
def test_truncated_input_is_input_corrupt_before_tinify(tmp_path: Path, fmt: str, detected: str):
    src = tmp_path / "photo.bin"
    src.write_bytes(_truncated(fmt))
    out = tmp_path / "out.bin"
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(out), "--json")
    error = _assert_error(result, "input_corrupt", 27)
    assert error["details"] == {"detected_format": detected}
    _assert_tinify_not_called(fake)
    assert not out.exists()


def test_truncated_jpeg_message_is_the_decoder_one(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_truncated("JPEG"))
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    assert "truncated" in _assert_error(result, "input_corrupt", 27)["message"].lower()  # Pillow's own message


def test_jpeg_header_alone_is_input_corrupt(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"\xff\xd8\xff")  # exiftool still describes it, with a warning
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    _assert_error(result, "input_corrupt", 27)
    _assert_tinify_not_called(fake)


def test_truncated_input_from_stdin_is_input_corrupt(tmp_path: Path):
    with _fake_tinify() as fake:
        result = _invoke("--output", str(tmp_path / "out.jpg"), "--json", input=_truncated("JPEG"))
    error = _assert_error(result, "input_corrupt", 27)
    assert error["details"] == {"detected_format": "jpeg"}
    _assert_tinify_not_called(fake)


def test_crashing_exiftool_gives_internal_error_before_tinify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools_dir = tmp_path / "tools"
    fake_dir = tools_dir / f"exiftool-{EXIFTOOL_VERSION}"
    fake_dir.mkdir(parents=True)
    fake_exiftool = fake_dir / "exiftool"
    # The right version, but it dies on any file without writing anything
    fake_exiftool.write_text(
        f'#!/bin/sh\n[ "$1" = -ver ] && echo {EXIFTOOL_VERSION} && exit 0\necho boom >&2\nexit 1\n'
    )
    fake_exiftool.chmod(0o755)
    monkeypatch.setenv("COMPRESSION_SUITE_TOOLS_DIR", str(tools_dir))
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    error = _assert_error(result, "internal_error", 50)
    assert "exiftool" in error["message"]
    assert "boom" in error["message"]
    _assert_tinify_not_called(fake)


# --- Errors from Tinify and later ---------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "exit_code"),
    [
        (tinify.ServerError("Service unavailable", "ServiceUnavailable", 503), "tinify_unavailable", 10),
        (tinify.ConnectionError("Error while connecting"), "tinify_unavailable", 10),
        (tinify.AccountError("Credentials are invalid", "Unauthorized", 401), "api_key_invalid", 11),
        (tinify.ClientError("Bad request", "BadRequest", 400), "api_key_invalid", 11),
        (tinify.AccountError("Your monthly limit has been exceeded", "TooManyRequests", 429), "quota_exceeded", 12),
    ],
)
def test_tinify_errors(tmp_path: Path, error: Exception, code: str, exit_code: int):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    out = tmp_path / "out.jpg"
    with _fake_tinify(error=error):
        result = _invoke(str(src), "--output", str(out), "--json")
    _assert_error(result, code, exit_code)
    assert not out.exists()


def test_metadata_copy_failed_leaves_no_output(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with _fake_tinify(output=b"not an image at all"):
        result = _invoke(str(src), "--output", str(out_dir / "photo.jpg"), "--json")
    _assert_error(result, "metadata_copy_failed", 25)
    assert list(out_dir.iterdir()) == []


def test_unexpected_exception_gives_internal_error(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify(error=ZeroDivisionError("boom")):
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    _assert_error(result, "internal_error", 50)


def test_keyboard_interrupt_gives_interrupted(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify(error=KeyboardInterrupt()):
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json")
    _assert_error(result, "interrupted", 130)


def test_output_is_a_directory_refused_before_tinify_even_with_overwrite(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    out = tmp_path / "out.jpg"
    out.mkdir()
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(out), "--overwrite", "--json")
    _assert_error(result, "output_exists", 24)
    _assert_tinify_not_called(fake)


# --- Human mode (no --json) ----------------------------------------------------


def test_human_mode_interrupt_message(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify(error=KeyboardInterrupt()):
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"))
    assert result.exit_code == 130
    assert "Interrupted by user" in result.stderr
    assert "Error" not in result.stderr


def test_human_mode_same_exit_codes_and_readable_stderr(tmp_path: Path):
    src = tmp_path / "anim.gif"
    src.write_bytes(_image_bytes("GIF"))
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(tmp_path / "out.gif"))
    assert result.exit_code == 21
    assert result.stdout == ""
    assert "gif" in result.stderr.lower()


def test_human_mode_tinify_error_exit_code(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    quota = tinify.AccountError("Your monthly limit has been exceeded", "TooManyRequests", 429)
    with _fake_tinify(error=quota):
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"))
    assert result.exit_code == 12
    assert result.stdout == ""
    assert "limit" in result.stderr


def test_human_mode_success_reports_on_stderr(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify():
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"))
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "Compressed" in result.stderr


def test_human_mode_stdin_to_stdout_binary():
    data = _image_bytes("JPEG")
    compressed = data + b"\0" * 10
    with _fake_tinify(output=compressed):
        result = _invoke("--metadata", "strip", input=data)
    assert result.exit_code == 0, result.stderr
    assert result.stdout_bytes == compressed


def test_output_permissions_follow_umask(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    out = tmp_path / "out.jpg"
    old_umask = os.umask(0o022)
    try:
        with _fake_tinify():
            result = _invoke(str(src), "--output", str(out), "--json")
    finally:
        os.umask(old_umask)
    assert result.exit_code == 0, result.output
    assert out.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "args",
    [
        ("--metadata", "bogus"),
        ("--unknown-flag",),
    ],
)
def test_typer_usage_errors_give_json_usage_error(tmp_path: Path, args: tuple[str, ...]):
    src = tmp_path / "photo.jpg"
    src.write_bytes(_image_bytes("JPEG"))
    with _fake_tinify() as fake:
        result = _invoke(str(src), "--output", str(tmp_path / "out.jpg"), "--json", *args)
    _assert_error(result, "usage_error", 2)
    _assert_tinify_not_called(fake)


def test_typer_usage_error_without_json_keeps_typer_message(tmp_path: Path):
    result = _invoke(str(tmp_path / "x.jpg"), "--metadata", "bogus")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "bogus" in result.stderr
