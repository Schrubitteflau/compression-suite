"""The HEIF layout check on its own: it must stay bounded on a crafted `iloc`, whatever libheif makes of it."""

import struct

import pytest

from compression_suite.utils.decoding import missing_item_data


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _heif(extent_count: int, item_count: int = 1) -> bytes:
    """A HEIF whose items have `extent_count` zero-size extents each, based at byte 1,000,000 of a tiny file."""
    item = struct.pack(">HHIH", 0, 0, 1_000_000, extent_count)  # method, data ref, base offset, extent count
    iloc = (
        bytes([1, 0, 0, 0])  # version 1, flags
        + bytes([0x00, 0x40])  # offset and length: 0 bytes; base offset: 4 bytes, no index
        + struct.pack(">H", item_count)
        + b"".join(struct.pack(">H", item_id) + item for item_id in range(1, item_count + 1))
    )
    return _box(b"ftyp", b"heic\0\0\0\0mif1heic") + _box(b"meta", b"\0\0\0\0" + _box(b"iloc", iloc))


@pytest.mark.parametrize(("extent_count", "item_count"), [(32, 1), (1, 1000)])
def test_data_past_the_end_is_reported(extent_count: int, item_count: int):
    data = _heif(extent_count, item_count)
    assert (
        missing_item_data(data) == f"file cut short, its image data runs to byte 1000000 but the file has {len(data)}"
    )


@pytest.mark.parametrize("extent_count", [33, 65535])
def test_more_extents_than_libheif_allows_is_left_to_libheif(extent_count: int):
    assert missing_item_data(_heif(extent_count)) is None


def test_more_items_than_libheif_allows_is_left_to_libheif():
    assert missing_item_data(_heif(1, 1001)) is None
