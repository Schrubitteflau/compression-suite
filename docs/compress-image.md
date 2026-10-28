# compress-image

Compress images via the [Tinify API](https://tinypng.com/developers) with selective metadata preservation.

Supports JPEG, PNG, WebP, and AVIF — any format accepted by Tinify. The API accepts files up to 500MB (the 5MB limit only applies to the web interface).

## Prerequisites

- **Pinned exiftool**: installed in the project by `just install-tools`, at the version of `pinned-tools.json`. The exiftool found in `PATH` (e.g. `/usr/bin/exiftool`) is never used
- **Perl**: needed to run exiftool
- **Tinify API key**: get one at https://tinypng.com/developers (500 free compressions/month)

## Usage

```bash
# File to file, preserving metadata (default)
compression_suite compress-image photo.jpg --output compressed.jpg --api-key=YOUR_KEY

# Strip metadata
compression_suite compress-image photo.jpg --output stripped.jpg --api-key=YOUR_KEY --metadata=strip

# stdin to stdout (for piping)
cat photo.jpg | compression_suite compress-image --api-key=YOUR_KEY --metadata=strip > compressed.jpg

# Override the 15MB safety limit
compression_suite compress-image huge.png --output out.png --api-key=YOUR_KEY --disable-hard-limit

# Machine-readable output (one JSON document on stdout, --output is required)
compression_suite compress-image photo.jpg --output compressed.jpg --api-key=YOUR_KEY --json
```

## Options

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | stdin | Path to input image file (omit to read from stdin) |
| `--output`, `-o` | stdout | Path to output file (omit to write to stdout) |
| `--api-key` | *(required)* | Tinify API key |
| `--metadata` | `keep` | `keep` (preserve via exiftool) or `strip` |
| `--overwrite` | `false` | Overwrite output file if it exists |
| `--disable-hard-limit` | `false` | Bypass the 15MB input size limit |
| `--json` | `false` | Write a single JSON document on stdout (requires `--output`) |
| `--verbose`, `-v` | `false` | Enable verbose logging |

## Metadata handling

### `--metadata=keep` (default)

Tinify keeps no metadata at all: no EXIF, no ICC profile, no PNG text block, no Samsung trailer. After the compression, metadata from the original image is copied back to the compressed file using exiftool. A blacklist approach is used — all tags are copied (`-all:all`, plus the ICC profile, the IFD0 dimensions and the Interop tags, which exiftool only copies when named) except those that become invalid after compression:

- **ThumbnailImage**: the EXIF thumbnail shows the original image, it is removed on purpose
- **Compression**: belongs to the thumbnail

This preserves camera info (Make, Model, ISO, Exposure, etc.), dates, GPS, orientation, the colour profile (e.g. the S23's "DCI-P3 D65") and all other tags. What can't be kept is listed in the metadata diff below.

### `--metadata=strip`

The Tinify API output is used as-is, with no metadata preservation. Only file-level attributes remain (dimensions, encoding process, color space). The metadata diff is still produced: every loss is expected, with the reason "métadonnées retirées à la demande", except a change of dimensions, which is never expected.

### Metadata diff (`--json`)

`result.metadata` compares the tags of the original and of the compressed file, read by the pinned exiftool on every group (`exiftool -j -G1 -a`, tags keyed `Group:Name`):

```json
{
  "important": [
    {"key": "capture_date", "before": "2026:09:25 20:57:02", "after": "2026:09:25 20:57:02", "source": "ExifIFD:DateTimeOriginal", "status": "same"},
    {"key": "color_profile", "before": "DCI-P3 D65 Gamut with sRGB Transfer", "after": "DCI-P3 D65 Gamut with sRGB Transfer", "source": "ICC_Profile:ProfileDescription", "status": "same"}
  ],
  "unexpected_losses": [{"tag": "PNG:ImageWidth", "kind": "changed", "before": 320, "after": 160}],
  "expected_losses": [{"tag": "Samsung:TimeStamp", "before": "2026:09:25 20:57:02.918+02:00", "reason": "zone Samsung de fin de fichier : retirée par Tinify, exiftool ne sait pas la réécrire"}],
  "added": [{"tag": "JFIF:JFIFVersion", "after": 1.01}],
  "before": {"IFD0:Make": "samsung", "...": "..."},
  "after": {"IFD0:Make": "samsung", "...": "..."}
}
```

- **Ignored groups**: `System` (file name, size, file system dates and permissions), `Composite` (values computed by exiftool) and `ExifTool` (version, warnings). They are not in `before` and `after` either.
- **`important`**: always the same fields, in this order, even when intact. `status` is `same`, `lost`, `changed` or `absent` (missing on both sides); `source` is the tag the value comes from.
  - `capture_date`: `ExifIFD:DateTimeOriginal`, else the XMP or PNG date, else the file's mtime (`source: "file_mtime"`, file to file only)
  - `gps`: `{latitude, latitude_ref, longitude, longitude_ref, altitude, altitude_ref}` (the parts present)
  - `camera`: `{make, model}`
  - `dimensions`: `{width, height}`
  - `orientation`
  - `color_profile`: description of the ICC profile, `null` if none
- **`expected_losses`** (`tag`, `before`, `reason`): losses decided by compression-suite, declared in `compress_image/main.py` next to the exiftool exclusions:
  - the EXIF thumbnail (`IFD1:*`) and its `Compression`
  - the fields of the Samsung end-of-file trailer (the `Samsung:` tags of exiftool's `Samsung::Trailer` table, read in the `SEFT` zone), which Tinify drops and exiftool can't write. Samsung's EXIF maker notes share the `Samsung` group but are copied back, so their loss stays unexpected
  - the encoding parameters Tinify chooses anew ("réencodage"): every `File` field but the dimensions, and the PNG and AVIF encoding fields (bit depth, colour type, palette…)
- **`unexpected_losses`** (`tag`, `kind`, `before`, `after`): any other tag that disappeared (`kind: "removed"`, `after: null`) or whose value changed (`kind: "changed"`). In human mode, their tags are printed on stderr. Known case: exiftool can't write an ICC profile into an AVIF, so a P3 AVIF loses it.
- **`added`** (`tag`, `after`): tags that appear after the compression, for information.
- Binary values are compared on exiftool's summary (`(Binary data N bytes…)`).

## I/O design

- Without `--json`, **stdout is reserved for binary image data** (when `--output` is omitted) — all reporting goes to stderr
- Compression stats (size before/after, ratio, API compression count) are always printed to stderr
- Compatible with Unix pipes: `cat input.jpg | compress-image --api-key=KEY | other-tool`
- The format is detected from the file content, never from its extension
- The output is written atomically: a temporary file in the destination folder, then a rename
- The output is written even when it is larger than the original
- When reading from a file, the output gets the original's atime and mtime, applied after exiftool rewrites it (with or without `--json`, whatever `--metadata`). From stdin, there is no date to carry over
- Every check that can fail without Tinify (pinned exiftool, input, format, size, existing output) runs before the API call, so no credit is wasted

## JSON output (`--json`)

With `--json`, stdout only contains one JSON document (`schema_version` is bumped on any incompatible change):

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "compress-image",
  "result": {
    "input": {"path": "photo.jpg", "format": "jpeg", "size_bytes": 2481152, "mtime": "2022-08-09T10:11:12.654321+00:00"},
    "output": {"path": "compressed.jpg", "format": "jpeg", "size_bytes": 812345, "mtime": "2022-08-09T10:11:12.654321+00:00"},
    "tinify": {"compressions_this_month": 42},
    "metadata": {"important": [], "unexpected_losses": [], "expected_losses": [], "added": [], "before": {}, "after": {}}
  }
}
```

`metadata` is the metadata diff described above. `mtime` is ISO 8601 in UTC. `input.path` and `input.mtime` are `null` when reading from stdin; `output.mtime` is then the date the output was written. On failure:

```json
{
  "schema_version": 1,
  "ok": false,
  "command": "compress-image",
  "error": {
    "code": "file_too_large",
    "message": "Input exceeds 15MB hard limit (16,000,000 bytes). Use --disable-hard-limit to override.",
    "details": {"size_bytes": 16000000, "limit_bytes": 15728640}
  }
}
```

## Error codes and exit codes

Exit codes are the same with or without `--json`.

| Error code | Exit | Meaning | `details` |
|---|---|---|---|
| — | 0 | Success | |
| `usage_error` | 2 | Invalid options (unknown flag, bad value, `--json` without `--output`) | |
| `tinify_unavailable` | 10 | Tinify server error or network failure | |
| `api_key_invalid` | 11 | Key rejected, Tinify client error | |
| `quota_exceeded` | 12 | Monthly API quota exceeded | |
| `unsupported_format` | 21 | Detected format is not JPEG, PNG, WebP or AVIF | `detected_format` |
| `file_too_large` | 22 | Input > 15MB without `--disable-hard-limit` | `size_bytes`, `limit_bytes` |
| `input_not_found` | 23 | Input missing or not a file | |
| `output_exists` | 24 | Output exists without `--overwrite`, or is a directory | |
| `metadata_copy_failed` | 25 | exiftool failed to copy metadata back | |
| `tool_missing` | 30 | Pinned exiftool not installed, not runnable, or of another version than the manifest's | `tool`, `expected_version`, `install_command` |
| `internal_error` | 50 | Any other error | |
| `interrupted` | 130 | Interrupted by user (Ctrl+C) | |

## Composability

`compress-image` can be combined with `reduce-jpeg-size` via pipes:

```bash
# First reduce to 5MB, then compress via API
cat large.jpg | compression_suite reduce-jpeg-size --max-size=4999 \
  | compression_suite compress-image --api-key=YOUR_KEY --metadata=strip > final.jpg
```
