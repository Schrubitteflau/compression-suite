"""CLI tests for the metadata diff of compress-image --json — Tinify is faked, exiftool runs for real.

Only cases the command can't reach call diff_metadata directly.

The fake Tinify behaves like the real one on the samples: it applies the orientation, converts the colours to sRGB
with the image's ICC profile, re-encodes the pixels and keeps no metadata at all (no EXIF, no ICC profile, no PNG text
block, no Samsung trailer).
"""

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageOps, ImageStat
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.utils.metadata_diff import diff_metadata

runner = CliRunner()

SAMPLES = Path(__file__).resolve().parent / "samples"
S23_PHOTO = SAMPLES / "s23_p3_seft.jpg"
SCREENSHOT = SAMPLES / "screenshot.png"

STRIP_REASON = "métadonnées retirées à la demande"
COLORS_CONVERTED_REASON = "couleurs converties en sRGB par Tinify (gamut réduit)"
P3_PROFILE = "DCI-P3 D65 Gamut with sRGB Transfer"
REENCODING_REASON = "réencodage"

# 2022-08-09T10:11:12.654321Z, in nanoseconds
SCREENSHOT_MTIME_NS = 1_660_039_872_654_321_000


def _reencode_like_tinify(
    data: bytes,
    resize: float = 1.0,
    drop_alpha: bool = False,
    apply_orientation: bool = True,
    transpose: Image.Transpose | None = None,
    convert_colors: bool = True,
    keep_icc_profile: bool = False,
) -> bytes:
    """Re-encode the pixels without any metadata, as Tinify does (progressive JPEG, palette PNG, same format else).

    Tinify applies the EXIF orientation to the pixels (seen on real calls, mirror included); `apply_orientation=False`
    keeps them as stored. `transpose` is applied to the stored pixels instead, and `drop_alpha` flattens the image
    first, as a faulty compression would. Tinify converts the colours to sRGB with the ICC profile (seen on real calls,
    for a P3 JPEG, PNG, WebP and AVIF); `convert_colors=False` keeps them as stored. `keep_icc_profile` writes the
    original's profile back onto the pixels, converted or not.
    """
    image = Image.open(io.BytesIO(data))
    fmt = image.format
    icc_profile = image.info.get("icc_profile")
    if transpose is not None:
        image = image.transpose(transpose)
    elif apply_orientation:
        image = ImageOps.exif_transpose(image)
    if convert_colors and icc_profile:
        source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
        converted = ImageCms.profileToProfile(
            image, source_profile, ImageCms.createProfile("sRGB"), outputMode=image.mode
        )
        assert converted is not None  # only None with inPlace=True
        image = converted
    image.info.clear()  # Pillow would otherwise write the ICC profile and EXIF back
    if resize != 1.0:
        image = image.resize((int(image.width * resize), int(image.height * resize)))
    fully_opaque = image.mode == "RGBA" and image.getchannel("A").getextrema() == (255, 255)
    if drop_alpha or fully_opaque:  # Tinify drops an alpha channel that hides nothing
        image = image.convert("RGB")
    buf = io.BytesIO()
    options = {"icc_profile": icc_profile} if keep_icc_profile and icc_profile else {}
    if fmt == "JPEG":
        image.save(buf, format="JPEG", quality=80, progressive=True, **options)
    elif fmt == "PNG":
        image.quantize(16).save(buf, format="PNG", **options)
    else:
        image.save(buf, format=fmt, **options)
    return buf.getvalue()


def _fake_tinify(
    resize: float = 1.0,
    drop_alpha: bool = False,
    output: bytes | None = None,
    apply_orientation: bool = True,
    transpose: Image.Transpose | None = None,
    convert_colors: bool = True,
    keep_icc_profile: bool = False,
):
    """Patch tinify so the compressed result is the input re-encoded without metadata, or `output`."""
    mock = MagicMock()
    mock.compression_count = 42

    def make_source(data: bytes):
        source = MagicMock()
        if output is None:
            source.to_buffer.return_value = _reencode_like_tinify(
                data, resize, drop_alpha, apply_orientation, transpose, convert_colors, keep_icc_profile
            )
        else:
            source.to_buffer.return_value = output
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
    apply_orientation: bool = True,
    transpose: Image.Transpose | None = None,
    convert_colors: bool = True,
    keep_icc_profile: bool = False,
) -> dict:
    """Run compress-image --json and return the metadata diff."""
    with _fake_tinify(resize, drop_alpha, output, apply_orientation, transpose, convert_colors, keep_icc_profile):
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
            "before": P3_PROFILE,
            "after": None,
            "source": "ICC_Profile:ProfileDescription",
            "status": "lost",
        },
        "transparency": {"key": "transparency", "before": False, "after": False, "source": "pixels", "status": "same"},
    }


def test_s23_photo_has_no_unexpected_loss(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg")
    assert metadata["unexpected_losses"] == []
    assert metadata["after"]["IFD0:ImageWidth"] == 4000


def test_s23_samsung_trailer_fields_are_kept(tmp_path: Path):
    """Tinify drops the `SEFT` zone: it is appended back after the compressed image."""
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg")
    assert metadata["after"]["Samsung:TimeStamp"] == "2024:06:15 12:00:00.918+02:00"
    assert metadata["after"]["Samsung:MCCData"] == "France (208)"
    assert not any(tag.startswith("Samsung:") for tag in _expected(metadata))


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


# --- Colours -------------------------------------------------------------------


def test_s23_photo_converted_to_srgb_by_tinify_gets_no_p3_profile(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg")
    assert not any(tag.startswith("ICC") for tag in metadata["after"])
    assert _expected(metadata)["ICC_Profile:ProfileDescription"] == {
        "tag": "ICC_Profile:ProfileDescription",
        "before": P3_PROFILE,
        "reason": COLORS_CONVERTED_REASON,
    }
    assert {
        loss["reason"] for loss in metadata["expected_losses"] if loss["tag"].startswith(("ICC_Profile:", "ICC-"))
    } == {COLORS_CONVERTED_REASON}


def test_s23_photo_colours_not_converted_by_tinify_keep_the_p3_profile(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg", convert_colors=False)
    assert metadata["unexpected_losses"] == []
    assert _important(metadata)["color_profile"]["status"] == "same"
    assert "ICC_Profile:ProfileDescription" not in _expected(metadata)


def test_p3_profile_on_pixels_converted_to_srgb_is_an_unexpected_colour_change(tmp_path: Path):
    """The bug of #6: a P3 profile on sRGB pixels, displayed oversaturated. Here Tinify itself would keep it."""
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg", keep_icc_profile=True)
    assert {"tag": "pixels:colors", "kind": "changed", "before": P3_PROFILE, "after": P3_PROFILE} in metadata[
        "unexpected_losses"
    ]
    assert _important(metadata)["color_profile"]["status"] == "same"  # the tags alone can't tell


def test_p3_profile_copied_back_onto_converted_pixels_is_an_unexpected_colour_change():
    """The bug of #6 as the diff sees it, which the command can no longer reach: same tags, colours changed."""
    tags = {"ICC_Profile:ProfileDescription": P3_PROFILE}
    metadata = diff_metadata(tags, tags, [], transparency=(False, False), same_colors=False)
    assert metadata["unexpected_losses"] == [
        {"tag": "pixels:colors", "kind": "changed", "before": P3_PROFILE, "after": P3_PROFILE}
    ]


def test_strip_of_a_photo_converted_to_srgb_has_no_unexpected_loss(tmp_path: Path):
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg", "--metadata", "strip")
    assert metadata["unexpected_losses"] == []


def test_strip_of_a_photo_tinify_did_not_convert_changes_its_colours(tmp_path: Path):
    """Without its profile, a viewer reads the P3 pixels as sRGB: the colours are duller."""
    metadata = _compress(S23_PHOTO, tmp_path / "out.jpg", "--metadata", "strip", convert_colors=False)
    assert {"tag": "pixels:colors", "kind": "changed", "before": P3_PROFILE, "after": None} in metadata[
        "unexpected_losses"
    ]


def test_srgb_profile_which_converts_nothing_changes_no_colour(tmp_path: Path):
    """Pixels converted from sRGB to sRGB can't be told from the original's: kept or dropped, its profile changes
    nothing."""
    src = tmp_path / "photo.jpg"
    srgb_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    with Image.open(S23_PHOTO) as photo:
        photo.resize((400, 300)).save(src, format="JPEG", quality=95, icc_profile=srgb_profile)
    metadata = _compress(src, tmp_path / "out.jpg")
    assert metadata["unexpected_losses"] == []


def _dull_p3_photo(tmp_path: Path) -> Path:
    """The S23 photo with dull colours, which its P3 profile barely changes, with that profile."""
    src = tmp_path / "dull.jpg"
    with Image.open(S23_PHOTO) as photo:
        dull = Image.blend(photo.convert("L").convert("RGB"), photo.convert("RGB"), 0.3).resize((400, 300))
        dull.save(src, format="JPEG", quality=95, icc_profile=photo.info["icc_profile"])
    return src


def test_dull_p3_photo_converted_to_srgb_gets_no_p3_profile(tmp_path: Path):
    metadata = _compress(_dull_p3_photo(tmp_path), tmp_path / "out.jpg")
    assert "ICC_Profile:ProfileDescription" not in metadata["after"]
    assert _expected(metadata)["ICC_Profile:ProfileDescription"]["reason"] == COLORS_CONVERTED_REASON


def _p3_avif(tmp_path: Path) -> Path:
    """A small AVIF of the S23 photo, with its P3 profile."""
    src = tmp_path / "photo.avif"
    with Image.open(S23_PHOTO) as photo:
        photo.resize((400, 300)).save(src, format="AVIF", icc_profile=photo.info["icc_profile"], quality=90)
    return src


def test_avif_converted_to_srgb_loses_its_profile_as_expected(tmp_path: Path):
    """exiftool can't write an ICC profile into an AVIF: once Tinify converted the colours, there is none to write."""
    metadata = _compress(_p3_avif(tmp_path), tmp_path / "out.avif")
    assert metadata["unexpected_losses"] == []
    expected = _expected(metadata)
    assert expected["ICC_Profile:ProfileDescription"]["reason"] == COLORS_CONVERTED_REASON
    assert expected["QuickTime:ColorPrimaries"] == {
        "tag": "QuickTime:ColorPrimaries",
        "before": "Unspecified",
        "reason": COLORS_CONVERTED_REASON,
    }


def test_avif_profile_exiftool_cannot_write_back_is_an_unexpected_loss(tmp_path: Path):
    """Colours left in P3 by Tinify need the profile, which exiftool can't write into an AVIF."""
    metadata = _compress(_p3_avif(tmp_path), tmp_path / "out.avif", convert_colors=False)
    unexpected = {loss["tag"]: loss for loss in metadata["unexpected_losses"]}
    assert unexpected["ICC_Profile:ProfileDescription"]["kind"] == "removed"
    assert unexpected["pixels:colors"] == {
        "tag": "pixels:colors",
        "kind": "changed",
        "before": P3_PROFILE,
        "after": None,
    }
    assert _important(metadata)["color_profile"]["status"] == "lost"


# --- Changed values -----------------------------------------------------------


def test_changed_dimensions_are_an_unexpected_change(tmp_path: Path):
    metadata = _compress(_screenshot(tmp_path), tmp_path / "out.png", resize=0.5)
    assert {"tag": "PNG:ImageWidth", "kind": "changed", "before": 320, "after": 160} in metadata["unexpected_losses"]
    dimensions = _important(metadata)["dimensions"]
    assert dimensions["status"] == "changed"
    assert dimensions["after"] == {"width": 160, "height": 100}


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


# --- Orientation --------------------------------------------------------------

ORIENTATION_REASON = "orientation appliquée aux pixels"
ROTATED_DIMENSIONS_REASON = "pixels pivotés selon l'orientation : l'image affichée garde ses dimensions"
ROTATE_90_CW = 6
MIRROR_HORIZONTAL = 2
ORIENTATIONS = [(ROTATE_90_CW, "Rotate 90 CW"), (MIRROR_HORIZONTAL, "Mirror horizontal")]

ORIENTATION_XMP = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about="" xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Orientation="{orientation}"/>
  </rdf:RDF>
</x:xmpmeta>"""


def _oriented_jpeg(tmp_path: Path, orientation: int, xmp: bool = False) -> Path:
    """Write an 80×60 JPEG whose shapes tell every rotation and mirror apart, with its orientation and EXIF size.

    `xmp` also writes the orientation in the XMP (`XMP-tiff:Orientation`).
    """
    image = Image.new("RGB", (80, 60), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 30, 24), fill=(200, 30, 30))
    draw.ellipse((52, 36, 76, 56), fill=(30, 60, 200))
    draw.polygon([(40, 8), (76, 8), (76, 28)], fill=(30, 160, 60))
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif.get_ifd(0x8769).update({0xA002: 80, 0xA003: 60})  # ExifImageWidth, ExifImageHeight
    src = tmp_path / "photo.jpg"
    options = {"xmp": ORIENTATION_XMP.format(orientation=orientation).encode()} if xmp else {}
    image.save(src, format="JPEG", quality=95, exif=exif.tobytes(), **options)
    return src


def _displayed_difference(a: Path, b: Path) -> float:
    """The mean difference, out of 255, between two images as a viewer displays them (orientation applied)."""
    first, second = (ImageOps.exif_transpose(Image.open(path)).convert("RGB") for path in (a, b))
    assert first.size == second.size
    return sum(ImageStat.Stat(ImageChops.difference(first, second)).mean) / 3


@pytest.mark.parametrize(("orientation", "name"), ORIENTATIONS)
def test_orientation_applied_by_tinify_is_reset(tmp_path: Path, orientation: int, name: str):
    src = _oriented_jpeg(tmp_path, orientation)
    out = tmp_path / "out.jpg"
    metadata = _compress(src, out)
    assert metadata["unexpected_losses"] == []
    assert metadata["after"]["IFD0:Orientation"] == "Horizontal (normal)"
    assert _expected(metadata)["IFD0:Orientation"] == {
        "tag": "IFD0:Orientation",
        "before": name,
        "reason": ORIENTATION_REASON,
    }
    assert _displayed_difference(src, out) < 5


@pytest.mark.parametrize(("orientation", "name"), ORIENTATIONS)
def test_orientation_not_applied_by_tinify_is_copied_back(tmp_path: Path, orientation: int, name: str):
    src = _oriented_jpeg(tmp_path, orientation)
    out = tmp_path / "out.jpg"
    metadata = _compress(src, out, apply_orientation=False)
    assert metadata["unexpected_losses"] == []
    assert metadata["after"]["IFD0:Orientation"] == name
    assert "IFD0:Orientation" not in _expected(metadata)
    assert _displayed_difference(src, out) < 5


def test_dimensions_of_a_rotated_portrait_are_compared_as_displayed(tmp_path: Path):
    metadata = _compress(_oriented_jpeg(tmp_path, ROTATE_90_CW), tmp_path / "out.jpg")
    assert _important(metadata)["dimensions"] == {
        "key": "dimensions",
        "before": {"width": 60, "height": 80},
        "after": {"width": 60, "height": 80},
        "source": "File:ImageWidth",
        "status": "same",
    }
    assert _expected(metadata)["File:ImageWidth"] == {
        "tag": "File:ImageWidth",
        "before": 80,
        "reason": ROTATED_DIMENSIONS_REASON,
    }


def test_exif_dimensions_follow_the_rotated_pixels(tmp_path: Path):
    metadata = _compress(_oriented_jpeg(tmp_path, ROTATE_90_CW), tmp_path / "out.jpg")
    assert metadata["after"]["ExifIFD:ExifImageWidth"] == 60
    assert metadata["after"]["ExifIFD:ExifImageHeight"] == 80
    assert _expected(metadata)["ExifIFD:ExifImageWidth"]["reason"] == ORIENTATION_REASON


def test_xmp_orientation_is_reset_too(tmp_path: Path):
    metadata = _compress(_oriented_jpeg(tmp_path, ROTATE_90_CW, xmp=True), tmp_path / "out.jpg")
    assert metadata["before"]["XMP-tiff:Orientation"] == "Rotate 90 CW"
    assert metadata["after"]["XMP-tiff:Orientation"] == "Horizontal (normal)"
    assert metadata["unexpected_losses"] == []


@pytest.mark.parametrize(
    ("orientation", "transpose"),
    [
        (ROTATE_90_CW, Image.Transpose.ROTATE_90),  # turned the other way
        (ROTATE_90_CW, Image.Transpose.ROTATE_180),  # turned twice, back to the stored layout
        (MIRROR_HORIZONTAL, Image.Transpose.FLIP_TOP_BOTTOM),
    ],
)
def test_output_displayed_otherwise_than_the_original_is_unexpected(
    tmp_path: Path, orientation: int, transpose: Image.Transpose
):
    metadata = _compress(_oriented_jpeg(tmp_path, orientation), tmp_path / "out.jpg", transpose=transpose)
    assert "pixels:orientation" in {loss["tag"] for loss in metadata["unexpected_losses"]}


def test_orientation_copied_back_onto_turned_pixels_is_unexpected():
    """The S23 portrait bug, which the command can no longer reach: `Rotate 90 CW` on pixels already turned."""
    before = {"File:ImageWidth": 4000, "File:ImageHeight": 3000, "IFD0:Orientation": "Rotate 90 CW"}
    after = {"File:ImageWidth": 3000, "File:ImageHeight": 4000, "IFD0:Orientation": "Rotate 90 CW"}
    metadata = diff_metadata(before, after, [], transparency=(False, False))
    assert {"tag": "File:ImageWidth", "kind": "changed", "before": 4000, "after": 3000} in metadata["unexpected_losses"]
    assert _important(metadata)["dimensions"]["after"] == {"width": 4000, "height": 3000}


def test_dimension_tag_lost_while_another_group_keeps_the_size_is_unexpected():
    """Only width and height swapped with the pixels are expected: a dimension tag merely lost is not."""
    before = {"File:ImageWidth": 320, "File:ImageHeight": 200, "PNG:ImageWidth": 320, "PNG:ImageHeight": 200}
    after = {"File:ImageWidth": 320, "File:ImageHeight": 200}
    metadata = diff_metadata(before, after, [], transparency=(False, False))
    assert {"tag": "PNG:ImageWidth", "kind": "removed", "before": 320, "after": None} in metadata["unexpected_losses"]


def test_changed_displayed_dimensions_of_a_rotated_portrait_are_unexpected(tmp_path: Path):
    metadata = _compress(_oriented_jpeg(tmp_path, ROTATE_90_CW), tmp_path / "out.jpg", resize=0.5)
    assert {"tag": "File:ImageWidth", "kind": "changed", "before": 80, "after": 30} in metadata["unexpected_losses"]
    assert _important(metadata)["dimensions"]["status"] == "changed"


def test_strip_of_a_portrait_rotated_by_tinify_has_no_unexpected_loss(tmp_path: Path):
    metadata = _compress(_oriented_jpeg(tmp_path, ROTATE_90_CW), tmp_path / "out.jpg", "--metadata", "strip")
    assert metadata["unexpected_losses"] == []


def test_strip_of_a_portrait_tinify_did_not_rotate_is_displayed_otherwise(tmp_path: Path):
    """Without its orientation, a viewer shows the stored pixels: the portrait is displayed lying down."""
    metadata = _compress(
        _oriented_jpeg(tmp_path, ROTATE_90_CW), tmp_path / "out.jpg", "--metadata", "strip", apply_orientation=False
    )
    assert _important(metadata)["dimensions"]["status"] == "changed"
    assert "pixels:orientation" in {loss["tag"] for loss in metadata["unexpected_losses"]}


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
