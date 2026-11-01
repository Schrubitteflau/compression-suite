# preview-portion

Encode a portion of a video (3 s by default) at the exact settings of an export, and write it in H.264 for display in a browser. The video itself is only read: its bytes and its modification date never change.

Encoding at the export's settings is several times slower than playing (x265 at the `slow` speed): no real stream is possible. WelcomeApp calls this command once per portion, ahead of playback, and plays the portions next to the original. The command keeps nothing from one call to the next: which portions to encode, when to start again after a jump or a change of setting, and which portions to keep are WelcomeApp's choices.

ffmpeg and ffprobe are the pinned ones (`just install-tools`), never those of the `PATH`. Every path is handed to them with the `file:` protocol. Neither exiftool nor a Tinify key is needed.

## Usage

```bash
# Readable report on stderr
compression_suite preview-portion hevc_rotated.mp4 --preset default --start 0.25 -o /tmp/portion.mp4

# Another CRF than the preset's own, machine-readable output (one JSON document on stdout)
compression_suite preview-portion hevc_rotated.mp4 --preset screen --crf 38 --start 0.25 -o /tmp/portion.mp4 --json
```

## Options

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | *(required)* | Path to the video (never modified) |
| `--preset` | *(required)* | Id of the video preset, as `video-presets` lists it |
| `--crf` | the preset's | CRF in place of the preset's own, an integer within its `crf_bounds` |
| `--start` | *(required)* | Where the portion starts, in seconds from the start of the video: 0 or more, below the video's duration |
| `--duration` | `3` | Length of the portion, in seconds, above 0; the last portion of a video may be shorter |
| `--output`, `-o` | *(required)* | Path to the MP4 to write (its folder is created if needed), not ending with a slash, never the input itself. Always an MP4, whatever its extension |
| `--overwrite` | `false` | Overwrite output file if it exists |
| `--json` | `false` | Write a single JSON document on stdout |

## What is encoded

Two runs of ffmpeg:

1. **The portion, as the export encodes it**: the same preset settings as [compress-video](compress-video.md) (x265, its speed and CRF, `--crf` included, 4:2:0, an odd width or height losing its last column or row; Opus audio at the preset's bitrate, downmixed to a layout Opus takes, or no audio for `dance`), every frame at its own time, the rotation kept as a display matrix. Only the video stream and the first audio stream: the player plays one. The seek is exact: the portion starts at the first frame at or after `--start`, and holds the frames before `--start` + `--duration`. This encoding goes into an anonymous scratch file of the temporary folder (`$TMPDIR`, `/tmp` by default): it has no name, and vanishes with the process, even killed. Its size and bitrate are reported.
2. **The display rendering** of that encoded portion, as the [display copy](display-copy.md) renders a video: H.264 (libx264, CRF 16, `superfast`), 8 bits 4:2:0, every frame at its own time, the rotation applied to the pixels (shown as the original is shown, with no display matrix), an HDR video tone-mapped to SDR BT.709. The Opus audio is copied. No metadata. Index first (`+faststart`).

What the browser shows is thus what the export will look like: the x265 losses at this CRF, seen through a visually lossless H.264.

A portion weighs more than the same seconds in a whole export: it starts with a keyframe, where a whole export only places one every few seconds, and holds its own index and headers. The shorter the portion, the larger the gap.

## JSON output (`--json`)

The fabricated sample `hevc_rotated.mp4` (1 s), from 0.25 s: the last portion of the video, shorter than 3 s.

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "preview-portion",
  "result": {
    "input": "hevc_rotated.mp4",
    "output": "/tmp/portion.mp4",
    "preset": "default",
    "crf": 32,
    "start_seconds": 0.25,
    "duration_seconds": 0.75,
    "encoded": {"format": "mp4", "size_bytes": 8551, "bitrate_kbps": 91.2},
    "elapsed_seconds": 0.5
  }
}
```

- **`start_seconds`**, **`duration_seconds`**: where the portion really lies on the original's timeline, in seconds from the start of the original, to the millisecond. The portion's own timeline starts at 0: a time `t` of the portion is `start_seconds + t` in the original. `start_seconds` is where the portion really starts on the original's timeline. With audio, it is normally `--start` itself (the audio is cut there), but later when the video starts after the audio: a video starting at 0.2 s with audio from 0 gives, for `--start 0`, a `start_seconds` after 0 (0.143 for instance), and the audio before it is not in the portion. Without audio (`dance`), it is the time of the first frame: a `--start` between two frames gives the next frame's time. Either way, `start_seconds + t` is the original's time. `duration_seconds` runs to the end of the portion's last frame or sample: `--duration`, give or take a frame, less at the end of the video.
- **`encoded`**: the portion encoded with the preset (the x265 MP4 of step 1, not the H.264 output): `size_bytes`, and `bitrate_kbps`, its size over `duration_seconds`.
- **`preset`**, **`crf`**: the preset used and the CRF it was encoded with, the preset's own or `--crf`.
- **`elapsed_seconds`**: the time the command took.

## Writing and interruption

The output is written atomically, as the display copy is (see [display-copy](display-copy.md#atomic-writing)): an anonymous file of the output's folder, named only once complete. A portion interrupted at any point, even by SIGKILL, leaves neither a partial output nor a temporary file, in the output's folder or the temporary folder; ffmpeg stops with compression-suite. SIGINT and SIGTERM, at any point before the output takes its name, give `interrupted` (130); SIGHUP too while ffmpeg runs. Once the JSON document is written, a signal no longer changes the exit code. The output's folders that the command created are removed again if it fails or is interrupted (not after a SIGKILL: they then stay, empty).

## Errors

Every check that can fail without encoding runs first, in this order: preset and CRF, start and duration, input, output being the input, format, start within the video, output.

| Code | Exit | When |
|---|---|---|
| `usage_error` | 2 | invalid options; unknown preset (`details.preset`, `known_presets`); CRF out of bounds (`details.crf`, `crf_min`, `crf_max`); `--start` negative or not a number, `--duration` not above 0 or infinite; `--start` at or beyond the video's duration, or after its last frame (the end of the audio only would remain): `details.start`, `details.duration`; output that is the input itself (same path, hard link or symbolic link) or ends with a slash |
| `unsupported_format` | 21 | not a video: an image, a file ffmpeg doesn't recognise, a file with no video stream (audio only); `details.detected_format` |
| `input_corrupt` | 27 | a video that can't be decoded: as the display copy probes it (cut before its index, first frame or end missing), or any decoder error met while encoding the portion; the reason in `message`, nothing written |
| `input_not_found` | 23 | input missing, or not a file |
| `input_unreadable` | 26 | input present but unreadable |
| `output_exists` | 24 | output exists without `--overwrite`, is a symbolic link to a missing file without `--overwrite`, or is a folder |
| `tool_missing` | 30 | pinned ffmpeg or ffprobe not installed |
| `interrupted` | 130 | SIGINT or SIGTERM before the output took its name; nothing written |
| `internal_error` | 50 | ffmpeg failed for a reason that doesn't point at the input (disk full, for instance) |

## Limits

- **Damage before the portion**: the exact seek decodes from the keyframe before `--start`. A decoder error in those frames, which the portion doesn't show, makes the video damaged all the same; damage elsewhere in the video is not met (the video is probed only at its start and end).
- **A video with two video streams** gets a portion of the first one, while `compress-video` refuses it (`unsupported_format`).
- **Two simultaneous calls to the same `--output`** without `--overwrite` are not guarded against each other: both can report success, and the last one written wins. WelcomeApp gives each call its own output path.
