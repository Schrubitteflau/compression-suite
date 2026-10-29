"""Fabricate the HEIC samples: neutral pictures, since the real HEICs at hand are someone else's photos, with GPS.

- `oriented_p3.heic`: laid out like an iPhone photo, with a non-standard orientation (EXIF "Mirror horizontal and
  rotate 270 CW", which pillow-heif writes as `irot` + `imir` transformations) and a P3 ICC profile, the one of
  `s23_p3_seft.jpg`. Its stored pixels are a landscape: red left half, blue right half, green square in the top-left
  corner. Displayed, it is a portrait: red top half, blue bottom half, green square still top-left, which no rotation
  alone gives.
- `plain.heic`: no ICC profile and no orientation, like one of the real iPhone HEICs at hand.

No HDR sample: pillow-heif can't write the auxiliary image that carries an Apple gain map.

Run from the repo root: `uv run python src/compression_suite/compress_image/samples/make_heic_samples.py`
"""

from pathlib import Path

import pillow_heif
from PIL import Image

SAMPLES = Path(__file__).resolve().parent

# EXIF orientation 5, "Mirror horizontal and rotate 270 CW"
TRANSPOSE = 5


def stored_pixels() -> Image.Image:
    """The landscape pattern, as stored before any transformation."""
    image = Image.new("RGB", (64, 32), (220, 40, 40))
    image.paste((40, 40, 220), (32, 0, 64, 32))
    image.paste((40, 200, 40), (0, 0, 8, 8))
    return image


def main() -> None:
    """Write both samples."""
    icc_profile = Image.open(SAMPLES / "s23_p3_seft.jpg").info["icc_profile"]
    exif = Image.Exif()
    exif[0x0112] = TRANSPOSE
    oriented = pillow_heif.from_pillow(stored_pixels())
    oriented.save(SAMPLES / "oriented_p3.heic", exif=exif.tobytes(), icc_profile=icc_profile, quality=90)
    pillow_heif.from_pillow(stored_pixels()).save(SAMPLES / "plain.heic", quality=90)


if __name__ == "__main__":
    main()
