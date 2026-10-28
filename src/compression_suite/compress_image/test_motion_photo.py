"""CLI tests for the motion photo refusal of compress-image — Tinify is faked, exiftool runs for real.

Each fabricated sample isolates one case; the real S23 motion photo carries the Google and Samsung formats at once.
"""

import io
import json
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import Result
from PIL import Image
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

SAMPLES = Path(__file__).resolve().parent / "samples"
GOOGLE_MOTION_PHOTO = SAMPLES / "google_motion_photo.jpg"
SAMSUNG_MOTION_PHOTO = SAMPLES / "samsung_motion_photo.jpg"
TRAILING_DATA = SAMPLES / "trailing_data.jpg"
S23_MOTION_PHOTO = SAMPLES / "s23_motion_photo.jpg"
S23_PHOTO = SAMPLES / "s23_p3_seft.jpg"
SCREENSHOT = SAMPLES / "screenshot.png"

GCAMERA_XMP = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"
        xmlns:Container="http://ns.google.com/photos/1.0/container/"
        xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
      {attributes}>
      {directory}
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>"""

CONTAINER_DIRECTORY = """<Container:Directory>
        <rdf:Seq>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"/>
          </rdf:li>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="{mime}" Item:Semantic="MotionPhoto" Item:Length="0"/>
          </rdf:li>
        </rdf:Seq>
      </Container:Directory>"""


def _fake_tinify():
    """Patch tinify so the compressed result is the input itself."""
    mock = MagicMock()
    mock.compression_count = 42

    def make_source(data: bytes):
        source = MagicMock()
        source.to_buffer.return_value = data
        return source

    mock.from_file.side_effect = lambda path: make_source(Path(path).read_bytes())
    mock.from_buffer.side_effect = make_source
    return patch("compression_suite.compress_image.main.tinify", mock)


def _invoke(src: Path | None, out: Path, *args: str, input: bytes | None = None) -> tuple[Result, MagicMock]:
    """Run compress-image --json, returning the CLI result and the fake Tinify."""
    with _fake_tinify() as fake:
        result = runner.invoke(
            app,
            [
                "compress-image", *([str(src)] if src is not None else []), "--output", str(out),
                "--api-key", "test-key", "--json", *args,
            ],
            input=input,
        )
    return result, fake


def _assert_refused(result: Result, fake: MagicMock, out: Path) -> list[str]:
    """Check the `motion_photo` refusal, made before Tinify, and return its signals."""
    assert result.exit_code == 20, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "motion_photo"
    assert not fake.from_file.called
    assert not fake.from_buffer.called
    assert not out.exists()
    return error["details"]["signals"]


def _motion_photo_result(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["result"]["motion_photo"]


def _jpeg(xmp: str | None = None) -> bytes:
    image = Image.new("RGB", (64, 48), (53, 132, 228))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", **({"xmp": xmp.encode()} if xmp is not None else {}))
    return buf.getvalue()


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- Each signal, isolated ----------------------------------------------------


def test_google_motion_photo_is_refused(tmp_path: Path):
    out = tmp_path / "out.jpg"
    signals = _assert_refused(*_invoke(GOOGLE_MOTION_PHOTO, out), out)
    # The appended video is explained by the XMP alone, which is no structure the end of the JPEG accounts for
    assert signals == ["xmp_google_motion_photo", "xmp_container_video", "trailing_data"]


def test_samsung_motion_photo_is_refused(tmp_path: Path):
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(SAMSUNG_MOTION_PHOTO, out), out) == ["samsung_motion_photo_data"]


def test_trailing_data_is_refused(tmp_path: Path):
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(TRAILING_DATA, out), out) == ["trailing_data"]


@pytest.mark.parametrize(
    ("attributes", "directory", "signal"),
    [
        ('GCamera:MotionPhoto="1"', "", "xmp_google_motion_photo"),
        ('GCamera:MicroVideo="1"', "", "xmp_google_micro_video"),
        ("", CONTAINER_DIRECTORY.format(mime="video/mp4"), "xmp_container_video"),
    ],
)
def test_each_xmp_signal_alone_is_refused(tmp_path: Path, attributes: str, directory: str, signal: str):
    src = _write(tmp_path, "photo.jpg", _jpeg(GCAMERA_XMP.format(attributes=attributes, directory=directory)))
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == [signal]


@pytest.mark.parametrize(
    ("attributes", "directory"),
    [
        ('GCamera:MotionPhoto="0"', ""),
        ('GCamera:MicroVideo="0"', ""),
        ("", CONTAINER_DIRECTORY.format(mime="image/jpeg")),
    ],
)
def test_xmp_without_video_is_accepted(tmp_path: Path, attributes: str, directory: str):
    src = _write(tmp_path, "photo.jpg", _jpeg(GCAMERA_XMP.format(attributes=attributes, directory=directory)))
    assert _motion_photo_result(_invoke(src, tmp_path / "out.jpg")[0]) == {"detected": False, "signals": []}


def test_motion_photo_from_stdin_is_refused(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, fake = _invoke(None, out, input=SAMSUNG_MOTION_PHOTO.read_bytes())
    assert _assert_refused(result, fake, out) == ["samsung_motion_photo_data"]


# --- Real S23 motion photo: both formats at once --------------------------------


def test_s23_motion_photo_gives_a_single_refusal_listing_both_formats(tmp_path: Path):
    out = tmp_path / "out.jpg"
    signals = _assert_refused(*_invoke(S23_MOTION_PHOTO, out), out)
    # The video lies in the `SEFT` zone, which explains every byte after the image
    assert signals == ["xmp_google_motion_photo", "xmp_container_video", "samsung_motion_photo_data"]


def test_s23_motion_photo_error_message_names_the_flag(tmp_path: Path):
    with _fake_tinify():
        result = runner.invoke(
            app, ["compress-image", str(S23_MOTION_PHOTO), "--output", str(tmp_path / "out.jpg"), "--api-key", "k"],
        )
    assert result.exit_code == 20
    assert "--allow-motion-photo-loss" in result.stderr
    assert "samsung_motion_photo_data" in result.stderr


# --- Known structures ----------------------------------------------------------


def test_s23_photo_with_seft_zone_without_motion_photo_is_accepted(tmp_path: Path):
    result, fake = _invoke(S23_PHOTO, tmp_path / "out.jpg")
    assert _motion_photo_result(result) == {"detected": False, "signals": []}
    assert fake.from_file.called


def test_mpf_images_are_not_trailing_data(tmp_path: Path):
    """The secondary images an MPF segment declares, as in a Samsung JPEG, follow the main image's EOI."""
    src = tmp_path / "photo.jpg"
    first = Image.new("RGB", (64, 48), (53, 132, 228))
    second = Image.new("RGB", (32, 24), (200, 30, 30))
    first.save(src, format="MPO", save_all=True, append_images=[second])
    assert b"MPF\0" in src.read_bytes()
    assert _motion_photo_result(_invoke(src, tmp_path / "out.jpg")[0]) == {"detected": False, "signals": []}


def test_bytes_after_mpf_images_are_trailing_data(tmp_path: Path):
    mpo = io.BytesIO()
    Image.new("RGB", (64, 48)).save(mpo, format="MPO", save_all=True, append_images=[Image.new("RGB", (32, 24))])
    src = _write(tmp_path, "photo.jpg", mpo.getvalue() + b"unexplained")
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_unreadable_seft_index_is_trailing_data(tmp_path: Path):
    data = bytearray(S23_PHOTO.read_bytes())
    index_length = struct.unpack("<I", data[-8:-4])[0]
    index_start = len(data) - 8 - index_length
    data[index_start + 8 : index_start + 12] = struct.pack("<I", 1000)  # more entries than the index holds
    src = _write(tmp_path, "photo.jpg", bytes(data))
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_bytes_outside_the_seft_zone_are_trailing_data(tmp_path: Path):
    """The index locates its entries back from itself: bytes inserted before the zone leave it readable."""
    data = S23_PHOTO.read_bytes()
    image_end = data.index(b"Image_UTC_Data") - 8
    src = _write(tmp_path, "photo.jpg", data[:image_end] + b"unexplained" + data[image_end:])
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_motion_photo_data_in_an_unreadable_index_is_only_trailing_data(tmp_path: Path):
    data = bytearray(SAMSUNG_MOTION_PHOTO.read_bytes())
    data[-12:-8] = b"XXXX"  # last entry's size, past its distance back
    src = _write(tmp_path, "photo.jpg", bytes(data))
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


# --- Other formats -------------------------------------------------------------


def test_png_bytes_after_iend_are_trailing_data(tmp_path: Path):
    src = _write(tmp_path, "capture.png", SCREENSHOT.read_bytes() + b"unexplained")
    out = tmp_path / "out.png"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_png_without_trailing_bytes_is_accepted(tmp_path: Path):
    assert _motion_photo_result(_invoke(SCREENSHOT, tmp_path / "out.png")[0]) == {"detected": False, "signals": []}


def _webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (53, 132, 228)).save(buf, format="WEBP")
    return buf.getvalue()


def test_webp_bytes_beyond_the_riff_size_are_trailing_data(tmp_path: Path):
    src = _write(tmp_path, "image.webp", _webp() + b"unexplained")
    out = tmp_path / "out.webp"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_webp_without_trailing_bytes_is_accepted(tmp_path: Path):
    src = _write(tmp_path, "image.webp", _webp())
    assert _motion_photo_result(_invoke(src, tmp_path / "out.webp")[0]) == {"detected": False, "signals": []}


# --- --allow-motion-photo-loss ---------------------------------------------------


def test_allow_motion_photo_loss_compresses_and_reports_the_signals(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, fake = _invoke(S23_MOTION_PHOTO, out, "--allow-motion-photo-loss")
    assert _motion_photo_result(result) == {
        "detected": True,
        "signals": ["xmp_google_motion_photo", "xmp_container_video", "samsung_motion_photo_data"],
    }
    assert fake.from_file.called
    assert out.exists()


def test_allow_motion_photo_loss_without_motion_photo_reports_none(tmp_path: Path):
    result, _ = _invoke(S23_PHOTO, tmp_path / "out.jpg", "--allow-motion-photo-loss")
    assert _motion_photo_result(result) == {"detected": False, "signals": []}
