"""Fabricate `photo.jpg`: a camera-like JPEG for the compress-image and reduce-jpeg-size tests.

The repo is public, so no real photo is versioned. The picture is a grey gradient blended with a seeded, smoothed
noise and scattered ellipses: detailed enough that it can't be shrunk for free, so jpegoptim has to work to bring it
under a target size. 1200x800 at quality 95, about 400 KB. Its EXIF says what a camera writes: `Make` TestCamera,
`Model` TestModel, an orientation and a date, so a test can tell whether the metadata survived.

The noise comes from a fixed seed: running the script again writes the same pixels.

Run from the repo root: `uv run python src/compression_suite/compress_image/samples/make_photo_jpeg.py`
"""

import random
from pathlib import Path

from PIL import Image, ImageDraw

OUTPUT = Path(__file__).resolve().parent / "photo.jpg"

WIDTH, HEIGHT = 1200, 800
NOISE_CELL = 4  # one noise value per 4x4 block, smoothed when upscaled
ELLIPSES = 60
QUALITY = 95
SEED = 0

# EXIF tags (IFD0)
MAKE, MODEL, ORIENTATION, DATE_TIME = 0x010F, 0x0110, 0x0112, 0x0132


def picture(rng: random.Random) -> Image.Image:
    """Draw the gradient, the noise and the ellipses."""
    small = (WIDTH // NOISE_CELL, HEIGHT // NOISE_CELL)
    noise = Image.frombytes("RGB", small, rng.randbytes(small[0] * small[1] * 3))
    noise = noise.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)
    gradient = Image.linear_gradient("L").resize((WIDTH, HEIGHT)).convert("RGB")
    image = Image.blend(gradient, noise, 0.5)
    draw = ImageDraw.Draw(image)
    for _ in range(ELLIPSES):
        x, y, size = rng.randrange(WIDTH), rng.randrange(HEIGHT), rng.randrange(20, 200)
        draw.ellipse((x, y, x + size, y + size // 2), fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    return image


def main() -> None:
    """Write the picture with its EXIF."""
    exif = Image.Exif()
    exif[MAKE] = "TestCamera"
    exif[MODEL] = "TestModel"
    exif[ORIENTATION] = 1
    exif[DATE_TIME] = "2025:01:01 12:00:00"
    picture(random.Random(SEED)).save(OUTPUT, quality=QUALITY, exif=exif)


if __name__ == "__main__":
    main()
