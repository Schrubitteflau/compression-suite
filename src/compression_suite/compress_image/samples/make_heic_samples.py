"""Fabricate the HEIC samples: neutral pictures, since the real HEICs at hand are someone else's photos, with GPS.

- `oriented_p3.heic`: laid out like an iPhone photo, with a non-standard orientation (EXIF "Mirror horizontal and
  rotate 270 CW", which pillow-heif writes as `irot` + `imir` transformations) and a P3 ICC profile, the one of
  `s23_p3_seft.jpg`. Its stored pixels are a landscape: red left half, blue right half, green square in the top-left
  corner. Displayed, it is a portrait: red top half, blue bottom half, green square still top-left, which no rotation
  alone gives.
- `plain.heic`: no ICC profile and no orientation, like one of the real iPhone HEICs at hand.
- `tiled.heic`: a grid of 256 px tiles (4 x 3), like an iPhone photo (512 px tiles), so that a copy cut short loses
  whole tiles, which libheif fills in without an error. Diagonal lines, so each tile holds data of its own. Its
  256 px thumbnail, an auxiliary item like an iPhone's, is stored after the tiles: a copy cut by a few bytes loses only
  the thumbnail, and the main image still decodes whole.
- `over_limit.heic`: `plain.heic` with its image data declared in 33 extents of its `iloc` box, one more than libheif's
  security limit: libheif refuses to open it.

No HDR sample: pillow-heif can't write the auxiliary image that carries an Apple gain map.

Run from the repo root: `uv run python src/compression_suite/compress_image/samples/make_heic_samples.py`
"""

import struct
from pathlib import Path

import pillow_heif
from PIL import Image, ImageDraw

SAMPLES = Path(__file__).resolve().parent

# EXIF orientation 5, "Mirror horizontal and rotate 270 CW"
TRANSPOSE = 5

TILE_SIZE = 256
THUMBNAIL_SIZE = 256

# libheif's default security limit on the extents of one item, plus one
OVER_LIMIT_EXTENTS = 33


def stored_pixels() -> Image.Image:
    """The landscape pattern, as stored before any transformation."""
    image = Image.new("RGB", (64, 32), (220, 40, 40))
    image.paste((40, 40, 220), (32, 0, 64, 32))
    image.paste((40, 200, 40), (0, 0, 8, 8))
    return image


def tiled_pixels() -> Image.Image:
    """Diagonal lines across a 1024 x 768 picture, 12 tiles of 256 px."""
    image = Image.new("RGB", (1024, 768), (200, 200, 200))
    draw = ImageDraw.Draw(image)
    for x in range(0, 1024, 64):
        draw.line((x, 0, 1023 - x, 767), fill=(x // 4, 100, 255 - x // 4), width=9)
    return image


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _child_boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """The (type, start, end) of the boxes between start and end, headers included (no 64-bit sizes here)."""
    boxes = []
    while start < end:
        size, box_type = struct.unpack_from(">I4s", data, start)
        boxes.append((box_type, start, start + size))
        start += size
    return boxes


def over_limit(plain: bytes) -> bytes:
    """`plain.heic` whose single item, one extent of the `mdat` box, is redeclared as OVER_LIMIT_EXTENTS extents."""
    ftyp, meta, mdat = _child_boxes(plain, 0, len(plain))
    assert (ftyp[0], meta[0], mdat[0]) == (b"ftyp", b"meta", b"mdat")
    children = _child_boxes(plain, meta[1] + 12, meta[2])  # meta: 8-byte header, then a FullBox's version and flags
    iloc = next(child for child in children if child[0] == b"iloc")
    # version 0; 4-byte offsets, lengths and base offset; 1 item: ID, data reference, base offset, 1 extent
    item_id, base_offset, extent_count, offset, length = struct.unpack_from(">HxxIHII", plain, iloc[1] + 16)
    assert plain[iloc[1] + 8 : iloc[1] + 16] == bytes([0, 0, 0, 0, 0x44, 0x40, 0, 1]) and extent_count == 1
    lengths = [1] * (OVER_LIMIT_EXTENTS - 1) + [length - (OVER_LIMIT_EXTENTS - 1)]
    shift = (OVER_LIMIT_EXTENTS - 1) * 8  # the item's data moves along with the end of the grown `iloc` box
    extents = b"".join(struct.pack(">II", offset + i, size) for i, size in enumerate(lengths))
    new_iloc = _box(
        b"iloc",
        bytes([0, 0, 0, 0, 0x44, 0x40])
        + struct.pack(">HHHIH", 1, item_id, 0, base_offset + shift, OVER_LIMIT_EXTENTS)
        + extents,
    )
    meta_payload = plain[meta[1] + 8 : iloc[1]] + new_iloc + plain[iloc[2] : meta[2]]
    return plain[: meta[1]] + _box(b"meta", meta_payload) + plain[meta[2] :]


def main() -> None:
    """Write the samples."""
    icc_profile = Image.open(SAMPLES / "s23_p3_seft.jpg").info["icc_profile"]
    exif = Image.Exif()
    exif[0x0112] = TRANSPOSE
    oriented = pillow_heif.from_pillow(stored_pixels())
    oriented.save(SAMPLES / "oriented_p3.heic", exif=exif.tobytes(), icc_profile=icc_profile, quality=90)
    pillow_heif.from_pillow(stored_pixels()).save(SAMPLES / "plain.heic", quality=90)
    tiled = pillow_heif.from_pillow(tiled_pixels())
    tiled.info["thumbnails"] = [THUMBNAIL_SIZE]  # written after the tiles, as the last data of the file
    tiled.save(SAMPLES / "tiled.heic", quality=90, tile_size=TILE_SIZE)
    (SAMPLES / "over_limit.heic").write_bytes(over_limit((SAMPLES / "plain.heic").read_bytes()))


if __name__ == "__main__":
    main()
