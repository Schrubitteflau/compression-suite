"""Fabricate the PNG samples of `compress-image --format jpeg`: neutral pictures, since the repo is public.

- `opaque_photo.png`: an opaque 8-bit RGB PNG laid out like a photo exported by a photo manager: a camera EXIF block
  stored in an `eXIf` chunk (`Make` TestCamera, `Model` TestModel, a capture date and a fake GPS position), an XMP
  packet (a keyword and a rating) and the P3 ICC profile of `s23_p3_seft.jpg`. Its picture is a gradient with
  ellipses, smooth enough to keep the file small, with saturated colours that the P3 profile changes.
- `opaque_rgba.png`: the same picture in an RGBA PNG whose pixels are all fully opaque, like a screenshot with alpha.
- `transparent_pixel.png`: `opaque_rgba.png` with one pixel fully transparent, the least a transparency can be.
- `truncated.png`: an RGB PNG of the same picture cut in the middle of its pixel data, as by an interrupted sync.
- `gray.png`, `gray16.png`, `rgb16.png`, `palette.png`: the pixel modes other than 8-bit RGB, without metadata. Each
  is 256 columns wide, column `x` holding the 8-bit level `x` (on 16 bits, `x * 257`, so that the 16-bit gradient spans
  the whole range): `gray.png` 8-bit grayscale, `gray16.png` 16-bit grayscale, `rgb16.png` 16-bit RGB (red rising,
  green falling, blue at half), `palette.png` the colours of `rgb16.png` on 8 bits in a palette of 64 entries.
- `gray16_alpha.png`: the gradient of `gray16.png` with a 16-bit alpha channel, fully opaque, and an `sBIT` chunk,
  which Pillow opens as RGBA.
- `gray16_icc.png`: `gray16.png` with a grey ICC profile (gamma 2.2), fabricated here.
- `text_blocks.png`: an opaque 8-bit RGB PNG with no EXIF and no XMP, only text blocks: one of every keyword that
  has a standard equivalent in a JPEG (`Title`, `Description` as international text, `Author`, `Copyright`,
  `Comment`, `Software`, `Creation Time`, its only date), an unknown keyword (`Mood`) and an international text
  without equivalent (`Disclaimer`, in French).

Run from the repo root: `uv run python src/compression_suite/compress_image/samples/make_png_samples.py`
"""

import io
import random
import struct
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, PngImagePlugin

SAMPLES = Path(__file__).resolve().parent

WIDTH, HEIGHT = 320, 240
ELLIPSES = 12
SEED = 0

# EXIF tags: IFD0, the Exif and GPS sub-IFDs
MAKE, MODEL, ORIENTATION = 0x010F, 0x0110, 0x0112
EXIF_IFD, DATE_TIME_ORIGINAL = 0x8769, 0x9003
GPS_IFD, LATITUDE_REF, LATITUDE, LONGITUDE_REF, LONGITUDE = 0x8825, 1, 2, 3, 4

XMP = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="compression-suite samples">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/">
   <dc:subject><rdf:Bag><rdf:li>sample</rdf:li></rdf:Bag></dc:subject>
   <xmp:Rating>4</xmp:Rating>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def picture(rng: random.Random) -> Image.Image:
    """A horizontal gradient with saturated ellipses."""
    image = Image.linear_gradient("L").rotate(90).resize((WIDTH, HEIGHT)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for _ in range(ELLIPSES):
        x, y, size = rng.randrange(WIDTH), rng.randrange(HEIGHT), rng.randrange(20, 120)
        color = rng.choice([(230, 30, 30), (30, 200, 40), (30, 60, 230), (240, 200, 20)])
        draw.ellipse((x, y, x + size, y + size // 2), fill=color)
    return image


def camera_exif() -> Image.Exif:
    """The EXIF of a camera photo, with a fake position."""
    exif = Image.Exif()
    exif[MAKE] = "TestCamera"
    exif[MODEL] = "TestModel"
    exif[ORIENTATION] = 1
    exif.get_ifd(EXIF_IFD)[DATE_TIME_ORIGINAL] = "2025:03:04 10:20:30"
    gps = exif.get_ifd(GPS_IFD)
    gps[LATITUDE_REF], gps[LATITUDE] = "N", (12.0, 20.0, 44.16)
    gps[LONGITUDE_REF], gps[LONGITUDE] = "E", (65.0, 25.0, 55.56)
    return exif


def p3_profile() -> bytes:
    """The P3 ICC profile of the S23 sample."""
    with Image.open(SAMPLES / "s23_p3_seft.jpg") as photo:
        return photo.info["icc_profile"]


GRADIENT_WIDTH, GRADIENT_HEIGHT = 256, 16


def gradient_rgb(x: int) -> tuple[int, int, int]:
    """The 8-bit colour of column `x` of `rgb16.png` and `palette.png`."""
    return x, 255 - x, 128


def gray16() -> Image.Image:
    """A 16-bit grayscale gradient, column `x` at `x * 257`."""
    image = Image.new("I;16", (GRADIENT_WIDTH, GRADIENT_HEIGHT))
    image.putdata([x * 257 for _ in range(GRADIENT_HEIGHT) for x in range(GRADIENT_WIDTH)])
    return image


def png_by_hand(colour_type: int, samples: list[tuple[int, ...]], chunks: list[tuple[bytes, bytes]]) -> bytes:
    """A 16-bit PNG of the gradient, `samples[x]` the 16-bit samples of column `x`, with `chunks` before its pixels,
    written by hand: Pillow can't write a 16-bit PNG with colour."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"".join(struct.pack(f">{len(sample)}H", *sample) for sample in samples)
    pixels = b"".join(b"\0" + row for _ in range(GRADIENT_HEIGHT))  # filter type 0 on each row
    header = struct.pack(">IIBBBBB", GRADIENT_WIDTH, GRADIENT_HEIGHT, 16, colour_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + b"".join(chunk(kind, data) for kind, data in chunks)
        + chunk(b"IDAT", zlib.compress(pixels, 9))
        + chunk(b"IEND", b"")
    )


def rgb16_png() -> bytes:
    """A 16-bit RGB PNG of the gradient, each level at `level * 257`."""
    return png_by_hand(2, [tuple(level * 257 for level in gradient_rgb(x)) for x in range(GRADIENT_WIDTH)], [])


def gray16_alpha_png() -> bytes:
    """A 16-bit grey and alpha PNG of the gradient, fully opaque, with an `sBIT` chunk (16 significant bits each)."""
    return png_by_hand(4, [(x * 257, 65535) for x in range(GRADIENT_WIDTH)], [(b"sBIT", bytes((16, 16)))])


def gray_profile() -> bytes:
    """A minimal ICC v2 grey display profile, gamma 2.2: no grey profile ships with Pillow."""

    def xyz(x: float, y: float, z: float) -> bytes:
        return struct.pack(">3i", *(round(v * 65536) for v in (x, y, z)))

    name = b"compression-suite gray gamma 2.2\0"
    tags = [
        (b"desc", b"desc" + bytes(4) + struct.pack(">I", len(name)) + name + bytes(4 + 4 + 2 + 1 + 67)),
        (b"wtpt", b"XYZ " + bytes(4) + xyz(0.9642, 1.0, 0.8249)),
        (b"kTRC", b"curv" + bytes(4) + struct.pack(">IH", 1, round(2.2 * 256)) + bytes(2)),
        (b"cprt", b"text" + bytes(4) + b"No copyright, use freely\0"),
    ]
    table_size = 4 + 12 * len(tags)
    offset, table, data = 128 + table_size, struct.pack(">I", len(tags)), b""
    for signature, tag in tags:
        tag += bytes(-len(tag) % 4)  # tags aligned on 4 bytes
        table += signature + struct.pack(">II", offset + len(data), len(tag))
        data += tag
    size = 128 + table_size + len(data)
    header = (
        struct.pack(">I", size)
        + bytes(4)
        + bytes((2, 0x10, 0, 0))  # version 2.1
        + b"mntrGRAYXYZ "
        + bytes(12)  # creation date
        + b"acsp"
        + bytes(4 + 4 + 4 + 4 + 8 + 4)  # platform, flags, manufacturer, model, attributes, intent
        + xyz(0.9642, 1.0, 0.8249)  # PCS illuminant, D50
        + bytes(4 + 16 + 28)  # creator, ID, reserved
    )
    return header + table + data


def palette() -> Image.Image:
    """The gradient of `rgb16.png` on 8 bits, in a palette of 64 colours."""
    image = Image.new("RGB", (GRADIENT_WIDTH, GRADIENT_HEIGHT))
    image.putdata([gradient_rgb(x) for _ in range(GRADIENT_HEIGHT) for x in range(GRADIENT_WIDTH)])
    return image.quantize(64)


def text_blocks() -> PngImagePlugin.PngInfo:
    """Text blocks of every keyword with a JPEG equivalent, plus two without."""
    info = PngImagePlugin.PngInfo()
    info.add_text("Title", "Sample title")
    info.add_itxt("Description", "Été au bord de l'eau")
    info.add_text("Author", "Test Author")
    info.add_text("Copyright", "CC0 1.0")
    info.add_text("Comment", "Fabricated for the compression-suite tests")
    info.add_text("Software", "make_png_samples.py")
    info.add_text("Creation Time", "2025:03:04 10:20:30")
    info.add_text("Mood", "calm")
    info.add_itxt("Disclaimer", "Image fabriquée", lang="fr", tkey="Avertissement")
    return info


def main() -> None:
    """Write the samples."""
    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", XMP)
    picture(random.Random(SEED)).save(
        SAMPLES / "opaque_photo.png", exif=camera_exif(), icc_profile=p3_profile(), pnginfo=info
    )
    Image.linear_gradient("L").rotate(90).resize((GRADIENT_WIDTH, GRADIENT_HEIGHT)).save(SAMPLES / "gray.png")
    gray16().save(SAMPLES / "gray16.png")
    (SAMPLES / "rgb16.png").write_bytes(rgb16_png())
    (SAMPLES / "gray16_alpha.png").write_bytes(gray16_alpha_png())
    gray16().save(SAMPLES / "gray16_icc.png", icc_profile=gray_profile())
    palette().save(SAMPLES / "palette.png")

    rgba = picture(random.Random(SEED)).convert("RGBA")
    rgba.save(SAMPLES / "opaque_rgba.png")
    rgba.putpixel((0, 0), (0, 0, 0, 0))
    rgba.save(SAMPLES / "transparent_pixel.png")

    whole = io.BytesIO()
    picture(random.Random(SEED)).save(whole, format="PNG")
    (SAMPLES / "truncated.png").write_bytes(whole.getvalue()[: len(whole.getvalue()) // 2])
    picture(random.Random(SEED)).resize((WIDTH // 4, HEIGHT // 4)).save(
        SAMPLES / "text_blocks.png", pnginfo=text_blocks()
    )


if __name__ == "__main__":
    main()
