# compress-video

Compress a whole video with a named preset (see [video-presets](video-presets.md)), to store it in place of the original. The original is only read: its bytes and its modification date never change.

The output is an MP4: HEVC video (x265) and Opus audio, or no audio for a preset without one (`dance`). Its definition, frame rate and the time of every frame are those of the original (a phone's variable frame rate included); only an odd width or height loses its last column or row. The metadata of the original are kept, the creation date, GPS and rotation among them, and the output gets the original's modification date.

It runs the pinned ffmpeg, ffprobe and exiftool (see [doctor](doctor.md)), never those of the `PATH`, and needs no API key. Encoding takes time: about 3 times the video's duration for a 1080p phone video at the `default` preset.

## Usage

```bash
# Readable progress bar and report on stderr
compression_suite compress-video 20240601_120000.mp4 --preset default -o /tmp/20240601_120000.mp4

# Another CRF than the preset's own
compression_suite compress-video lecture.mp4 --preset screen --crf 38 -o /tmp/lecture.mp4

# Machine-readable output: the progress as JSON lines on stderr, one JSON document on stdout
compression_suite compress-video 20240601_120000.mp4 --preset default -o /tmp/20240601_120000.mp4 --json
```

## Options

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | *(required)* | Path to the video to compress (never modified) |
| `--preset` | *(required)* | Id of the video preset, as `video-presets` lists it |
| `--crf` | the preset's | CRF in place of the preset's own, an integer within its `crf_bounds` |
| `--output`, `-o` | *(required)* | Path to the MP4 to write (its folder is created if needed), not ending with a slash. Always an MP4, whatever its extension |
| `--overwrite` | `false` | Overwrite output file if it exists |
| `--json` | `false` | Write a single JSON document on stdout, and the progress as JSON lines on stderr |

## What is encoded

- The video stream: a source must hold exactly one, besides its cover arts (attached pictures, never encoded, even when one comes first). A source with two video streams is refused (`unsupported_format`).
- Every audio stream, each encoded with the preset's audio settings (a preset without audio drops them all). Opus only takes some channel layouts: mono, stereo, 3.0, quad, 5.0, 5.1, 6.1 and 7.1. A track in another layout is downmixed to the one keeping the most channels, with no more channels, and no more non-LFE channels, than it has: 4.0 gives quad, 2.1 stereo, hexagonal and 5.1(side) give 5.1. A track whose layout is unknown (PCM in a MOV) counts all its channels as non-LFE.
- Other streams are not kept: subtitles, cover arts, data tracks (Apple's `mebx` metadata, Samsung's).
- The video is decoded without applying its rotation: the pixels are stored as in the original, and the rotation stays in the metadata (the track's display matrix). A player shows the output the same way as the original.
- Every frame is kept, at its own time, in the original's time base.
- The index (`moov`) is written at the start of the file, so that a player can start before reading it all.

## Metadata

ffmpeg keeps the rotation, the track languages and handler names. It writes the creation date into every QuickTime date, modification dates included, and turns a zero date into the creation date: for an MP4, MOV or 3GP original, exiftool writes back the original's dates, those of the movie (`CreateDate`, `ModifyDate`) and of each track (`TrackCreateDate`, `TrackModifyDate`, `MediaCreateDate`, `MediaModifyDate`, found by track ID), zero dates included. A Matroska, WebM, AVI or MPEG-TS original has no such dates: the output's come from its `creation_time`, if any. exiftool also copies back from the original what ffmpeg doesn't write in an MP4: the user data (`©xyz` GPS of Android phones, author, Samsung's play mode), the `mdta` keys (Android version, capture frame rate and time zone; Apple's location and model), the item list and the XMP. ffmpeg's own copy of the location (a 3GPP `loci` box) is not written: the output would hold a second GPS the original doesn't have.

Some fields change by nature (brands, sizes, bitrates, encoder name, durations of a few milliseconds for Opus) and a few can't be written back (Samsung's `smta` box, giving the phone's model). The diff of the metadata before and after is not reported yet.

## Progress (`--json`)

With `--json`, the progress is written on stderr, one JSON object per line, about every half second while encoding:

```json
{"schema_version": 1, "command": "compress-video", "progress": {"encoded_seconds": 0.0, "duration_seconds": 7.59, "percent": 0.0}}
{"schema_version": 1, "command": "compress-video", "progress": {"encoded_seconds": 3.52, "duration_seconds": 7.59, "percent": 46.4}}
{"schema_version": 1, "command": "compress-video", "progress": {"encoded_seconds": 7.59, "duration_seconds": 7.59, "percent": 100.0}}
```

- **`encoded_seconds`**: the seconds of video encoded so far; **`duration_seconds`**: the video's duration.
- **`percent`**: never goes back, stays below 100 while encoding; the last line, at 100, is written once the output is in place, just before the document on stdout. `duration_seconds` and `percent` are `null` for a video whose duration ffprobe can't read (a Matroska file written to a pipe, for instance); the last line then has `percent` 100 and the seconds encoded.

The first line comes once the checks have passed, when encoding starts. An export that fails or is stopped writes no line at 100. Without `--json`, stderr shows a progress bar and a report instead.

## JSON output (`--json`)

stdout only holds one JSON document:

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "compress-video",
  "result": {
    "input": {"path": "20240601_120000.mp4", "format": "mp4", "size_bytes": 14100000, "mtime": "2024-06-01T10:00:12.123456+00:00"},
    "output": {"path": "/tmp/20240601_120000.mp4", "format": "mp4", "size_bytes": 1090000, "mtime": "2024-06-01T10:00:12.123456+00:00"},
    "duration_seconds": 7.59,
    "elapsed_seconds": 24.5,
    "preset": "default",
    "crf": 32
  }
}
```

- **`format`**: the container, detected on the content: `mp4`, `mov`, `3gp`, `matroska`, `webm`, `avi` or `mpegts` for the input; always `mp4` for the output.
- **`mtime`**: ISO 8601 in UTC; the output's is the input's.
- **`duration_seconds`**: the video's duration (`null` when ffprobe can't read it); **`elapsed_seconds`**: the time the export took.
- **`preset`**, **`crf`**: the preset used and the CRF it was encoded with, the preset's own or `--crf`.

## Writing and interruption

The output is written atomically: ffmpeg writes a temporary file next to it (`.<name>.<random>.tmp`), exiftool rewrites it, it gets the original's dates (atime and mtime), then it takes the output's name. A reader never sees a half-written output. Without `--overwrite`, a file that appeared at the output during the export is kept: the export fails with `output_exists` (the name is given by a hard link, which can't replace a file; on a filesystem without hard links, the output is checked again just before the rename).

An export that fails, or is stopped by SIGTERM or SIGINT (Ctrl+C) at any point before the output takes its name, kills ffmpeg or exiftool, removes the temporary file and the output's folders it created (if still empty), and exits with `interrupted` (130) for a signal. From the moment the output takes its name until the JSON document is written, both signals are ignored: the export is done and reported as such (exit 0), never `interrupted` with a complete output on disk. A SIGKILL is the exception, see [Known limits](#known-limits).

Paths are given to ffmpeg and ffprobe as absolute `file:` paths: a file named `2024-06-01T10:30:00.mp4` is a file, not a protocol.

## Errors

Every check that can fail without encoding runs first, in this order: preset and CRF, pinned tools, input, format, output.

| Code | Exit | When |
|---|---|---|
| `usage_error` | 2 | invalid options, unknown preset (`details.preset`, `known_presets`), CRF out of bounds (`details.crf`, `crf_min`, `crf_max`), output that is the input itself or ends with a slash |
| `unsupported_format` | 21 | the reason in `message`, `details.detected_format`: an image (`jpeg`...); a video in a container other than those above (`video container mpeg not supported (supported containers: …)`, `detected_format` the container ffprobe names); not a video at all (`unknown`); a supported container without a video stream (audio only) or with more than one (`holds 2 video streams, only one is supported`) |
| `input_not_found` | 23 | input missing, or not a file |
| `output_exists` | 24 | output exists without `--overwrite` (before the export, or appeared during it), is a symbolic link to a missing file without `--overwrite`, or is a folder |
| `metadata_copy_failed` | 25 | exiftool failed to copy the metadata back |
| `input_unreadable` | 26 | input present but unreadable |
| `input_corrupt` | 27 | a video ffprobe can't open (a phone video cut before its index) or ffmpeg can't decode to the end (frames missing, damaged data, a Matroska or WebM file whose end is gone): `details.detected_format`, ffmpeg's last messages in `message`, nothing written |
| `tool_missing` | 30 | pinned ffmpeg, ffprobe or exiftool not installed, not runnable, or of another version than the manifest's |
| `internal_error` | 50 | ffmpeg failed on a video it decodes to the end (`message` gives its last messages), or any other error |
| `interrupted` | 130 | stopped by SIGINT (Ctrl+C) or SIGTERM before the output took its name |

A damaged video is told from a failure of the encoder by decoding it alone once the encoding has failed, or reported an error without failing: ffmpeg stops at the first decoding error (`-xerror`) rather than encode a video cut short, and any error it reports while decoding counts (a truncated Matroska only gives "File ended prematurely"). This decoding check, run on 48 real phone videos (S23 camera and WhatsApp, read-only copies), flagged none of them.

## Known limits

- **A truncated MPEG-TS may pass**: MPEG-TS has no index and declares no duration. A file cut in the middle of a packet is `input_corrupt`, but one cut exactly between the packets of each stream is a shorter, valid stream: it exports as a shorter video.
- **SIGKILL leaves the temporary file**: a process killed by SIGKILL (`kill -9`, the OOM killer, a power cut) can't clean up. The temporary file `.<name>.<random>.tmp`, and possibly exiftool's `.<name>.<random>.tmp_exiftool_tmp`, stays next to the output, along with any folder created for it. The output itself is never left half-written: it only gets its name once complete. No later export removes these files: delete them by hand. Stop an export with SIGTERM or SIGINT, never SIGKILL.
