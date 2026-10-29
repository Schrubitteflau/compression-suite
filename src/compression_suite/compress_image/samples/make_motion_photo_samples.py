"""Fabricate the motion photo samples: one per isolated case, the real S23 motion photo carrying both formats at once.

- `google_motion_photo.jpg`: small JPEG with a tiny MP4 appended and the XMP `GCamera` + `Container:Directory`
  pointing to it, as a Pixel writes it.
- `samsung_motion_photo.jpg`: small JPEG followed by a Samsung end-of-file trailer (`SEFT`) whose `MotionPhoto_Data`
  entry holds the same MP4, laid out like the S23's (entries, then the `SEFH` index, its length and `SEFT`).
- `trailing_data.jpg`: small JPEG followed by bytes that no known structure explains.

The MP4 is made by ffmpeg (one blue frame, no audio), which is only needed to run this script.

Run from the repo root: `uv run python src/compression_suite/compress_image/samples/make_motion_photo_samples.py`
"""

import io
import struct
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

SAMPLES = Path(__file__).resolve().parent

# Entry types of the S23's trailer, as its `SEFH` index lists them.
IMAGE_UTC_DATA = 0x0A01
MCC_DATA = 0x0AA1
MOTION_PHOTO_DATA = 0x0A30


def small_jpeg(xmp: bytes | None = None) -> bytes:
    """Encode a small neutral JPEG, with an XMP packet if given."""
    image = Image.new("RGB", (64, 48), (53, 132, 228))
    buf = io.BytesIO()
    if xmp is None:
        image.save(buf, format="JPEG")
    else:
        image.save(buf, format="JPEG", xmp=xmp)
    return buf.getvalue()


def tiny_mp4() -> bytes:
    """Encode one blue frame as an MP4 with ffmpeg."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "video.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x3584e4:s=64x48:d=0.1:r=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                str(out),
            ],
            check=True,
        )
        return out.read_bytes()


def google_xmp(video_length: int) -> bytes:
    """XMP of a Google motion photo whose video is the last `video_length` bytes of the file."""
    return f"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"
        xmlns:Container="http://ns.google.com/photos/1.0/container/"
        xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
      GCamera:MotionPhoto="1"
      GCamera:MotionPhotoVersion="1"
      GCamera:MotionPhotoPresentationTimestampUs="0">
      <Container:Directory>
        <rdf:Seq>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Padding="0"/>
          </rdf:li>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="{video_length}"/>
          </rdf:li>
        </rdf:Seq>
      </Container:Directory>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>""".encode()


def seft_trailer(entries: list[tuple[int, str, bytes]]) -> bytes:
    """Build a Samsung trailer: each entry, then the `SEFH` index, its length and the `SEFT` marker.

    An entry is `\\0\\0`, its type (int16u), the length of its name (int32u), its name and its value; the index
    gives, for each entry, its type, its distance back from the start of the index and its size (little-endian).
    """
    blocks = [
        struct.pack("<HHI", 0, entry_type, len(name)) + name.encode() + value for entry_type, name, value in entries
    ]
    index = b"SEFH" + struct.pack("<II", 107, len(blocks))
    distance = sum(len(block) for block in blocks)
    for (entry_type, _, _), block in zip(entries, blocks, strict=True):
        index += struct.pack("<HHII", 0, entry_type, distance, len(block))
        distance -= len(block)
    return b"".join(blocks) + index + struct.pack("<I", len(index)) + b"SEFT"


def main() -> None:
    """Write the three fabricated samples."""
    video = tiny_mp4()
    (SAMPLES / "google_motion_photo.jpg").write_bytes(small_jpeg(google_xmp(len(video))) + video)
    (SAMPLES / "samsung_motion_photo.jpg").write_bytes(
        small_jpeg()
        + seft_trailer(
            [
                (IMAGE_UTC_DATA, "Image_UTC_Data", b"1718445600000"),
                (MCC_DATA, "MCC_Data", b"208"),
                (MOTION_PHOTO_DATA, "MotionPhoto_Data", video),
            ],
        ),
    )
    (SAMPLES / "trailing_data.jpg").write_bytes(small_jpeg() + b"bytes that no known structure explains")


if __name__ == "__main__":
    main()
