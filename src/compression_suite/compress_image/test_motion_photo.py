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
from PIL import Image, ImageCms, ImageOps
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


MOTION_PHOTO_REASON = "photo animée : perte autorisée"


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


def _fake_tinify(reencode: bool = False):
    """Patch tinify so the compressed result is the input itself, or the input re-encoded like Tinify."""
    mock = MagicMock()
    mock.compression_count = 42

    def make_source(data: bytes):
        source = MagicMock()
        source.to_buffer.return_value = _reencode_like_tinify(data) if reencode else data
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
) -> tuple[Result, MagicMock]:
    """Run compress-image --json, returning the CLI result and the fake Tinify."""
    with _fake_tinify(reencode) as fake:
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
            app,
            ["compress-image", str(S23_MOTION_PHOTO), "--output", str(tmp_path / "out.jpg"), "--api-key", "k"],
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


@pytest.mark.parametrize("sample", [S23_MOTION_PHOTO, GOOGLE_MOTION_PHOTO, SAMSUNG_MOTION_PHOTO])
def test_allowed_loss_strips_the_motion_photo_and_expects_its_losses(tmp_path: Path, sample: Path):
    out = tmp_path / "out.jpg"
    result, _ = _invoke(sample, out, "--allow-motion-photo-loss", reencode=True)
    assert result.exit_code == 0, result.output
    metadata = json.loads(result.stdout)["result"]["metadata"]
    assert metadata["unexpected_losses"] == []
    assert not any(tag.startswith(("XMP-GCamera:", "XMP-GContainer:")) for tag in metadata["after"])

    # The compressed file is no motion photo any more: it compresses without the flag
    again, _ = _invoke(out, tmp_path / "again.jpg")
    assert _motion_photo_result(again) == {"detected": False, "signals": []}


def test_allowed_loss_of_the_s23_motion_photo_gives_its_reason(tmp_path: Path):
    result, _ = _invoke(S23_MOTION_PHOTO, tmp_path / "out.jpg", "--allow-motion-photo-loss", reencode=True)
    metadata = json.loads(result.stdout)["result"]["metadata"]
    expected = {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}
    for tag in (
        "XMP-GCamera:MotionPhoto",
        "XMP-GCamera:MotionPhotoVersion",
        "XMP-GCamera:MotionPhotoPresentationTimestampUs",
        "XMP-GContainer:DirectoryItemMime",
        "XMP-GContainer:DirectoryItemLength",
        "Google:MotionPhotoVideo",
        "Samsung:EmbeddedVideoType",
        "Samsung:EmbeddedVideoFile",
        "XMP-x:XMPToolkit",
    ):
        assert expected[tag] == MOTION_PHOTO_REASON, tag


def test_s23_motion_photo_turned_by_tinify_is_not_turned_twice(tmp_path: Path):
    """The real S23 portrait is stored 4000×3000 with `Rotate 90 CW`: Tinify returns its pixels turned, 3000×4000."""
    result, _ = _invoke(S23_MOTION_PHOTO, tmp_path / "out.jpg", "--allow-motion-photo-loss", reencode=True)
    metadata = json.loads(result.stdout)["result"]["metadata"]
    assert metadata["after"]["IFD0:Orientation"] == "Horizontal (normal)"
    assert (metadata["after"]["File:ImageWidth"], metadata["after"]["File:ImageHeight"]) == (3000, 4000)
    assert (metadata["after"]["ExifIFD:ExifImageWidth"], metadata["after"]["ExifIFD:ExifImageHeight"]) == (3000, 4000)
    dimensions = {field["key"]: field for field in metadata["important"]}["dimensions"]
    assert (dimensions["before"], dimensions["status"]) == ({"width": 3000, "height": 4000}, "same")
    assert metadata["unexpected_losses"] == []


def test_s23_motion_photo_converted_to_srgb_by_tinify_gets_no_p3_profile(tmp_path: Path):
    result, _ = _invoke(S23_MOTION_PHOTO, tmp_path / "out.jpg", "--allow-motion-photo-loss", reencode=True)
    metadata = json.loads(result.stdout)["result"]["metadata"]
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
    assert _motion_photo_result(result) == {"detected": False, "signals": []}
