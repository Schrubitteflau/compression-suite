"""Reader of the Samsung end-of-file trailer (`SEFT`): named entries appended after the image, then their index.

Layout, little-endian, as exiftool's `Samsung::ProcessSamsung` reads it:

- each entry: `\\0\\0`, its type (int16u), the length of its name (int32u), its name (`Image_UTC_Data`,
  `MCC_Data`, `MotionPhoto_Data`...) and its value;
- the index: `SEFH`, a version (int32u), the entry count (int32u), then per entry `\\0\\0`, its type (int16u),
  its distance back from the start of the index (int32u) and its size (int32u);
- the length of the index (int32u), then `SEFT`, which ends the file.

The zone runs from the farthest entry to the end of the file. Only its entries and its index are described by
the index: a byte between them is not.
"""

import mmap
import struct
from dataclasses import dataclass

SEFT_MARKER = b"SEFT"
SEFH_MARKER = b"SEFH"

_INDEX_HEADER_SIZE = 12
_INDEX_ENTRY_SIZE = 12
_ENTRY_HEADER_SIZE = 8

# A whole file in memory, or mapped: a large file (a video passed to inspect) doesn't have to be read.
FileBytes = bytes | mmap.mmap


class SeftError(ValueError):
    """The file ends with `SEFT` but its index can't be read."""


@dataclass(frozen=True)
class SeftEntry:
    """One entry of the zone: `offset` and `size` locate it in the file, header and name included."""

    type: int
    name: str
    offset: int
    size: int

    @property
    def value_offset(self) -> int:
        """Where the entry's value starts in the file, after its header and name."""
        return self.offset + _ENTRY_HEADER_SIZE + len(self.name.encode("latin-1"))

    @property
    def value_size(self) -> int:
        return self.offset + self.size - self.value_offset


@dataclass(frozen=True)
class SeftZone:
    """The trailer, from `start` (its farthest entry) to `end` (the end of the file), its index from `index_start`."""

    start: int
    index_start: int
    end: int
    entries: tuple[SeftEntry, ...]

    def described_spans(self) -> list[tuple[int, int]]:
        """Byte ranges (end excluded) the index describes: each entry, then the index up to the end of the file."""
        return [(entry.offset, entry.offset + entry.size) for entry in self.entries] + [(self.index_start, self.end)]

    def entry(self, name: str) -> SeftEntry | None:
        """The first entry with this name, if any."""
        return next((entry for entry in self.entries if entry.name == name), None)


def read_seft(data: FileBytes) -> SeftZone | None:
    """Read the Samsung trailer at the end of `data`, or return None if the file doesn't end with `SEFT`.

    Raises SeftError if the file ends with `SEFT` but its index is unreadable or points outside the file.
    """
    if data[-len(SEFT_MARKER) :] != SEFT_MARKER:
        return None
    if len(data) < 8:
        raise SeftError("file too short for a SEFT index length")
    index_length = struct.unpack_from("<I", data, len(data) - 8)[0]
    index_start = len(data) - 8 - index_length
    if index_length < _INDEX_HEADER_SIZE or index_start < 0:
        raise SeftError(f"SEFT index length out of the file: {index_length}")
    if data[index_start : index_start + 4] != SEFH_MARKER:
        raise SeftError("SEFT index doesn't start with SEFH")
    count = struct.unpack_from("<I", data, index_start + 8)[0]
    if _INDEX_HEADER_SIZE + _INDEX_ENTRY_SIZE * count > index_length:
        raise SeftError(f"SEFT index too short for its {count} entries")

    entries = []
    for i in range(count):
        _, entry_type, distance, size = struct.unpack_from("<HHII", data, index_start + 12 + _INDEX_ENTRY_SIZE * i)
        if distance > index_start or size > distance or size < _ENTRY_HEADER_SIZE:
            raise SeftError(f"SEFT entry {i} out of the zone (distance {distance}, size {size})")
        offset = index_start - distance
        name_length = struct.unpack_from("<I", data, offset + 4)[0]
        if _ENTRY_HEADER_SIZE + name_length > size:
            raise SeftError(f"SEFT entry {i} name longer than the entry")
        name = data[offset + _ENTRY_HEADER_SIZE : offset + _ENTRY_HEADER_SIZE + name_length].decode("latin-1")
        entries.append(SeftEntry(entry_type, name, offset, size))

    start = min((entry.offset for entry in entries), default=index_start)
    return SeftZone(start, index_start, len(data), tuple(entries))
