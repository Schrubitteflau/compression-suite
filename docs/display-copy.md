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
- The video is re-encoded only if it is at fault (codec, pixel format or HDR): H.264 (libx264), CRF 16, preset `superfast`, `yuv420p`. Visually lossless, and fast: on an 8-thread machine, the whole command takes about 12 s for a 20 s 1080p HEVC from a Galaxy S23, and 5.5 s for a 7.6 s one. When only the container or the audio is at fault, the video stream is copied as is.
- **Timestamps**: every frame keeps its exact time (`-fps_mode passthrough`, `-enc_time_base:v demux`): same frames, same definition, same timeline as the original, even when a phone's frames don't come at an exact rate (their times, on a 90 kHz clock, are not snapped to multiples of 1/30 s).
- **Orientation**: the rotation is applied to the pixels of a re-encoded video, which then carries no display matrix: a portrait stays a portrait, rotated once. A copied video stream keeps its display matrix, which browsers apply.
- **HDR**: a PQ or HLG video is tone-mapped to SDR BT.709, 8 bits (zscale to linear light, peak 100 cd/m², `tonemap=hable`, back to BT.709), and tagged BT.709. Primaries or matrix the video doesn't give (only its transfer is tagged, or only the container's `colr` box) are taken as BT.2020; the constant-luminance BT.2020 matrix, which zscale lacks, as the non-constant one. An SDR 10-bit video is only brought down to 8 bits.
- The audio is copied as is if it is AAC, MP3 or Opus, else re-encoded to AAC 192 kb/s.
- No metadata (dates, GPS): the copy is for display only. Its index comes first (`+faststart`): the browser starts at once.
- Always MP4, whatever the extension of `--output`.

The rendering of the video (codec, timestamps, orientation, HDR) is shared with the other commands that hand a video to WelcomeApp's players.

## JSON output (`--json`)

A copy was needed (the fabricated sample `hevc_rotated.mp4`):

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "display-copy",
  "result": {
    "input": "hevc_rotated.mp4",
    "copy_needed": true,
    "copy_reasons": ["video codec hevc"],
    "output": "/tmp/hevc_rotated.display.mp4",
    "duration": 1.0,
    "width": 72,
    "height": 128,
    "fps": 30.0,
    "video_codec": "hevc",
    "audio_codec": "aac"
  }
}
```

No copy needed: `copy_needed` is `false`, `copy_reasons` is empty, there is no `output`, and nothing is written, not even the folder of `--output`. An existing file at `--output` is then left alone, without `--overwrite` too, and the command still succeeds.

- **`copy_reasons`**: why the browser may not play the original, in plain English (`video codec hevc`, `pixel format yuv420p10le`, `hdr smpte2084`, `container matroska,webm`, `audio codec pcm_s16le`...), for a log or a tooltip; not a stable code.
- **`duration`**: in seconds, the container's.
- **`width`**, **`height`**: as displayed, rotation applied: a phone portrait is taller than wide. Those of the copy too.
- **`fps`**: the average frame rate, to the thousandth (a phone recording at "30 fps" gives slightly less or more).
- **`video_codec`**, **`audio_codec`**: those of the original, as ffprobe names them (`hevc`, `h264`, `aac`, `opus`...); `audio_codec` is `null` without audio.

## Damaged videos

Before deciding, the video is probed: its first frame is decoded, and its last seconds (3 s before the end of the video stream, in the stream's own time, whatever the audio lasts or the timestamps start at), whose frames must reach the end the container announces for the video stream itself, within two frames: its start plus its duration (MP4, MPEG-TS), or its `DURATION` tag, the stream's end (Matroska, WebM). Without a duration of the video stream's own (the container's covers the audio too), frames only have to come out. A video whose decoded frames stop short is truncated (`input_corrupt`): an MP4 cut in its data, its index first, still announces every frame. A seek that lands in the middle of a group of pictures (MPEG-TS) is not an error: if no frame comes out of the window, the whole stream is decoded instead.

- **No copy needed**: nothing more is decoded, so that a long video costs no wait. A video damaged only in the middle is served as is.
- **Copy**: everything is decoded, a stream copied as is included (into a second output that writes nothing), and any error of the input's demuxer or decoders makes the video damaged (`input_corrupt`), even when ffmpeg conceals it: ffmpeg is pinned, so a decoder error means a damaged file, as for the images. Nothing is written.

## Atomic writing

ffmpeg writes into an anonymous file (`O_TMPFILE`) of the output's folder, which gets its name only once complete: a short hidden name (`.<8 hex>.tmp`, so that an output name of up to 255 bytes works), then an atomic rename over the output. A copy interrupted at any point, even by SIGKILL, leaves neither a partial copy nor a temporary file. From the start of ffmpeg to the final rename, SIGTERM, SIGINT and SIGHUP stop ffmpeg and give `interrupted` (exit 130); a SIGHUP already ignored (`nohup`) stays ignored. ffmpeg also stops if compression-suite is killed. The output's folders that the command created are removed again if the copy fails or is interrupted (not after a SIGKILL: they then stay, empty). On a file system without `O_TMPFILE`, a hidden temporary file (`.<random>.tmp`) is used instead: removed on any error or signal it can catch, but left behind by a SIGKILL.

## Errors

| Code | Exit | When |
|---|---|---|
| `unsupported_format` | 21 | not a video: an image (detected on the content, a HEIC included), a file ffmpeg doesn't recognise, a file with no video stream (audio only); `details.detected_format` |
| `input_corrupt` | 27 | a video that can't be decoded: a video container ffprobe can't open, or holding no stream (an MP4 cut before its index, which a phone writes last), whose first frame can't be decoded, whose frames stop short of its end (truncated), or with any decoder error during the copy; the reason in `message`, nothing written |
| `input_not_found` | 23 | input missing, or not a file |
| `input_unreadable` | 26 | input present but unreadable |
| `output_exists` | 24 | a copy is needed and the output exists without `--overwrite`, or is a folder |
| `tool_missing` | 30 | pinned ffmpeg or ffprobe not installed (`just install-tools`) |
| `interrupted` | 130 | SIGINT, SIGTERM or SIGHUP during the copy; nothing written |
| `internal_error` | 50 | ffmpeg failed for a reason that doesn't point at the input (disk full, for instance) |
| `usage_error` | 2 | invalid options, `--output` missing, or the output is the input file (same path, hard link or symbolic link, with or without `--overwrite`), checked before anything is read or written |

## Limits

- **Damage in the middle of a video that needs no copy** is not detected (see [Damaged videos](#damaged-videos)).
- **Non-square pixels**: `width` and `height` are the stored ones, rotation applied; a sample aspect ratio is not applied.
- **Browsers checked**: Chrome 156 and Firefox 136 under Linux play neither picture of a S23 HEVC (Chrome plays its sound only, Firefox refuses it), and play the copy upright, as well as an H.264 MP4 or MOV, rotated or not.
