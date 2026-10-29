"""CLI tests for the metadata diff of compress-image --json — Tinify is faked, exiftool runs for real.

The fake Tinify behaves like the real one on the samples: it re-encodes the pixels and keeps no metadata
at all (no EXIF, no ICC profile, no PNG text block, no Samsung trailer).
"""

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()

SAMPLES = Path(__file__).resolve().parent / "samples"
S23_PHOTO = SAMPLES / "s23_p3_seft.jpg"
SCREENSHOT = SAMPLES / "screenshot.png"

SAMSUNG_TRAILER_REASON = "zone Samsung de fin de fichier : retirée par Tinify, exiftool ne sait pas la réécrire"
STRIP_REASON = "métadonnées retirées à la demande"
REENCODING_REASON = "réencodage"

# 2022-08-09T10:11:12.654321Z, in nanoseconds
SCREENSHOT_MTIME_NS = 1_660_039_872_654_321_000


def _reencode_like_tinify(data: bytes, resize: float = 1.0, drop_alpha: bool = False) -> bytes:
    """Re-encode the pixels without any metadata, as Tinify does (progressive JPEG, palette PNG, same format else).

    `drop_alpha` flattens the image first, as a faulty compression would.
    """
    image = Image.open(io.BytesIO(data))
    fmt = image.format
    image.info.clear()  # Pillow would otherwise write the ICC profile and EXIF back
    if resize != 1.0:
        image = image.resize((int(image.width * resize), int(image.height * resize)))
    fully_opaque = image.mode == "RGBA" and image.getchannel("A").getextrema() == (255, 255)
    if drop_alpha or fully_opaque:  # Tinify drops an alpha channel that hides nothing
        image = image.convert("RGB")
    buf = io.BytesIO()
    if fmt == "JPEG":
        image.save(buf, format="JPEG", quality=80, progressive=True)
    elif fmt == "PNG":
        image.quantize(16).save(buf, format="PNG")
    else:
        image.save(buf, format=fmt)
    return buf.getvalue()


def _fake_tinify(resize: float = 1.0, drop_alpha: bool = False, output: bytes | None = None):
    """Patch tinify so the compressed result is the input re-encoded without metadata, or `output`."""
    mock = MagicMock()
    mock.compression_count = 42

    def make_source(data: bytes):
        source = MagicMock()
        reencoded = _reencode_like_tinify(data, resize, drop_alpha) if output is None else output
        source.to_buffer.return_value = reencoded
        return source

    mock.from_file.side_effect = lambda path: make_source(Path(path).read_bytes())
    mock.from_buffer.side_effect = make_source
    return patch("compression_suite.compress_image.main.tinify", mock)


def _compress(
    src: Path,
    out: Path,
    *args: str,
    resize: float = 1.0,
    drop_alpha: bool = False,
    output: bytes | None = None,
) -> dict:
    """Run compress-image --json and return the metadata diff."""
    with _fake_tinify(resize, drop_alpha, output):
        result = runner.invoke(
            app,
            ["compress-image", str(src), "--output", str(out), "--api-key", "test-key", "--json", *args],
        )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["result"]["metadata"]


def _important(metadata: dict) -> dict[str, dict]:
    return {field["key"]: field for field in metadata["important"]}


def _expected(metadata: dict) -> dict[str, dict]:
    return {loss["tag"]: loss for loss in metadata["expected_losses"]}


def _screenshot(tmp_path: Path) -> Path:
    """Copy the PNG capture, dated like an old screenshot."""
    src = tmp_path / "screenshot.png"
    src.write_bytes(SCREENSHOT.read_bytes())
    os.utime(src, ns=(SCREENSHOT_MTIME_NS, SCREENSHOT_MTIME_NS))
    return src


def _transparent_image(tmp_path: Path, fmt: str = "PNG") -> Path:
    """Write an image whose left half is fully transparent."""
    image = Image.new("RGBA", (40, 30), (200, 30, 30, 255))
    image.paste((0, 0, 0, 0), (0, 0, 20, 30))
    src = tmp_path / f"logo.{fmt.lower()}"
    image.save(src, format=fmt)
    return src


# --- S23 photo ----------------------------------------------------------------


def test_s23_photo_keeps_its_important_fields(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg")

    assert [field["key"] for field in metadata["important"]] == [
        "capture_date",
        "gps",
        "camera",
        "dimensions",
        "orientation",
        "color_profile",
        "transparency",
    ]
    gps = {
        "latitude": "12 deg 20' 44.16\"",
        "latitude_ref": "North",
        "longitude": "65 deg 25' 55.56\"",
        "longitude_ref": "East",
        "altitude": "42 m",
        "altitude_ref": "Above Sea Level",
    }
    assert _important(metadata) == {
        "capture_date": {
            "key": "capture_date",
            "before": "2024:06:15 12:00:00",
            "after": "2024:06:15 12:00:00",
            "source": "ExifIFD:DateTimeOriginal",
            "status": "same",
        },
        "gps": {"key": "gps", "before": gps, "after": gps, "source": "GPS:GPSLatitude", "status": "same"},
        "camera": {
            "key": "camera",
            "before": {"make": "samsung", "model": "Galaxy S23"},
            "after": {"make": "samsung", "model": "Galaxy S23"},
            "source": "IFD0:Make",
            "status": "same",
        },
        "dimensions": {
            "key": "dimensions",
            "before": {"width": 4000, "height": 3000},
            "after": {"width": 4000, "height": 3000},
            "source": "File:ImageWidth",
            "status": "same",
        },
        "orientation": {
            "key": "orientation",
            "before": "Horizontal (normal)",
            "after": "Horizontal (normal)",
            "source": "IFD0:Orientation",
            "status": "same",
        },
        "color_profile": {
            "key": "color_profile",
            "before": "DCI-P3 D65 Gamut with sRGB Transfer",
            "after": "DCI-P3 D65 Gamut with sRGB Transfer",
            "source": "ICC_Profile:ProfileDescription",
            "status": "same",
        },
        "transparency": {"key": "transparency", "before": False, "after": False, "source": "pixels", "status": "same"},
    }


def test_s23_photo_has_no_unexpected_loss(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg")
    assert metadata["unexpected_losses"] == []
    assert metadata["after"]["ICC_Profile:ProfileDescription"] == "DCI-P3 D65 Gamut with sRGB Transfer"
    assert metadata["after"]["IFD0:ImageWidth"] == 4000


def test_s23_samsung_trailer_fields_are_expected_losses(tmp_path: Path):
    expected = _expected(_compress(S23_PHOTO, tmp_path / "out.jpg"))
    assert expected["Samsung:TimeStamp"] == {
        "tag": "Samsung:TimeStamp",
        "before": "2024:06:15 12:00:00.918+02:00",
        "reason": SAMSUNG_TRAILER_REASON,
    }
    assert expected["Samsung:MCCData"] == {
        "tag": "Samsung:MCCData",
        "before": "France (208)",
        "reason": SAMSUNG_TRAILER_REASON,
    }


def test_exif_thumbnail_is_an_expected_loss(tmp_path: Path):
    expected = _expected(_compress(S23_PHOTO, tmp_path / "out.jpg"))
    thumbnail = expected["IFD1:ThumbnailImage"]
    assert thumbnail["before"] == "(Binary data 46956 bytes, use -b option to extract)"
    assert "miniature" in thumbnail["reason"]


def test_file_group_encoding_changes_are_reencoding_losses(tmp_path: Path):
    expected = _expected(_compress(S23_PHOTO, tmp_path / "out.jpg"))
    assert expected["File:EncodingProcess"] == {
        "tag": "File:EncodingProcess",
        "before": "Baseline DCT, Huffman coding",
        "reason": REENCODING_REASON,
    }


def test_tags_appearing_after_compression_are_listed_as_added(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg")
    assert {"tag": "JFIF:JFIFVersion", "after": 1.01} in metadata["added"]


def test_before_and_after_skip_file_system_and_computed_groups(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg")
    for side in ("before", "after"):
        groups = {tag.split(":", 1)[0] for tag in metadata[side]}
        assert groups.isdisjoint({"System", "Composite", "ExifTool"}), side
        assert "SourceFile" not in metadata[side]
    assert metadata["before"]["IFD0:Model"] == "Galaxy S23"
    assert metadata["before"]["File:ImageWidth"] == 4000


# --- Photo with XMP -----------------------------------------------------------

TOOLKIT_XMP = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.1">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:title><rdf:Alt><rdf:li xml:lang="x-default">Neutral</rdf:li></rdf:Alt></dc:title>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>"""


def test_xmp_toolkit_is_kept(tmp_path: Path):
    """exiftool names itself as the toolkit of the XMP it writes, unless the original's is copied back."""
    src = tmp_path / "photo.jpg"
    Image.new("RGB", (64, 48), (53, 132, 228)).save(src, format="JPEG", xmp=TOOLKIT_XMP.encode())
    metadata = _compress(src, tmp_path / "out.jpg")
    assert metadata["before"]["XMP-x:XMPToolkit"] == "Adobe XMP Core 5.1"
    assert metadata["after"]["XMP-x:XMPToolkit"] == "Adobe XMP Core 5.1"
    assert metadata["after"]["XMP-dc:Title"] == "Neutral"
    assert metadata["unexpected_losses"] == []


# --- PNG capture --------------------------------------------------------------


def test_png_capture_date_falls_back_to_the_file_mtime(tmp_path: Path):
    metadata = _compress(_screenshot(tmp_path), tmp_path / "out.png")
    assert _important(metadata)["capture_date"] == {
        "key": "capture_date",
        "before": "2022-08-09T10:11:12.654321+00:00",
        "after": "2022-08-09T10:11:12.654321+00:00",
        "source": "file_mtime",
        "status": "same",
    }


def test_png_text_blocks_are_kept(tmp_path: Path):
    metadata = _compress(_screenshot(tmp_path), tmp_path / "out.png")
    assert metadata["unexpected_losses"] == []
    assert metadata["after"]["PNG:Software"] == "gnome-screenshot"
    assert metadata["after"]["PNG:Comment"] == "Fake screen capture for the compression-suite tests"


def test_png_without_photo_fields_reports_them_absent(tmp_path: Path):
    important = _important(_compress(_screenshot(tmp_path), tmp_path / "out.png"))
    for key in ("gps", "camera", "orientation", "color_profile"):
        assert important[key] == {"key": key, "before": None, "after": None, "source": None, "status": "absent"}
    assert important["dimensions"]["before"] == {"width": 320, "height": 200}
    assert important["dimensions"]["source"] == "PNG:ImageWidth"


def test_stdin_has_no_mtime_to_fall_back_to(tmp_path: Path):
    with _fake_tinify():
        result = runner.invoke(
            app,
            ["compress-image", "--output", str(tmp_path / "out.png"), "--api-key", "test-key", "--json"],
            input=SCREENSHOT.read_bytes(),
        )
    assert result.exit_code == 0, result.output
    capture_date = _important(json.loads(result.stdout)["result"]["metadata"])["capture_date"]
    assert capture_date == {"key": "capture_date", "before": None, "after": None, "source": None, "status": "absent"}


# --- Changed values -----------------------------------------------------------


def test_changed_dimensions_are_an_unexpected_change(tmp_path: Path):
    metadata = _compress(_screenshot(tmp_path), tmp_path / "out.png", resize=0.5)
    assert {"tag": "PNG:ImageWidth", "kind": "changed", "before": 320, "after": 160} in metadata["unexpected_losses"]
    dimensions = _important(metadata)["dimensions"]
    assert dimensions["status"] == "changed"
    assert dimensions["after"] == {"width": 160, "height": 100}


def test_icc_profile_exiftool_cannot_write_back_is_an_unexpected_removal(tmp_path: Path):
    """exiftool can't write an ICC profile into an AVIF: the real Tinify loses it, and so does the fake."""
    src = tmp_path / "photo.avif"
    icc_profile = Image.open(S23_PHOTO).info["icc_profile"]
    Image.open(SCREENSHOT).convert("RGB").save(src, format="AVIF", icc_profile=icc_profile)
    metadata = _compress(src, tmp_path / "out.avif")
    assert {
        "tag": "ICC_Profile:ProfileDescription",
        "kind": "removed",
        "before": "DCI-P3 D65 Gamut with sRGB Transfer",
        "after": None,
    } in metadata["unexpected_losses"]
    assert _important(metadata)["color_profile"]["status"] == "lost"


# --- Transparency -------------------------------------------------------------


def test_opaque_alpha_channel_dropped_from_a_gnome_capture_is_reencoding(tmp_path: Path):
    """gnome-screenshot writes RGBA captures without any transparent pixel: Tinify drops their alpha channel."""
    metadata = _compress(_screenshot(tmp_path), tmp_path / "out.png")
    assert metadata["unexpected_losses"] == []
    assert _expected(metadata)["PNG:ColorType"] == {
        "tag": "PNG:ColorType",
        "before": "RGB with Alpha",
        "reason": REENCODING_REASON,
    }
    assert _important(metadata)["transparency"] == {
        "key": "transparency",
        "before": False,
        "after": False,
        "source": "pixels",
        "status": "same",
    }


def test_transparency_kept_in_a_palette_is_reencoding(tmp_path: Path):
    metadata = _compress(_transparent_image(tmp_path), tmp_path / "out.png")
    assert metadata["unexpected_losses"] == []
    assert _expected(metadata)["PNG:ColorType"]["reason"] == REENCODING_REASON
    assert _important(metadata)["transparency"] == {
        "key": "transparency",
        "before": True,
        "after": True,
        "source": "pixels",
        "status": "same",
    }


@pytest.mark.parametrize("mode", ["keep", "strip"])
@pytest.mark.parametrize(
    ("fmt", "alpha_loss"),
    [
        ("PNG", {"tag": "PNG:ColorType", "kind": "changed", "before": "RGB with Alpha", "after": "Palette"}),
        ("WEBP", {"tag": "RIFF:WebP_Flags", "kind": "removed", "before": "Alpha", "after": None}),
        (
            "AVIF",
            {
                "tag": "QuickTime:AuxiliaryImageType",
                "kind": "removed",
                "before": "urn:mpeg:mpegB:cicp:systems:auxiliary:alpha",
                "after": None,
            },
        ),
    ],
)
def test_lost_transparency_is_never_expected(tmp_path: Path, mode: str, fmt: str, alpha_loss: dict):
    src = _transparent_image(tmp_path, fmt)
    metadata = _compress(src, tmp_path / f"out.{fmt.lower()}", "--metadata", mode, drop_alpha=True)
    assert _important(metadata)["transparency"] == {
        "key": "transparency",
        "before": True,
        "after": False,
        "source": "pixels",
        "status": "lost",
    }
    assert alpha_loss in metadata["unexpected_losses"]
    assert {"tag": "pixels:transparency", "kind": "removed", "before": True, "after": False} in metadata[
        "unexpected_losses"
    ]
    assert alpha_loss["tag"] not in _expected(metadata)


def test_transparency_is_unverified_when_the_output_cannot_be_decoded(tmp_path: Path):
    corrupt_png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    metadata = _compress(_transparent_image(tmp_path), tmp_path / "out.png", "--metadata", "strip", output=corrupt_png)
    assert _important(metadata)["transparency"] == {
        "key": "transparency",
        "before": True,
        "after": None,
        "source": "pixels",
        "status": "unverified",
    }
    assert "PNG:ColorType" not in _expected(metadata)
    assert "PNG:ColorType" in {loss["tag"] for loss in metadata["unexpected_losses"]}
    assert {
        "tag": "pixels:transparency",
        "kind": "unverified",
        "before": True,
        "after": None,
    } in metadata["unexpected_losses"]


def test_transparency_made_opaque_without_alpha_tag_change_is_unexpected(tmp_path: Path):
    """The output keeps its RGBA colour type but every pixel is opaque: no alpha tag changes, only the pixels tell."""
    src = _transparent_image(tmp_path)
    opaque = Image.open(src)
    opaque.putalpha(255)
    buf = io.BytesIO()
    opaque.save(buf, format="PNG")
    metadata = _compress(src, tmp_path / "out.png", output=buf.getvalue())
    assert metadata["after"]["PNG:ColorType"] == metadata["before"]["PNG:ColorType"] == "RGB with Alpha"
    assert _important(metadata)["transparency"]["status"] == "lost"
    assert metadata["unexpected_losses"] == [
        {"tag": "pixels:transparency", "kind": "removed", "before": True, "after": False},
    ]


def test_unverified_transparency_of_an_opaque_original_is_no_loss(tmp_path: Path):
    corrupt_png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    metadata = _compress(_screenshot(tmp_path), tmp_path / "out.png", "--metadata", "strip", output=corrupt_png)
    assert _important(metadata)["transparency"]["status"] == "unverified"
    assert "pixels:transparency" not in {loss["tag"] for loss in metadata["unexpected_losses"]}


# --- --metadata strip ---------------------------------------------------------


def test_strip_lists_every_loss_as_expected(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg", "--metadata", "strip")
    assert metadata["unexpected_losses"] == []
    expected = _expected(metadata)
    assert {loss["reason"] for loss in metadata["expected_losses"]} == {STRIP_REASON}
    assert expected["ICC_Profile:ProfileDescription"]["before"] == "DCI-P3 D65 Gamut with sRGB Transfer"
    assert "IFD0:Make" in expected
    assert "GPS:GPSLatitude" in expected
    assert _important(metadata)["color_profile"]["status"] == "lost"


def test_strip_still_reports_changed_dimensions_as_unexpected(tmp_path: Path):
    metadata = _compress(_screenshot(tmp_path), tmp_path / "out.png", "--metadata", "strip", resize=0.5)
    assert {"tag": "PNG:ImageWidth", "kind": "changed", "before": 320, "after": 160} in metadata["unexpected_losses"]
    assert {loss["reason"] for loss in metadata["expected_losses"]} == {STRIP_REASON}
