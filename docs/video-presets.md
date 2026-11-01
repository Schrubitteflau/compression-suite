# video-presets

List the named video presets. They are defined in compression-suite only, in one place shared by every command that encodes a video ([`compress-video`](compress-video.md) today): a caller (WelcomeApp) shows them from this list and never hard-codes one.

Every preset keeps the definition and the frame rate of the original; only an odd width or height loses its last column or row, which x265 can't encode in 4:2:0. The output is an MP4: HEVC video (x265, tagged `hvc1`) in 4:2:0, which players and galleries read, with Opus audio or no audio at all. An 8-bit source (RGB and 4:4:4 screen recordings included) gives 8-bit Main, a 10-bit one (HDR, HLG) 10-bit Main 10 with its colour tags. A command that encodes takes a preset id and, optionally, another CRF within the bounds given here.

It runs no external tool and needs no API key.

## Usage

```bash
# Readable list on stdout
compression_suite video-presets

# Machine-readable output (one JSON document on stdout)
compression_suite video-presets --json
```

## Options

| Option | Default | Description |
|---|---|---|
| `--json` | `false` | Write a single JSON document on stdout |

## JSON output (`--json`)

```json
{
  "schema_version": 2,
  "ok": true,
  "command": "video-presets",
  "result": {
    "default_preset": "default",
    "presets": [
      {
        "id": "default",
        "label": "Défaut",
        "container": "mp4",
        "video": {"encoder": "x265", "speed": "slow", "crf": 32},
        "crf_bounds": {"min": 0, "max": 51},
        "audio": {"codec": "opus", "bitrate_kbps": 32}
      },
      {
        "id": "screen",
        "label": "Écran / cours",
        "container": "mp4",
        "video": {"encoder": "x265", "speed": "slow", "crf": 35},
        "crf_bounds": {"min": 0, "max": 51},
        "audio": {"codec": "opus", "bitrate_kbps": 32}
      },
      {
        "id": "dance",
        "label": "Danse",
        "container": "mp4",
        "video": {"encoder": "x265", "speed": "slow", "crf": 45},
        "crf_bounds": {"min": 0, "max": 51},
        "audio": null
      }
    ]
  }
}
```

- **`default_preset`**: the id of the preset to select when none is chosen.
- **`presets`**: in display order. `id` is stable: it is what a command that encodes takes. `label` is for display, in French, and may change.
- **`video`**: `encoder` (`x265`), `speed` (the x265 preset, `slow`) and `crf`, the CRF used when the caller gives none. A lower CRF keeps more quality and makes a larger file.
- **`crf_bounds`**: the integer CRF values a command that encodes accepts in place of the preset's own, bounds included.
- **`audio`**: `codec` (`opus`) and `bitrate_kbps`, or `null` when the output keeps no audio track.

A command that encodes, given an unknown preset id or a CRF outside `crf_bounds`, fails with `usage_error` (exit 2) and writes nothing. The CRF must be a plain integer (not a boolean, a decimal or a string). Its `details` give `preset` and `known_presets`, or `crf`, `crf_min` and `crf_max` (`crf` is then the value given, as text when it is not an integer).

## Errors

| Code | Exit | When |
|---|---|---|
| `usage_error` | 2 | invalid options |
