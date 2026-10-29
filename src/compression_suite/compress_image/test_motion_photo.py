"""CLI tests for the motion photos of compress-image — Tinify is faked, exiftool runs for real.

A Samsung motion photo keeps its video in the `SEFT` zone appended back after the compressed image; any other is
refused, unless its loss is allowed. Each fabricated sample isolates one case; the real S23 motion photo carries the
Google and Samsung formats at once.
"""

import io
import json
import struct
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import Result
from PIL import Image, ImageCms, ImageOps
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.utils.motion_photo import image_end
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.utils.seft import SeftZone, read_seft

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


MOTION_PHOTO_REASON = "photo animée : perte autorisée"
STRIP_REASON = "métadonnées retirées à la demande"

NO_MOTION_PHOTO = {"detected": False, "signals": [], "video_kept": None}
S23_SIGNALS = ["xmp_google_motion_photo", "xmp_container_video", "samsung_motion_photo_data"]


def _reencode_like_tinify(data: bytes) -> bytes:
    """Re-encode the pixels without any metadata nor trailer, as Tinify does, with the orientation applied to them and
    the colours converted to sRGB with their ICC profile."""
    image = Image.open(io.BytesIO(data))
    icc_profile = image.info.get("icc_profile")
    image = ImageOps.exif_transpose(image)
    if icc_profile:
        source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
        converted = ImageCms.profileToProfile(image, source_profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
        assert converted is not None  # only None with inPlace=True
        image = converted
    image.info.clear()
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _fake_tinify(reencode: bool = False, tail: bytes = b""):
    """Patch tinify so the compressed result is the input itself, or the input re-encoded like Tinify, then `tail`."""
    mock = MagicMock()
    mock.compression_count = 42

    def make_source(data: bytes):
        source = MagicMock()
        source.to_buffer.return_value = (_reencode_like_tinify(data) if reencode else data) + tail
        return source

    mock.from_file.side_effect = lambda path: make_source(Path(path).read_bytes())
    mock.from_buffer.side_effect = make_source
    return patch("compression_suite.compress_image.main.tinify", mock)


def _invoke(
    src: Path | None,
    out: Path,
    *args: str,
    input: bytes | None = None,
    reencode: bool = False,
    tail: bytes = b"",
) -> tuple[Result, MagicMock]:
    """Run compress-image --json, returning the CLI result and the fake Tinify."""
    with _fake_tinify(reencode, tail) as fake:
        result = runner.invoke(
            app,
            [
                "compress-image",
                *([str(src)] if src is not None else []),
                "--output",
                str(out),
                "--api-key",
                "test-key",
                "--json",
                *args,
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


def _motion_photo_result(result: Result) -> dict:
    """Check the compression succeeded and return its `motion_photo` result."""
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["result"]["motion_photo"]


def _metadata(result: Result) -> dict:
    """Check the compression succeeded and return its metadata diff."""
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["result"]["metadata"]


def _expected_reasons(metadata: dict) -> dict[str, str]:
    """The reason of each expected loss, by tag."""
    return {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}


def _zone(data: bytes) -> SeftZone:
    """The file's `SEFT` zone, which must be there."""
    zone = read_seft(data)
    assert zone is not None
    return zone


def _samsung_video(data: bytes) -> bytes:
    """The value of the `MotionPhoto_Data` entry of the file's `SEFT` zone."""
    entry = _zone(data).entry("MotionPhoto_Data")
    assert entry is not None
    return data[entry.value_offset : entry.offset + entry.size]


def _zone_bytes(data: bytes) -> bytes:
    """The `SEFT` zone of the file, from its farthest entry to the end."""
    return data[_zone(data).start :]


def _with_bytes_after_the_image(sample: Path, tmp_path: Path) -> Path:
    """The sample with unexplained bytes between its image and its `SEFT` zone: its zone stays readable."""
    data = sample.read_bytes()
    end = image_end(data)
    assert end is not None
    return _write(tmp_path, f"trailing_{sample.name}", data[:end] + b"unexplained" + data[end:])


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
    # The appended video is the item `Container:Directory` declares: no trailing data
    assert signals == ["xmp_google_motion_photo", "xmp_container_video"]


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
    assert _motion_photo_result(_invoke(src, tmp_path / "out.jpg")[0]) == NO_MOTION_PHOTO


def test_motion_photo_from_stdin_is_refused(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, fake = _invoke(None, out, input=GOOGLE_MOTION_PHOTO.read_bytes())
    assert _assert_refused(result, fake, out) == ["xmp_google_motion_photo", "xmp_container_video"]


# --- Samsung motion photo: its video kept in the `SEFT` zone ---------------------


@pytest.mark.parametrize(
    ("sample", "signals"),
    [(S23_MOTION_PHOTO, S23_SIGNALS), (SAMSUNG_MOTION_PHOTO, ["samsung_motion_photo_data"])],
)
def test_samsung_motion_photo_keeps_its_video(tmp_path: Path, sample: Path, signals: list[str]):
    out = tmp_path / "out.jpg"
    result, fake = _invoke(sample, out, reencode=True)
    assert _motion_photo_result(result) == {"detected": True, "signals": signals, "video_kept": True}
    assert fake.from_file.called
    original, compressed = sample.read_bytes(), out.read_bytes()
    assert _samsung_video(compressed) == _samsung_video(original)
    # The zone, appended right after the image, comes out of the exiftool copy intact
    assert compressed.endswith(_zone_bytes(original))
    assert _zone(compressed).start == image_end(compressed)
    assert _metadata(result)["unexpected_losses"] == []


def test_samsung_motion_photo_from_stdin_keeps_its_video(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, _ = _invoke(None, out, input=SAMSUNG_MOTION_PHOTO.read_bytes(), reencode=True)
    assert _motion_photo_result(result)["video_kept"] is True
    assert _samsung_video(out.read_bytes()) == _samsung_video(SAMSUNG_MOTION_PHOTO.read_bytes())


def test_samsung_fields_of_the_zone_are_kept(tmp_path: Path):
    metadata = _metadata(_invoke(S23_MOTION_PHOTO, tmp_path / "out.jpg", reencode=True)[0])
    for tag in ("Samsung:TimeStamp", "Samsung:MCCData", "Samsung:EmbeddedVideoType", "Samsung:EmbeddedVideoFile"):
        assert metadata["after"][tag] == metadata["before"][tag], tag
    assert not any(tag.startswith("Samsung:") for tag in _expected_reasons(metadata))


def _google_video(path: Path) -> bytes:
    """The video the Google `Container:Directory` points to, extracted by exiftool."""
    exiftool = resolve_tool("exiftool")
    return subprocess.run([str(exiftool), "-b", "-Google:MotionPhotoVideo", str(path)], capture_output=True).stdout


def test_s23_motion_photo_keeps_a_coherent_google_directory(tmp_path: Path):
    out = tmp_path / "out.jpg"
    metadata = _metadata(_invoke(S23_MOTION_PHOTO, out, reencode=True)[0])
    after = metadata["after"]
    for tag in ("XMP-GCamera:MotionPhoto", "XMP-GContainer:DirectoryItemMime", "XMP-GContainer:DirectoryItemLength"):
        assert after[tag] == metadata["before"][tag], tag
    original_video = _google_video(S23_MOTION_PHOTO)
    assert len(original_video) == after["XMP-GContainer:DirectoryItemLength"]
    assert _google_video(out) == original_video
    # The primary's padding is the gap between its EOI and the video, which ends the file
    compressed = out.read_bytes()
    video_start = len(compressed) - after["XMP-GContainer:DirectoryItemLength"]
    assert after["XMP-GContainer:DirectoryItemPadding"][0] == video_start - image_end(compressed)


def test_kept_motion_photo_is_still_one_and_compresses_again(tmp_path: Path):
    out = tmp_path / "out.jpg"
    _invoke(S23_MOTION_PHOTO, out, reencode=True)
    inspected = runner.invoke(app, ["inspect", str(out), "--json"])
    assert inspected.exit_code == 0, inspected.output
    inspection = json.loads(inspected.stdout)["result"]
    assert inspection["motion_photo"] == {"detected": True, "signals": S23_SIGNALS}
    assert inspection["corrupt"] == {"detected": False, "reason": None}

    again, _ = _invoke(out, tmp_path / "again.jpg", reencode=True)
    assert _motion_photo_result(again) == {"detected": True, "signals": S23_SIGNALS, "video_kept": True}


def test_allow_motion_photo_loss_still_keeps_the_samsung_video(tmp_path: Path):
    """The flag allows a loss, it doesn't ask for one."""
    out = tmp_path / "out.jpg"
    result, _ = _invoke(S23_MOTION_PHOTO, out, "--allow-motion-photo-loss", reencode=True)
    assert _motion_photo_result(result) == {"detected": True, "signals": S23_SIGNALS, "video_kept": True}
    assert _samsung_video(out.read_bytes()) == _samsung_video(S23_MOTION_PHOTO.read_bytes())


def test_samsung_motion_photo_zone_after_other_images_is_refused(tmp_path: Path):
    """Appended after the main image alone, a zone that followed MPF images would move the video closer to the EOI."""
    mpo = io.BytesIO()
    Image.new("RGB", (64, 48)).save(mpo, format="MPO", save_all=True, append_images=[Image.new("RGB", (32, 24))])
    src = _write(tmp_path, "photo.jpg", mpo.getvalue() + _zone_bytes(SAMSUNG_MOTION_PHOTO.read_bytes()))
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["samsung_motion_photo_data"]


@pytest.mark.parametrize("sample", [S23_MOTION_PHOTO, SAMSUNG_MOTION_PHOTO])
def test_strip_refuses_a_samsung_motion_photo(tmp_path: Path, sample: Path):
    """`strip` is for privacy: the zone carries the operator's country, a UTC timestamp and the video."""
    out = tmp_path / "out.jpg"
    assert "samsung_motion_photo_data" in _assert_refused(*_invoke(sample, out, "--metadata", "strip"), out)


def test_strip_refusal_names_the_flag(tmp_path: Path):
    with _fake_tinify():
        result = runner.invoke(
            app,
            [
                "compress-image",
                str(S23_MOTION_PHOTO),
                "--output",
                str(tmp_path / "out.jpg"),
                "--api-key",
                "k",
                "--metadata",
                "strip",
            ],
        )
    assert result.exit_code == 20
    assert "--allow-motion-photo-loss" in result.stderr
    assert "samsung_motion_photo_data" in result.stderr


def test_strip_with_allowed_loss_appends_no_zone(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, _ = _invoke(S23_MOTION_PHOTO, out, "--metadata", "strip", "--allow-motion-photo-loss", reencode=True)
    assert _motion_photo_result(result) == {"detected": True, "signals": S23_SIGNALS, "video_kept": False}
    assert read_seft(out.read_bytes()) is None
    metadata = _metadata(result)
    assert metadata["unexpected_losses"] == []
    assert _expected_reasons(metadata)["Samsung:TimeStamp"] == STRIP_REASON


def test_strip_appends_no_zone_to_a_photo(tmp_path: Path):
    out = tmp_path / "out.jpg"
    metadata = _metadata(_invoke(S23_PHOTO, out, "--metadata", "strip", reencode=True)[0])
    assert read_seft(out.read_bytes()) is None
    assert _expected_reasons(metadata)["Samsung:MCCData"] == STRIP_REASON


# --- Compressed file with bytes after its image: no zone appended ----------------


def test_zone_is_not_appended_after_bytes_following_the_compressed_image(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, _ = _invoke(S23_PHOTO, out, reencode=True, tail=b"junk")
    metadata = _metadata(result)
    assert out.read_bytes().endswith(b"junk")
    unexpected = {loss["tag"]: loss["kind"] for loss in metadata["unexpected_losses"]}
    assert unexpected["Samsung:TimeStamp"] == "removed"
    assert unexpected["Samsung:MCCData"] == "removed"


def test_zone_is_not_appended_after_a_zone_of_the_compressed_file(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, _ = _invoke(SAMSUNG_MOTION_PHOTO, out, reencode=True, tail=_zone_bytes(S23_PHOTO.read_bytes()))
    assert _motion_photo_result(result)["video_kept"] is False
    assert out.read_bytes().endswith(_zone_bytes(S23_PHOTO.read_bytes()))


def test_motion_photo_whose_zone_cant_be_appended_reports_the_lost_video(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, _ = _invoke(S23_MOTION_PHOTO, out, reencode=True, tail=b"junk")
    assert _motion_photo_result(result) == {"detected": True, "signals": S23_SIGNALS, "video_kept": False}
    unexpected = {loss["tag"] for loss in _metadata(result)["unexpected_losses"]}
    assert {"Samsung:EmbeddedVideoFile", "Google:MotionPhotoVideo", "XMP-GCamera:MotionPhoto"} <= unexpected
    # The XMP no longer claims a video the file doesn't have
    assert "XMP-GCamera:MotionPhoto" not in _metadata(result)["after"]


# --- Known structures ----------------------------------------------------------


def test_s23_photo_with_seft_zone_without_motion_photo_is_accepted(tmp_path: Path):
    result, fake = _invoke(S23_PHOTO, tmp_path / "out.jpg")
    assert _motion_photo_result(result) == NO_MOTION_PHOTO
    assert fake.from_file.called


def test_s23_photo_zone_is_appended_and_its_fields_kept(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, _ = _invoke(S23_PHOTO, out, reencode=True)
    original, compressed = S23_PHOTO.read_bytes(), out.read_bytes()
    assert compressed.endswith(_zone_bytes(original))
    zone = _zone(compressed)
    assert zone.start == image_end(compressed)
    assert [(e.name, e.size) for e in zone.entries] == [(e.name, e.size) for e in _zone(original).entries]
    metadata = _metadata(result)
    assert metadata["after"]["Samsung:TimeStamp"] == "2024:06:15 12:00:00.918+02:00"
    assert metadata["after"]["Samsung:MCCData"] == "France (208)"
    assert metadata["unexpected_losses"] == []
    assert not any(tag.startswith("Samsung:") for tag in _expected_reasons(metadata))


def test_mpf_images_are_not_trailing_data(tmp_path: Path):
    """The secondary images an MPF segment declares, as in a Samsung JPEG, follow the main image's EOI."""
    src = tmp_path / "photo.jpg"
    first = Image.new("RGB", (64, 48), (53, 132, 228))
    second = Image.new("RGB", (32, 24), (200, 30, 30))
    first.save(src, format="MPO", save_all=True, append_images=[second])
    assert b"MPF\0" in src.read_bytes()
    assert _motion_photo_result(_invoke(src, tmp_path / "out.jpg")[0]) == NO_MOTION_PHOTO


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


def test_bytes_between_seft_entries_are_trailing_data(tmp_path: Path):
    """Bytes slipped between the last entry and the index are outside every entry the index describes."""
    data = S23_PHOTO.read_bytes()
    index_start = len(data) - 8 - struct.unpack("<I", data[-8:-4])[0]
    src = _write(tmp_path, "photo.jpg", data[:index_start] + b"unexplained" + data[index_start:])
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_bogus_mpf_entry_does_not_explain_trailing_bytes(tmp_path: Path):
    """An MP entry pointing at no JPEG, here the appended bytes themselves, is not an image."""
    mpo = io.BytesIO()
    Image.new("RGB", (64, 48)).save(mpo, format="MPO", save_all=True, append_images=[Image.new("RGB", (32, 24))])
    data = bytearray(mpo.getvalue())
    second = data.index(b"\xff\xd8\xff", 2)
    data[second : second + 3] = b"XYZ"
    src = _write(tmp_path, "photo.jpg", bytes(data))
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_motion_photo_data_in_an_unreadable_index_is_only_trailing_data(tmp_path: Path):
    data = bytearray(SAMSUNG_MOTION_PHOTO.read_bytes())
    data[-12:-8] = b"XXXX"  # last entry's size, past its distance back
    src = _write(tmp_path, "photo.jpg", bytes(data))
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_bytes_after_the_container_items_are_trailing_data(tmp_path: Path):
    """The last item ends the file: bytes appended after it leave the start of the video unexplained."""
    src = _write(tmp_path, "photo.jpg", GOOGLE_MOTION_PHOTO.read_bytes() + b"unexplained")
    out = tmp_path / "out.jpg"
    signals = _assert_refused(*_invoke(src, out), out)
    assert signals == ["xmp_google_motion_photo", "xmp_container_video", "trailing_data"]


def test_container_items_longer_than_the_trailing_bytes_explain_nothing(tmp_path: Path):
    xmp = GCAMERA_XMP.format(attributes="", directory=CONTAINER_DIRECTORY.format(mime="video/mp4")).replace(
        'Item:Length="0"',
        'Item:Length="100000"',
    )
    src = _write(tmp_path, "photo.jpg", _jpeg(xmp) + b"short video")
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["xmp_container_video", "trailing_data"]


def test_container_item_padding_after_the_primary_image_is_explained(tmp_path: Path):
    xmp = (
        GCAMERA_XMP.format(attributes="", directory=CONTAINER_DIRECTORY.format(mime="video/mp4"))
        .replace(
            'Item:Semantic="Primary"',
            'Item:Semantic="Primary" Item:Padding="4"',
        )
        .replace('Item:Length="0"', 'Item:Length="5"')
    )
    src = _write(tmp_path, "photo.jpg", _jpeg(xmp) + b"\0\0\0\0" + b"video")
    out = tmp_path / "out.jpg"
    assert _assert_refused(*_invoke(src, out), out) == ["xmp_container_video"]


# --- Other formats -------------------------------------------------------------


def test_png_bytes_after_iend_are_trailing_data(tmp_path: Path):
    src = _write(tmp_path, "capture.png", SCREENSHOT.read_bytes() + b"unexplained")
    out = tmp_path / "out.png"
    assert _assert_refused(*_invoke(src, out), out) == ["trailing_data"]


def test_png_without_trailing_bytes_is_accepted(tmp_path: Path):
    assert _motion_photo_result(_invoke(SCREENSHOT, tmp_path / "out.png")[0]) == NO_MOTION_PHOTO


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
    assert _motion_photo_result(_invoke(src, tmp_path / "out.webp")[0]) == NO_MOTION_PHOTO


# --- --allow-motion-photo-loss ---------------------------------------------------


def test_allow_motion_photo_loss_compresses_and_reports_the_signals(tmp_path: Path):
    out = tmp_path / "out.jpg"
    result, fake = _invoke(GOOGLE_MOTION_PHOTO, out, "--allow-motion-photo-loss")
    assert _motion_photo_result(result) == {
        "detected": True,
        "signals": ["xmp_google_motion_photo", "xmp_container_video"],
        "video_kept": False,
    }
    assert fake.from_file.called
    assert out.exists()


@pytest.mark.parametrize("sample", [S23_MOTION_PHOTO, GOOGLE_MOTION_PHOTO, SAMSUNG_MOTION_PHOTO])
def test_allowed_loss_strips_the_motion_photo_and_expects_its_losses(tmp_path: Path, sample: Path):
    """Bytes slipped after the image keep the zone, readable, from being appended: the video is lost."""
    src = _with_bytes_after_the_image(sample, tmp_path) if sample != GOOGLE_MOTION_PHOTO else sample
    out = tmp_path / "out.jpg"
    result, _ = _invoke(src, out, "--allow-motion-photo-loss", reencode=True)
    assert _motion_photo_result(result)["video_kept"] is False
    metadata = _metadata(result)
    assert metadata["unexpected_losses"] == []
    assert not any(tag.startswith(("XMP-GCamera:", "XMP-GContainer:")) for tag in metadata["after"])
    assert read_seft(out.read_bytes()) is None

    # The compressed file is no motion photo any more: it compresses without the flag
    again, _ = _invoke(out, tmp_path / "again.jpg")
    assert _motion_photo_result(again) == NO_MOTION_PHOTO


def test_allowed_loss_of_the_s23_motion_photo_gives_its_reason(tmp_path: Path):
    src = _with_bytes_after_the_image(S23_MOTION_PHOTO, tmp_path)
    result, _ = _invoke(src, tmp_path / "out.jpg", "--allow-motion-photo-loss", reencode=True)
    expected = _expected_reasons(_metadata(result))
    for tag in (
        "XMP-GCamera:MotionPhoto",
        "XMP-GCamera:MotionPhotoVersion",
        "XMP-GCamera:MotionPhotoPresentationTimestampUs",
        "XMP-GContainer:DirectoryItemMime",
        "XMP-GContainer:DirectoryItemLength",
        "Google:MotionPhotoVideo",
        "Samsung:EmbeddedVideoType",
        "Samsung:EmbeddedVideoFile",
        # The rest of the zone is not appended either
        "Samsung:TimeStamp",
        "Samsung:MCCData",
        "XMP-x:XMPToolkit",
    ):
        assert expected[tag] == MOTION_PHOTO_REASON, tag


def test_s23_motion_photo_turned_by_tinify_is_not_turned_twice(tmp_path: Path):
    """The real S23 portrait is stored 4000×3000 with `Rotate 90 CW`: Tinify returns its pixels turned, 3000×4000."""
    result, _ = _invoke(S23_MOTION_PHOTO, tmp_path / "out.jpg", reencode=True)
    metadata = _metadata(result)
    assert metadata["after"]["IFD0:Orientation"] == "Horizontal (normal)"
    assert (metadata["after"]["File:ImageWidth"], metadata["after"]["File:ImageHeight"]) == (3000, 4000)
    assert (metadata["after"]["ExifIFD:ExifImageWidth"], metadata["after"]["ExifIFD:ExifImageHeight"]) == (3000, 4000)
    dimensions = {field["key"]: field for field in metadata["important"]}["dimensions"]
    assert (dimensions["before"], dimensions["status"]) == ({"width": 3000, "height": 4000}, "same")
    assert metadata["unexpected_losses"] == []


def test_s23_motion_photo_converted_to_srgb_by_tinify_gets_no_p3_profile(tmp_path: Path):
    result, _ = _invoke(S23_MOTION_PHOTO, tmp_path / "out.jpg", reencode=True)
    metadata = _metadata(result)
    assert "ICC_Profile:ProfileDescription" not in metadata["after"]
    expected = {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}
    assert expected["ICC_Profile:ProfileDescription"] == "couleurs converties en sRGB par Tinify (gamut réduit)"


def test_xmp_other_than_the_motion_photo_is_kept(tmp_path: Path):
    xmp = GCAMERA_XMP.format(
        attributes='GCamera:MotionPhoto="1" xmlns:dc="http://purl.org/dc/elements/1.1/"',
        directory='<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Neutral</rdf:li></rdf:Alt></dc:title>',
    ).replace('xmlns:x="adobe:ns:meta/"', 'xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.1"')
    src = _write(tmp_path, "photo.jpg", _jpeg(xmp))
    result, _ = _invoke(src, tmp_path / "out.jpg", "--allow-motion-photo-loss", reencode=True)
    metadata = json.loads(result.stdout)["result"]["metadata"]
    assert metadata["after"]["XMP-dc:Title"] == "Neutral"
    # The toolkit is copied back with the rest: it is no loss, not even an expected one
    assert metadata["after"]["XMP-x:XMPToolkit"] == "Adobe XMP Core 5.1"
    assert "XMP-x:XMPToolkit" not in {loss["tag"] for loss in metadata["expected_losses"]}
    assert "XMP-GCamera:MotionPhoto" not in metadata["after"]
    assert metadata["unexpected_losses"] == []


def test_allow_motion_photo_loss_without_motion_photo_reports_none(tmp_path: Path):
    result, _ = _invoke(S23_PHOTO, tmp_path / "out.jpg", "--allow-motion-photo-loss")
    assert _motion_photo_result(result) == NO_MOTION_PHOTO
