# display-copy

Tell whether a browser plays a video as is, and if not, write an H.264 copy of it, for display only. The video itself is only read: its bytes and its modification date never change, and it is the file that gets stored.

A browser under Linux doesn't play HEVC, the codec of phone videos: WelcomeApp shows the copy instead. A video the browser already plays is **not** copied: the command says so, writes nothing, and WelcomeApp serves the original. Opening a long video that plays as is costs no wait.

ffmpeg and ffprobe are the pinned ones (`just install-tools`), never those of the `PATH`. Every path is handed to them with the `file:` protocol: a file named `concat:x.mov` is that file, never ffmpeg's `concat` protocol. Neither exiftool nor a Tinify key is needed.

## Usage

```bash
# Readable report on stderr
compression_suite display-copy hevc_rotated.mp4 -o /tmp/hevc_rotated.display.mp4

# Machine-readable output (one JSON document on stdout)
compression_suite display-copy hevc_rotated.mp4 -o /tmp/hevc_rotated.display.mp4 --json
```

## Options

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | *(required)* | Path to the video (never modified) |
| `--output`, `-o` | *(required)* | Path to the MP4 copy to write, if one is needed (its folder is created if needed); never the input itself |
| `--overwrite` | `false` | Overwrite output file if it exists |
| `--json` | `false` | Write a single JSON document on stdout |

## Played as is

The rule is deliberately narrow: in doubt, a copy is made. A video is played as is when all of these hold:

- **container** MP4 or MOV: ffprobe's `mov,mp4,m4a,3gp,3g2,mj2` demuxer, with the brand `qt  ` (MOV) or an MP4 brand (`isom`, `iso2`, `iso4` to `iso6`, `mp41`, `mp42`, `avc1`, `M4V `); 3GP and unknown brands are copied;
- **video** (the first video stream that isn't cover art) H.264, profile Constrained Baseline, Baseline, Main or High, 8 bits 4:2:0: pixel format `yuv420p`, or `yuvj420p` (full range, as some Android phones write it: Chrome and Firefox play it);
- **SDR**: an HDR transfer (PQ `smpte2084`, HLG `arib-std-b67`) is copied, whatever the codec, for its tone mapping (below);
- **audio** (the first audio stream) absent, AAC, MP3 or Opus.

A rotation (display matrix) doesn't call for a copy: browsers apply it.

## The copy

- Only the video stream and the first audio stream, for the whole duration.
- The video is re-encoded only if it is at fault (codec, pixel format or HDR): H.264 (libx264), CRF 16, preset `superfast`, `yuv420p`. Visually lossless, and fast: on an 8-thread machine, a phone's 1080p HEVC is copied faster than it plays; decoding the HEVC takes a good part of that time. When only the container or the audio is at fault, the video stream is copied as is.
- **Timestamps**: every frame keeps its exact time (`-fps_mode passthrough`, `-enc_time_base:v demux`): same frames, same definition, same timeline as the original, even when a phone's frames don't come at an exact rate (their times, on a 90 kHz clock, are not snapped to multiples of 1/30 s).
- **Orientation**: the rotation is applied to the pixels of a re-encoded video, which then carries no display matrix: a portrait stays a portrait, rotated once. A copied video stream keeps its display matrix, which browsers apply.
- **HDR**: a PQ or HLG video is tone-mapped to SDR BT.709, 8 bits (zscale to linear light, peak 100 cd/m², `tonemap=hable`, back to BT.709), and tagged BT.709. Primaries or matrix the video doesn't give (only its transfer is tagged, or only the container's `colr` box), or gives outside a short allow-list of common values checked with the pinned ffmpeg (primaries `bt709`, `bt470bg`, `smpte170m`, `bt2020`, `smpte431`, `smpte432`; matrix `bt709`, `bt470bg`, `smpte170m`, `bt2020nc`), are taken as BT.2020: some values outside it zscale refuses (an RGB matrix on YUV, `smpte2085`, `smpte428` primaries with a `chroma-derived-nc` matrix); the constant-luminance BT.2020 matrix, which zscale lacks, as the non-constant one. An SDR 10-bit video is only brought down to 8 bits.
- The audio is copied as is if it is AAC, MP3 or Opus, else re-encoded to AAC 192 kb/s.
- No metadata (dates, GPS): the copy is for display only. Its index comes first (`+faststart`): the browser starts at once.
- Always MP4, whatever the extension of `--output`.

The rendering of the video (codec, timestamps, orientation, HDR) is shared with the other commands that hand a video to WelcomeApp's players.

## JSON output (`--json`)

A copy was needed (the fabricated sample `hevc_rotated.mp4`):

```json
{
  "schema_version": 2,
  "ok": true,
  "command": "display-copy",
  "result": {
    "input": {"path": "hevc_rotated.mp4", "format": "mp4", "size_bytes": 9769, "mtime": "2024-06-15T10:00:00+00:00"},
    "output": {"path": "/tmp/hevc_rotated.display.mp4", "format": "mp4", "size_bytes": 8080, "mtime": "2026-11-01T09:30:52.797220+00:00"},
    "duration_seconds": 1.0,
    "elapsed_seconds": 0.2,
    "copy_needed": true,
    "copy_reasons": ["video codec hevc"],
    "width": 72,
    "height": 128,
    "fps": 30.0,
    "video_codec": "hevc",
    "audio_codec": "aac"
  }
}
```

No copy needed: `copy_needed` is `false`, `copy_reasons` is empty, `output` is `null`, and nothing is written, not even the folder of `--output`. An existing file at `--output` is then left alone, without `--overwrite` too, and the command still succeeds.

`input`, `output`, `duration_seconds` and `elapsed_seconds` are the fields every video command gives, with the same values for the same video (see [the common contract](../README.md#the-video-commands-common-contract)):

- **`input`**: the path as given, the container detected on the content (`mp4`, `mov`, `matroska`...), the size and the mtime (ISO 8601, UTC). **`output`**: the same for the copy (`format` always `mp4`, its own mtime), `null` without a copy.
- **`duration_seconds`**: the video's duration, the container's, to the millisecond; `null` when the container gives none.
- **`elapsed_seconds`**: the time the command took.
- **`copy_reasons`**: why the browser may not play the original, in plain English (`video codec hevc`, `pixel format yuv420p10le`, `hdr smpte2084`, `container matroska,webm`, `audio codec pcm_s16le`...), for a log or a tooltip; not a stable code.
- **`width`**, **`height`**: as displayed, rotation applied: a phone portrait is taller than wide. Those of the copy too.
- **`fps`**: the average frame rate, to the thousandth (a phone recording at "30 fps" gives slightly less or more).
- **`video_codec`**, **`audio_codec`**: those of the original, as ffprobe names them (`hevc`, `h264`, `aac`, `opus`...); `audio_codec` is `null` without audio.

## Damaged videos

Before deciding, the video is probed: its first frame is decoded, and its last seconds (3 s before the end of the video stream, in the stream's own time, whatever the audio lasts or the timestamps start at), whose frames must reach the end the container announces for the video stream itself, within two frames: its start plus its duration (MP4, MPEG-TS), or its `DURATION` tag, the stream's end (Matroska, WebM). Without a duration of the video stream's own (the container's covers the audio too), or with a `DURATION` tag that doesn't read as `HH:MM:SS.n` (ignored), frames only have to come out. A video whose decoded frames stop short is truncated (`input_corrupt`): an MP4 cut in its data, its index first, still announces every frame. A seek that lands in the middle of a group of pictures (MPEG-TS) is not an error: if no frame comes out of the window, the whole stream is decoded instead.

- **No copy needed**: nothing more is decoded, so that a long video costs no wait. A video damaged only in the middle is served as is.
- **Copy**: everything is decoded, a stream copied as is included (into a second output that writes nothing; a copy that only changes the container, or re-encodes only the audio, then takes about twice as long as without that decoding, an accepted cost), and any error of the input's demuxer or decoders makes the video damaged (`input_corrupt`), even when ffmpeg conceals it: ffmpeg is pinned, so a decoder error means a damaged file, as for the images. Nothing is written.

## Atomic writing

The writing is shared by the three video commands. ffmpeg writes into an anonymous file (`O_TMPFILE`) of the output's folder, which gets its name only once complete: without `--overwrite`, by a hard link, which fails if a file appeared at the output meanwhile (`output_exists`, that file kept); with it, under a short hidden name (`.<8 hex>.tmp`, so that an output name of up to 255 bytes works), then an atomic rename over the output. A copy interrupted at any point, even by SIGKILL, leaves neither a partial copy nor a temporary file. SIGTERM, SIGINT and SIGHUP, from the moment the command starts its checks until the copy takes its name (while the video is probed too), stop ffmpeg if it runs and give `interrupted` (exit 130); a SIGHUP already ignored (`nohup`) stays ignored. A signal while the process is still starting (Python loading the command, about the first 0.2 to 0.3 s) ends it by that signal (exit 128 + the signal's number, 143 for SIGTERM), with no JSON on stdout and nothing written. Once the copy has its name, a signal no longer changes the outcome, nor the exit code of the JSON document. ffmpeg also stops if compression-suite is killed. The output's folders that the command created are removed again if the copy fails or is interrupted (not after a SIGKILL: they then stay, empty). A new folder that a parallel call removed meanwhile, as it cleaned up after its own interruption, is made again before the copy takes its name. On a file system without `O_TMPFILE`, a hidden temporary file (`.<random>.tmp`) is used instead: removed on any error or signal it can catch, but left behind by a SIGKILL.

## Errors

| Code | Exit | When |
|---|---|---|
| `unsupported_format` | 21 | not a video: an image (detected on the content, a HEIC included), a file ffmpeg doesn't recognise or reads as text, a file with no video stream (audio only); `details.detected_format`: the image format (`jpeg`...), the container (`mp4` for an audio-only M4A), or `unknown` |
| `input_corrupt` | 27 | a video that can't be decoded: a video container ffprobe can't open, or holding no stream (an MP4 cut before its index, which a phone writes last), whose first frame can't be decoded, whose frames stop short of its end (truncated), or with any decoder error during the copy; the reason in `message`, `details.detected_format` (the container), nothing written |
| `input_not_found` | 23 | input missing, or not a file |
| `input_unreadable` | 26 | input present but unreadable |
| `output_exists` | 24 | a copy is needed and the output exists without `--overwrite` (before the copy, or appeared during it), is a symbolic link to a missing file without `--overwrite`, or is a folder |
| `tool_missing` | 30 | pinned ffmpeg or ffprobe not installed (`just install-tools`) |
| `interrupted` | 130 | SIGINT, SIGTERM or SIGHUP from the start of the checks until the copy took its name, the probe included; nothing written |
| `internal_error` | 50 | ffmpeg failed for a reason that doesn't point at the input (disk full, for instance) |
| `usage_error` | 2 | invalid options, `--output` missing, or the output is the input file (same path, hard link or symbolic link, with or without `--overwrite`), checked before anything is read or written; a copy is needed and the output ends with a slash |

## Limits

- **Damage in the middle of a video that needs no copy** is not detected (see [Damaged videos](#damaged-videos)).
- **Two simultaneous calls to the same `--output`** with `--overwrite` both report success, and the last one written wins; without it, the second one to finish gets `output_exists`. WelcomeApp gives each call its own output path.
- **Non-square pixels**: `width` and `height` are the stored ones, rotation applied; a sample aspect ratio is not applied.
- **Browsers checked**: Chrome 156 and Firefox 136 under Linux play neither picture of a S23 HEVC (Chrome plays its sound only, Firefox refuses it), and play the copy upright, as well as an H.264 MP4 or MOV, rotated or not.
