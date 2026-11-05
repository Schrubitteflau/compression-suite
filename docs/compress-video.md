# compress-video

Compress a whole video with a named preset (see [video-presets](video-presets.md)), to store it in place of the original. The original is only read: its bytes and its modification date never change.

The output is an MP4: HEVC video (x265) and Opus audio, or no audio for a preset without one (`dance`). Its definition, frame rate and the time of every frame are those of the original (a phone's variable frame rate included); only an odd width or height loses its last column or row. The metadata of the original are kept, the creation date, GPS and rotation among them, and the output gets the original's modification date.

It runs the pinned ffmpeg, ffprobe and exiftool (see [doctor](doctor.md)), never those of the `PATH`, and needs no API key. Encoding takes time: at the `slow` speed of every preset, x265 encodes a 1080p video several times slower than it plays, so an export of a phone video takes longer than the video lasts.

## Usage

```bash
# Readable progress bar and report on stderr
compression_suite compress-video clip.mp4 --preset default -o /tmp/clip.mp4

# Another CRF than the preset's own
compression_suite compress-video lecture.mp4 --preset screen --crf 38 -o /tmp/lecture.mp4

# Machine-readable output: the progress as JSON lines on stderr, one JSON document on stdout
compression_suite compress-video clip.mp4 --preset default -o /tmp/clip.mp4 --json
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

The original is read as every video command reads it (see [the common contract](../README.md#the-video-commands-common-contract)): any container ffprobe reads, holding a video stream (MP4, MOV, 3GP, Matroska, WebM, AVI, MPEG-TS, FLV, Ogg, MPEG-PS...), its first frame and its last seconds decoded before anything is written.

- The video stream: a source must hold exactly one, besides its cover arts (attached pictures, never encoded, even when one comes first). A source with two video streams is refused (`unsupported_format`).
- Every audio stream, each encoded with the preset's audio settings (a preset without audio drops them all). Opus only takes some channel layouts: mono, stereo, 3.0, quad, 5.0, 5.1, 6.1 and 7.1. A track in another layout is downmixed to the one keeping the most channels, with no more channels, and no more non-LFE channels, than it has: 4.0 gives quad, 2.1 stereo, hexagonal and 5.1(side) give 5.1. The chosen layout may still hold a channel the track lacks: hexagonal, which has no LFE, gives 5.1 with an LFE channel left silent. A track whose layout is unknown (PCM in a MOV) counts all its channels as non-LFE.
- Other streams are not kept: subtitles, cover arts, data tracks (Apple's `mebx` metadata, Samsung's).
- The video is decoded without applying its rotation: the pixels are stored as in the original, and the rotation stays in the metadata (the track's display matrix). A player shows the output the same way as the original.
- Every frame is kept, at its own time, in the original's time base.
- An audio track that starts after the picture (an edit list beginning with an empty edit, as a Galaxy writes its sound, a few milliseconds late) keeps its start and its length: the export plays that edit itself, ffmpeg alone would move the track's end by the length of the empty edit. The [preview portions](preview-portion.md) play the audio with the same code.
- The index (`moov`) is written at the start of the file, so that a player can start before reading it all.

## Metadata

ffmpeg keeps the rotation, the track languages and handler names. It writes the creation date into every QuickTime date, modification dates included, and turns a zero date into the creation date: for an MP4, MOV or 3GP original, exiftool writes back the original's dates, those of the movie (`CreateDate`, `ModifyDate`) and of each track (`TrackCreateDate`, `TrackModifyDate`, `MediaCreateDate`, `MediaModifyDate`, found by track ID), zero dates included. An original in another container (Matroska, WebM, AVI, MPEG-TS...) has no such dates: the output's come from its `creation_time`, if any. exiftool also copies back from the original what ffmpeg doesn't write in an MP4: the user data (`©xyz` GPS of Android phones, author, Samsung's play mode), the `mdta` keys (Android version, capture frame rate and time zone; Apple's location and model), the item list and the XMP. ffmpeg's own copy of the location (a 3GPP `loci` box) is not written: the output would hold a second GPS the original doesn't have. Samsung's `smta` box (the phone's model), which neither ffmpeg nor exiftool writes, is put back raw into the output's user data: the media data after the index moves by its size, and every chunk offset with it. When it can't be (an output close to 4 GiB, whose 32-bit chunk offsets would overflow), the output is kept without it, and the model is an expected loss.

Some fields change by nature (brands, sizes, bitrates, encoder name, track IDs). The [metadata diff](#metadata-diff) lists them all.

### Metadata diff

`result.metadata` compares the tags of the original and of the output, read by the pinned exiftool on every group (`exiftool -j -G1 -a`, tags keyed `Group:Name`), in the same shape as [`compress-image`'s](compress-image.md#metadata-diff---json): `important`, `unexpected_losses`, `expected_losses`, `added`, `before`, `after`. The ignored groups are the same (`System`, `Composite`, `ExifTool`).

- **Tracks**: exiftool numbers the tracks of each file in order (`Track1`, `Track2`…), and the export numbers them anew: a dropped data track, or a track ffmpeg adds (the timecode track it writes for an original that has one), would shift them. In `after`, each track of the output is named after the original track it comes from (found by its `TrackID`), so that a track is compared with its own original, and a track the export doesn't keep is lost as a whole. A track of the output's own (ffmpeg's timecode track) keeps its name, or takes the next number free when an original track has it.
- **`important`**: always these fields, in this order, even when intact, with `before`, `after`, `source` and `status` as for images (`same`, `lost`, `changed`, `absent`):
  - `capture_date`: Apple's `Keys:CreationDate` (local time with its offset, `2024:06:01 10:00:00+02:00`), else the movie's `QuickTime:CreateDate`, stored in UTC and given in the same form: in the local time of the offset a Galaxy records (`Keys:AndroidTimeZone`, `+0200`), else in UTC with `+00:00`; else a Matroska, AVI or XMP date, as exiftool reads it, else the file's mtime (`source: "file_mtime"`). A date never set (`0000:00:00 00:00:00`) counts as absent
  - `gps`: `{coordinates}`, as exiftool reads Apple's `Keys:GPSCoordinates` or Android's `UserData:GPSCoordinates` (`©xyz`)
  - `camera`: `{make, model}` from Apple's or Android's keys or the user data, else `{model}` from Samsung's `smta` box (`Samsung:SamsungModel`)
  - `dimensions`: `{width, height}` of the video track as displayed, its rotation applied: a portrait video stored 1920×1080 is `{width: 1080, height: 1920}`
  - `rotation`: the clockwise rotation a player applies to the video track (0, 90, 180 or 270), read on its display matrix (`source: "Track1:MatrixStructure"`)
- **`expected_losses`** (`tag`, `before`, `reason`), declared in `compress_video/main.py`:
  - the encoding parameters the export chooses anew, with the reason « réencodage »: every `File` field (`FileType`… for a MOV original) but the dimensions; the brands, media data size and offset and time scale of the movie; the time scale, media duration, compressor, bit depth, frame rate, buffer size and bit rates of each track; the audio format, sample rate and bits per sample. The media durations (`mdhd`) count the delay of x265's B-frames and Opus's priming, which the edit list removes: the durations of the movie and of each track (`mvhd`, `tkhd`) are still compared
  - the track IDs given anew, the export numbering its tracks from 1 (`TrackID` of each track, `QuickTime:NextTrackID`, a video track's `TimecodeTrack`), « pistes renumérotées par l'export »
  - every audio track with a preset without audio (`dance`), with the reason « piste audio retirée par le préréglage « Danse » »
  - when a track is dropped, the duration of the movie (`QuickTime:Duration`), which lasts as long as its longest track: a phone's sound often lasts a few milliseconds longer than its picture. « durée du film : celle des pistes gardées »; the duration of each kept track is still compared
  - every other track the export doesn't keep (data tracks: Apple's `mebx`, Samsung's, a timecode; subtitles), « piste non conservée : l'export ne garde que la vidéo et l'audio (données, sous-titres…) »
  - the channel count of an audio track downmixed for Opus (see [What is encoded](#what-is-encoded)), with the layouts: « Opus ne prend pas la disposition 2.1 : piste mixée en stereo »
  - an odd stored width or height cropped by one, « largeur ou hauteur impaire : dernière colonne ou ligne retirée (x265 en 4:2:0) »
  - only when its box can't be put back (see [Metadata](#metadata)), Samsung's model (`Samsung:SamsungModel`), « boîte Samsung « smta » : ne peut être remise, la vidéo dépasserait les 4 Go qu'adressent ses index »: `camera` is then `lost`. Any other Samsung tag lost (the `sefd` box of a slow-motion clip, after the media data) is unexpected
  - for an original in another container than MP4, MOV or 3GP (Matroska, WebM, AVI, MPEG-TS...), every other loss, « conteneur matroska converti en MP4 »: its tags have no counterpart in an MP4
- **Never expected**: a date (any tag whose name holds `Date`), GPS (`GPS` in its name) or display matrix (`MatrixStructure`) of the movie or of a track the export keeps, whatever else is declared. A track the export doesn't keep loses its dates with it, as expected.
- **`unexpected_losses`** (`tag`, `kind`, `before`, `after`): any other tag that disappeared (`removed`) or changed (`changed`). In human mode, their tags are printed on stderr.
- **`added`** (`tag`, `after`): tags that appear in the output (bit rates, the encoder's name, ffmpeg's timecode track…).

On a Galaxy S23 video (HEVC, rotated or not, with GPS), the export loses nothing unexpectedly, with or without its sound (`dance`): `capture_date`, `gps`, `camera` and `rotation` stay `same`, and the expected losses are the encoding parameters (and the track IDs when the export renumbers them), and with `dance` the audio track and the movie's duration. Known limit: a MOV original also shows, as unexpected, tags of the QuickTime format an MP4 doesn't hold (`HandlerClass`, `VendorID`, an audio channel layout box, a language `Unknown (32767)` written `und`), and the `HandlerType` of a MOV track may be read on its data reference (`URL`). A Matroska or AVI date shows as an unexpected loss, though the output holds it as its creation date.

## Progress (`--json`)

With `--json`, the progress is written on stderr, one JSON object per line, about every half second while encoding (here, a fabricated 4-second 320×240 clip, encoded in less than a second):

```json
{"schema_version": 2, "command": "compress-video", "progress": {"encoded_seconds": 0.0, "duration_seconds": 4.0, "percent": 0.0}}
{"schema_version": 2, "command": "compress-video", "progress": {"encoded_seconds": 4.0, "duration_seconds": 4.0, "percent": 99.9}}
{"schema_version": 2, "command": "compress-video", "progress": {"encoded_seconds": 4.0, "duration_seconds": 4.0, "percent": 100.0}}
```

- **`encoded_seconds`**: the seconds of video encoded so far; **`duration_seconds`**: the video's duration.
- **`percent`**: never goes back, stays below 100 while encoding; the last line, at 100, is written once the output is in place, just before the document on stdout. `duration_seconds` and `percent` are `null` for a video whose duration ffprobe can't read (a Matroska file written to a pipe, for instance); the last line then has `percent` 100 and the seconds encoded.

Stderr also holds the line announcing the ffmpeg run (see [the common contract](../README.md#the-video-commands-common-contract)), which has no `progress`. The first progress line comes once the checks have passed, when encoding starts. An export that fails or is stopped writes no line at 100. Without `--json`, stderr shows a progress bar and a report instead.

## JSON output (`--json`)

stdout only holds one JSON document (the same clip):

```json
{
  "schema_version": 2,
  "ok": true,
  "command": "compress-video",
  "result": {
    "input": {"path": "clip.mp4", "format": "mp4", "size_bytes": 56613, "mtime": "2024-06-01T08:00:12+00:00"},
    "output": {"path": "/tmp/clip.mp4", "format": "mp4", "size_bytes": 39416, "mtime": "2024-06-01T08:00:12+00:00"},
    "duration_seconds": 4.0,
    "elapsed_seconds": 0.7,
    "preset": "default",
    "crf": 32,
    "metadata": {
      "important": [
        {"key": "capture_date", "before": "2024:06:01 08:00:00+00:00", "after": "2024:06:01 08:00:00+00:00", "source": "QuickTime:CreateDate", "status": "same"},
        {"key": "gps", "before": null, "after": null, "source": null, "status": "absent"},
        {"key": "camera", "before": null, "after": null, "source": null, "status": "absent"},
        {"key": "dimensions", "before": {"width": 240, "height": 320}, "after": {"width": 240, "height": 320}, "source": "Track1:ImageWidth", "status": "same"},
        {"key": "rotation", "before": 90, "after": 90, "source": "Track1:MatrixStructure", "status": "same"}
      ],
      "unexpected_losses": [],
      "expected_losses": [{"tag": "Track2:AudioFormat", "before": "mp4a", "reason": "réencodage"}],
      "added": [{"tag": "ItemList:Encoder", "after": "Lavf63.1.102"}],
      "before": {"QuickTime:CreateDate": "2024:06:01 08:00:00", "...": "..."},
      "after": {"QuickTime:CreateDate": "2024:06:01 08:00:00", "...": "..."}
    }
  }
}
```

`input`, `output`, `duration_seconds`, `elapsed_seconds`, `preset` and `crf` are the fields every video command gives, with the same values for the same video (see [the common contract](../README.md#the-video-commands-common-contract)).

- **`format`**: the container, detected on the content: `mp4`, `mov`, `3gp`, `matroska`, `webm`, `avi`, `mpegts`, else the name of ffmpeg's demuxer (`flv`, `ogg`, `mpeg`...) for the input, as `details.detected_format` names it; always `mp4` for the output.
- **`mtime`**: ISO 8601 in UTC; the output's is the input's.
- **`duration_seconds`**: the video's duration (`null` when ffprobe can't read it); **`elapsed_seconds`**: the time the export took.
- **`preset`**, **`crf`**: the preset used and the CRF it was encoded with, the preset's own or `--crf`.
- **`metadata`**: the [metadata diff](#metadata-diff) of the original and the output.

## Writing and interruption

The output is written as every video command writes it (see [display-copy](display-copy.md#atomic-writing)): ffmpeg encodes into an anonymous file (`O_TMPFILE`) of the output's folder, exiftool writes it again with the metadata into a second one, which gets the original's dates (atime and mtime), then the output's name. Until then neither file has a name: a reader never sees a half-written output, and an export stopped at any point, even by SIGKILL (`kill -9`, the OOM killer), leaves neither a partial output nor a temporary file. Both files take room in the output's folder at once: about twice the output's size, until the export ends. Without `--overwrite`, a file that appeared at the output during the export is kept: the export fails with `output_exists` (the name is given by a hard link, which can't replace a file). On a file system without `O_TMPFILE`, a hidden named temporary file (`.<random>.tmp`) stands for the output's, removed on any error or signal it can catch, but left behind by a SIGKILL.

An export that fails, or is stopped by SIGTERM, SIGINT (Ctrl+C) or SIGHUP from the moment it starts its checks until the output takes its name, kills ffmpeg or exiftool, removes the output's folders it created (if still empty), and exits with `interrupted` (130) for a signal. A SIGHUP already ignored (`nohup`) stays ignored. Only the first signal interrupts: the next ones are ignored, so that the cleanup runs to the end. From the moment the output takes its name until the process exits, the signals are ignored: the export is done and reported as such (exit 0), never `interrupted` with a complete output on disk. Once the JSON document is written, a signal never changes the exit code it announces (0, 130 or the error's code). ffmpeg stops with compression-suite, even after a SIGKILL; the output's folders it created then stay, empty. A signal while the process is still starting (Python loading the command, about the first 0.2 to 0.3 s) ends it by that signal (exit 128 + the signal's number, 143 for SIGTERM), with no JSON on stdout and nothing written.

Paths are given to ffmpeg and ffprobe with the `file:` protocol: a file named `2024-06-01T10:30:00.mp4` is a file, not a protocol.

## Errors

Every check that can fail without encoding runs first, in this order: preset and CRF, pinned tools, input, output being the input, format, output. The errors of the original (`unsupported_format`, `input_corrupt`) have the same message and details as in `display-copy` and `preview-portion`.

| Code | Exit | When |
|---|---|---|
| `usage_error` | 2 | invalid options, unknown preset (`details.preset`, `known_presets`), CRF out of bounds (`details.crf`, `crf_min`, `crf_max`), output that is the input itself or ends with a slash |
| `unsupported_format` | 21 | the reason in `message`, `details.detected_format`: an image (`jpeg`...); not a video at all (`unknown`); a container without a video stream (audio only, `detected_format` the container); a video with more than one video stream (`holds 2 video streams, only one is supported`): the export would lose the others |
| `input_not_found` | 23 | input missing, or not a file |
| `output_exists` | 24 | output exists without `--overwrite` (before the export, or appeared during it), is a symbolic link to a missing file without `--overwrite`, or is a folder |
| `metadata_copy_failed` | 25 | exiftool failed to copy the metadata back |
| `input_unreadable` | 26 | input present but unreadable |
| `input_corrupt` | 27 | a video that can't be decoded: as the display copy probes it (cut before its index, first frame or end missing: a phone video cut short, a Matroska or WebM file whose end is gone), or any error of the original's demuxer or decoders met while encoding (damaged data in the middle): `details.detected_format`, the reason in `message`, nothing written |
| `tool_missing` | 30 | pinned ffmpeg, ffprobe or exiftool not installed, not runnable, or of another version than the manifest's |
| `internal_error` | 50 | ffmpeg failed without an error of the original's demuxer or decoders (`message` gives its last messages: disk full, for instance), or any other error |
| `interrupted` | 130 | stopped by SIGINT (Ctrl+C), SIGTERM or SIGHUP from the start of the checks until the output took its name |

A damaged video is told from a failure of the encoder by ffmpeg's error output: ffmpeg stops at the first decoding error (`-xerror`) rather than encode a video cut short, and any line of its error output that comes from the original's demuxer, streams or decoders makes it damaged, even when ffmpeg succeeds, as in the display copy and the preview portions.

## Known limits

- **A truncated MPEG-TS may pass**: MPEG-TS has no index and declares no duration. A file cut in the middle of a packet is `input_corrupt`, but one cut exactly between the packets of each stream is a shorter, valid stream: it exports as a shorter video.
