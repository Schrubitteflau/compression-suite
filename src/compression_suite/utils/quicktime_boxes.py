"""The few QuickTime (MP4, MOV, 3GP) boxes compress-video reads or writes itself, byte for byte.

- The edit list of each track (`elst`): ffmpeg plays wrongly a track that starts with an empty edit (an audio track
  starting a few milliseconds after the video, as a Galaxy writes it), cutting or adding the length of the empty edit
  at its end.
- The boxes of the user data (`moov/udta`) neither ffmpeg nor exiftool write, such as Samsung's `smta` (the phone's
  model): put back raw into the output. The index (`moov`) comes first in the output, so growing it moves the media
  data: every chunk offset (`stco`, `co64`) is moved by as much.
"""

from dataclasses import dataclass
from pathlib import Path

# Boxes holding other boxes, on the way to those read here
CONTAINER_BOXES = (b"moov", b"trak", b"edts", b"mdia", b"minf", b"stbl", b"udta")

# Chunk size when moving the media data after a box grew
MOVE_CHUNK = 1 << 20

STCO_MAX = 0xFFFFFFFF


@dataclass(frozen=True)
class Box:
    """A box: its type, where it starts, the size of its header, and where it ends (offsets in its buffer)."""

    kind: bytes
    start: int
    header: int
    end: int

    @property
    def body(self) -> int:
        return self.start + self.header


def boxes(data: bytes | bytearray, start: int, end: int) -> list[Box]:
    """The boxes lying in `data[start:end]`, one after the other (a truncated last box is left out)."""
    found = []
    while start + 8 <= end:
        size, header = int.from_bytes(data[start : start + 4], "big"), 8
        if size == 1:
            size, header = int.from_bytes(data[start + 8 : start + 16], "big"), 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            break
        found.append(Box(bytes(data[start + 4 : start + 8]), start, header, start + size))
        start += size
    return found


def _child(data: bytes | bytearray, parent: Box, kind: bytes) -> Box | None:
    return next((box for box in boxes(data, parent.body, parent.end) if box.kind == kind), None)


def read_moov(path: Path) -> tuple[int, bytes] | None:
    """The offset and bytes of the file's `moov` box, or None if it has none, read without reading the media data."""
    with path.open("rb") as file:
        size = path.stat().st_size
        offset = 0
        while offset + 8 <= size:
            file.seek(offset)
            header = file.read(16)
            length = int.from_bytes(header[:4], "big")
            if length == 1:
                length = int.from_bytes(header[8:16], "big")
            elif length == 0:
                length = size - offset
            if length < 8:
                return None
            if header[4:8] == b"moov":
                file.seek(offset)
                return offset, file.read(length)
            offset += length
    return None


@dataclass(frozen=True)
class EditList:
    """A track's edit list: entries (duration in the movie's time scale, media time in the track's, -1 for an empty
    edit) and both time scales."""

    entries: tuple[tuple[int, int], ...]
    movie_timescale: int
    media_timescale: int

    def delayed_start(self) -> tuple[float, float, float] | None:
        """For an empty edit followed by one edit of media, the delay, the media start and the duration played, in
        seconds; None for any other edit list."""
        if len(self.entries) != 2 or self.entries[0][1] != -1 or self.entries[1][1] < 0:
            return None
        if not self.movie_timescale or not self.media_timescale:
            return None
        (empty, _), (duration, media_time) = self.entries
        return empty / self.movie_timescale, media_time / self.media_timescale, duration / self.movie_timescale


def _full_box_version(data: bytes, box: Box) -> int:
    return data[box.body]


def edit_lists(path: Path) -> dict[int, EditList]:
    """The edit list of each track that has one, by track ID."""
    found = read_moov(path)
    if found is None:
        return {}
    _, moov = found
    root = Box(b"moov", 0, 8, len(moov))
    mvhd = _child(moov, root, b"mvhd")
    if mvhd is None:
        return {}
    movie_timescale = int.from_bytes(moov[mvhd.body + (20 if _full_box_version(moov, mvhd) == 1 else 12) :][:4], "big")
    lists = {}
    for trak in (box for box in boxes(moov, root.body, root.end) if box.kind == b"trak"):
        tkhd, edts, mdia = (_child(moov, trak, kind) for kind in (b"tkhd", b"edts", b"mdia"))
        elst = _child(moov, edts, b"elst") if edts else None
        mdhd = _child(moov, mdia, b"mdhd") if mdia else None
        if tkhd is None or elst is None or mdhd is None:
            continue
        track_id = int.from_bytes(moov[tkhd.body + (20 if _full_box_version(moov, tkhd) == 1 else 12) :][:4], "big")
        media_timescale = int.from_bytes(
            moov[mdhd.body + (20 if _full_box_version(moov, mdhd) == 1 else 12) :][:4], "big"
        )
        wide = _full_box_version(moov, elst) == 1
        count = int.from_bytes(moov[elst.body + 4 : elst.body + 8], "big")
        entries, offset = [], elst.body + 8
        for _ in range(count):
            size = 8 if wide else 4
            if offset + 2 * size + 4 > elst.end:
                break
            duration = int.from_bytes(moov[offset : offset + size], "big")
            media_time = int.from_bytes(moov[offset + size : offset + 2 * size], "big", signed=True)
            entries.append((duration, media_time))
            offset += 2 * size + 4
        lists[track_id] = EditList(tuple(entries), movie_timescale, media_timescale)
    return lists


def user_data_boxes(path: Path, kinds: tuple[bytes, ...]) -> list[bytes]:
    """The raw boxes of these types in the file's user data (each `moov/udta` box), in their order."""
    found = read_moov(path)
    if found is None:
        return []
    _, moov = found
    root = Box(b"moov", 0, 8, len(moov))
    return [
        moov[box.start : box.end]
        for udta in boxes(moov, root.body, root.end)
        if udta.kind == b"udta"
        for box in boxes(moov, udta.body, udta.end)
        if box.kind in kinds
    ]


def _with_size(data: bytes, box: Box, growth: int) -> bytes:
    """The header of a box grown by `growth` bytes."""
    size = box.end - box.start + growth
    if box.header == 16:
        return data[box.start : box.start + 8] + size.to_bytes(8, "big")
    return size.to_bytes(4, "big") + data[box.start + 4 : box.start + 8]


def _moved_chunk_offsets(moov: bytearray, growth: int, after: int) -> None:
    """Move by `growth` bytes, in place, every chunk offset (`stco`, `co64`) of the tracks from `after` on: the media
    data after the `moov` box. Those before it (an index written last) don't move."""
    root = Box(b"moov", 0, 8, len(moov))
    for trak in (box for box in boxes(moov, root.body, root.end) if box.kind == b"trak"):
        stbl = trak
        for kind in (b"mdia", b"minf", b"stbl"):
            found = _child(moov, stbl, kind)
            if found is None:
                break
            stbl = found
        else:
            for table in boxes(moov, stbl.body, stbl.end):
                if table.kind not in (b"stco", b"co64"):
                    continue
                size = 4 if table.kind == b"stco" else 8
                count = int.from_bytes(moov[table.body + 4 : table.body + 8], "big")
                for offset in range(table.body + 8, table.body + 8 + count * size, size):
                    value = int.from_bytes(moov[offset : offset + size], "big")
                    value += growth if value >= after else 0
                    if size == 4 and value > STCO_MAX:
                        raise ValueError("a chunk offset no longer fits in 32 bits")
                    moov[offset : offset + size] = value.to_bytes(size, "big")


def add_user_data_boxes(path: Path, added: list[bytes]) -> None:
    """Add raw boxes at the end of the file's user data (`moov/udta`, made if missing), in place.

    The media data after the `moov` box moves by their size, and the chunk offsets with it.

    Raises:
        ValueError: the file has no `moov` box, or a 32-bit chunk offset would overflow (nothing is written).
    """
    found = read_moov(path)
    if found is None:
        raise ValueError("no moov box")
    moov_offset, moov = found
    root = Box(b"moov", 0, 16 if moov[:4] == b"\0\0\0\1" else 8, len(moov))
    udta = _child(moov, root, b"udta")
    payload = b"".join(added)
    if udta is None:
        payload = (8 + len(payload)).to_bytes(4, "big") + b"udta" + payload
        grown = bytearray(moov + payload)
    else:
        grown = bytearray(
            moov[: udta.start] + _with_size(moov, udta, len(payload)) + moov[udta.start + udta.header : udta.end]
        )
        grown += payload + moov[udta.end :]
    growth = len(payload)
    grown[: root.header] = _with_size(moov, root, growth)
    _moved_chunk_offsets(grown, growth, moov_offset + len(moov))
    with path.open("r+b") as file:
        size = path.stat().st_size
        tail = moov_offset + len(moov)
        position = size
        while position > tail:  # from the end: the moved bytes never overwrite those still to move
            length = min(MOVE_CHUNK, position - tail)
            position -= length
            file.seek(position)
            chunk = file.read(length)
            file.seek(position + growth)
            file.write(chunk)
        file.seek(moov_offset)
        file.write(grown)
