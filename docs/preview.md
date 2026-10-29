# preview

Convert a HEIC or HEIF to a JPEG, for display in a browser only. The HEIC itself is only read: its bytes and its modification date never change, and it is the file that gets stored.

The JPEG is at full resolution, the orientation is applied to its pixels (it carries no EXIF, so no viewer rotates it a second time) and the ICC profile of the HEIC is embedded (Display P3 for an iPhone photo). Quality: 90.

Decoding goes through `pillow-heif`, pinned by the lockfile, never through Ubuntu's `heif-convert`. It needs neither exiftool nor a Tinify key.

## Usage

```bash
# Readable report on stderr
compression_suite preview IMG_0001.HEIC -o /tmp/IMG_0001.jpg

# Machine-readable output (one JSON document on stdout)
compression_suite preview IMG_0001.HEIC -o /tmp/IMG_0001.jpg --json
```

## Options

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | *(required)* | Path to the HEIC or HEIF file (never modified) |
| `--output`, `-o` | *(required)* | Path to the JPEG preview to write (its folder is created if needed) |
| `--overwrite` | `false` | Overwrite output file if it exists |
| `--json` | `false` | Write a single JSON document on stdout |

## JSON output (`--json`)

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "preview",
  "result": {
    "input": "IMG_0001.HEIC",
    "output": "/tmp/IMG_0001.jpg",
    "width": 3024,
    "height": 4032,
    "size_bytes": 1562625
  }
}
```

- **`width`**, **`height`**: those of the JPEG, orientation applied: a portrait stays a portrait.
- **`size_bytes`**: the size of the JPEG.

The JPEG is written atomically: a reader never sees it half-written.

## Errors

| Code | Exit | When |
|---|---|---|
| `unsupported_format` | 21 | format detected on the content other than HEIC or HEIF (`details.detected_format`); a `.jpg` that is really a HEIC is converted |
| `input_not_found` | 23 | input missing, or not a file |
| `input_unreadable` | 26 | input present but unreadable |
| `output_exists` | 24 | output exists without `--overwrite`, or is a folder |
| `usage_error` | 2 | invalid options, `--output` missing |

## HDR photos

An Apple HDR photo (an auxiliary image `urn:com:apple:photo:2020:aux:hdrgainmap`) gives its SDR base image: the gain map is ignored, like every auxiliary image (depth, mattes). A 10 or 12-bit image is brought down to 8 bits.

**Known limit**: no automated test covers it. pillow-heif can't write an auxiliary image, so no HEIC with a gain map can be fabricated, and real ones (photos of other people, with GPS) can't be versioned. It was checked by hand on real iPhone HEICs with a gain map, among them "Rotate 90 CW", "Mirror vertical" and "Mirror horizontal and rotate 270 CW", with Display P3, Apple Wide Color Sharing or no profile: each one converted at full resolution, upright, with its profile, and left unchanged. None of them is tagged with a PQ curve (`exiftool -if '$ProfileDescription =~ /PQ/'` finds nothing in the sources): that case was not checked.

## Other limits

- **Colours described by `nclx` only**: a HEIC without an ICC profile gives a JPEG without one, which a browser shows as sRGB. The real iPhone HEIC without a profile at hand has an `nclx` box with unspecified primaries, so sRGB is the right reading. A HEIC whose `nclx` box declares other primaries (P3, BT.2020) would lose them: none was found in the sources.
- **Damaged HEIC**: a file detected as HEIC that libheif can't decode (truncated, for instance) fails with `internal_error` (exit 50), the decoder's message in `message`.
