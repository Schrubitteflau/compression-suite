"""Fabricate `screenshot.png`: a neutral screen capture laid out like gnome-screenshot's, with PNG text blocks.

A real capture would show personal content and the repo is public. Like gnome-screenshot's, it has a `Software`
text block and no date, so its capture date can only come from the file's mtime. A `Comment` block is added so
that more than one text block has to survive the compression.

Run from the repo root: `uv run python src/compression_suite/compress_image/samples/make_screenshot_png.py`
"""

from pathlib import Path

from PIL import Image, ImageDraw, PngImagePlugin

OUTPUT = Path(__file__).resolve().parent / "screenshot.png"


def main() -> None:
    """Draw a fake window and write it with its text blocks."""
    image = Image.new("RGBA", (320, 200), (246, 245, 244, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 319, 28), fill=(53, 132, 228, 255))
    draw.text((10, 8), "compression-suite", fill="white")
    for i in range(6):
        draw.rectangle((16, 44 + i * 24, 16 + 40 * (i + 2), 56 + i * 24), fill=(154, 153, 150, 255))

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "gnome-screenshot")
    info.add_text("Comment", "Fake screen capture for the compression-suite tests")
    image.save(OUTPUT, pnginfo=info)


if __name__ == "__main__":
    main()
