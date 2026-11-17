"""Fabricate the PNG samples of `compress-image --format jpeg`: neutral pictures, since the repo is public.

- `opaque_photo.png`: an opaque 8-bit RGB PNG laid out like a photo exported by a photo manager: a camera EXIF block
  stored in an `eXIf` chunk (`Make` TestCamera, `Model` TestModel, a capture date and a fake GPS position), an XMP
  packet (a keyword and a rating) and the P3 ICC profile of `s23_p3_seft.jpg`. Its picture is a gradient with
  ellipses, smooth enough to keep the file small, with saturated colours that the P3 profile changes.

Run from the repo root: `uv run python src/compression_suite/compress_image/samples/make_png_samples.py`
"""

import random
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


def main() -> None:
    """Write the samples."""
    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", XMP)
    picture(random.Random(SEED)).save(
        SAMPLES / "opaque_photo.png", exif=camera_exif(), icc_profile=p3_profile(), pnginfo=info
    )


if __name__ == "__main__":
    main()
