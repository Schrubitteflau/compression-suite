# Video samples for the video command tests

The repo is public: no real video may be versioned (a phone video carries its GPS). The samples are fabricated by `make_video_samples.py` with the pinned ffmpeg, from a lavfi pattern and a sine wave: no real data, and the GPS is fake. They last one second (30 fps, about 10 KB). Their paths and colours are importable from `compression_suite.video_samples`.

Their pixels show the pattern of the HEIC samples: red left half, blue right half, green square in the top-left corner, so a test tells from the decoded pixels whether a rotation was applied.

- `hevc_rotated.mp4`: laid out like a Galaxy S23 portrait video. HEVC Main 8 bits 4:2:0 (tag `hvc1`), stored as a 128x72 landscape with a display matrix of -90 degrees (ffprobe `rotation=-90`, exiftool `Rotation: 90`): displayed as a 72x128 portrait, red top half, blue bottom half, green square top-right. AAC audio, brand `mp42`, creation date 2024-06-15 10:00:00 UTC, fake GPS 12.3456 N, 65.4321 E (`©xyz`, as the S23 writes it).
- `h264.mp4`: playable by a browser as is. H.264 High 8 bits 4:2:0, AAC audio, brand `isom`, no rotation: 128x72.
- `hdr_pq.mp4`: an HDR10 video, as the S23 records with its "HDR10+ video" option. HEVC Main 10 (`yuv420p10le`), tagged BT.2020 primaries and matrix and PQ transfer (`smpte2084`), in the bitstream and the container. No audio, no rotation, 128x72. The pattern's colours are only tagged PQ, not converted: tests check the copy's tags and that it isn't black, not its exact colours.

Variants a test needs once (another container, another audio codec, 10 bits, a truncated file) are derived from these in the test itself.

The encoders run on one thread, so that regenerating gives the same bytes on any machine (with the same pinned ffmpeg).

Regenerate: `uv run python src/compression_suite/video_samples/make_video_samples.py` (after `just install-tools`).
