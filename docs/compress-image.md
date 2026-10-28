# compress-image

Compress images via the [Tinify API](https://tinypng.com/developers) with selective metadata preservation.

Supports JPEG, PNG, WebP, and AVIF — any format accepted by Tinify. The API accepts files up to 500MB (the 5MB limit only applies to the web interface).

## Prerequisites

- **exiftool** >= 11.88 (major version 11): required for metadata preservation (`--metadata=keep`)
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

After Tinify compression, metadata from the original image is copied back to the compressed file using exiftool. A blacklist approach is used — all tags are copied except those that become invalid after compression:

- **ThumbnailImage, ThumbnailOffset, ThumbnailLength**: reference the original image, stale after compression
- **Compression**: describes the original encoding (e.g., Baseline DCT), the compressed file uses Progressive DCT

This preserves camera info (Make, Model, ISO, Exposure, etc.), dates, GPS, orientation, and all other tags.

### `--metadata=strip`

The Tinify API output is used as-is, with no metadata preservation. Only file-level attributes remain (dimensions, encoding process, color space).

## I/O design

- Without `--json`, **stdout is reserved for binary image data** (when `--output` is omitted) — all reporting goes to stderr
- Compression stats (size before/after, ratio, API compression count) are always printed to stderr
- Compatible with Unix pipes: `cat input.jpg | compress-image --api-key=KEY | other-tool`
- The format is detected from the file content, never from its extension
- The output is written atomically: a temporary file in the destination folder, then a rename
- The output is written even when it is larger than the original
- Every check that can fail without Tinify (input, format, size, existing output) runs before the API call, so no credit is wasted

## JSON output (`--json`)

With `--json`, stdout only contains one JSON document (`schema_version` is bumped on any incompatible change):

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "compress-image",
  "result": {
    "input": {"path": "photo.jpg", "format": "jpeg", "size_bytes": 2481152},
    "output": {"path": "compressed.jpg", "format": "jpeg", "size_bytes": 812345},
    "tinify": {"compressions_this_month": 42}
  }
}
```

`input.path` is `null` when reading from stdin. On failure:

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
| `usage_error` | 2 | Invalid options (e.g. `--json` without `--output`) | |
| `tinify_unavailable` | 10 | Tinify server error or network failure | |
| `api_key_invalid` | 11 | Key rejected, Tinify client error | |
| `quota_exceeded` | 12 | Monthly API quota exceeded | |
| `unsupported_format` | 21 | Detected format is not JPEG, PNG, WebP or AVIF | `detected_format` |
| `file_too_large` | 22 | Input > 15MB without `--disable-hard-limit` | `size_bytes`, `limit_bytes` |
| `input_not_found` | 23 | Input missing or not a file | |
| `output_exists` | 24 | Output exists without `--overwrite` | |
| `metadata_copy_failed` | 25 | exiftool failed to copy metadata back | |
| `internal_error` | 50 | Any other error | |
| `interrupted` | 130 | Interrupted by user (Ctrl+C) | |

## Composability

`compress-image` can be combined with `reduce-jpeg-size` via pipes:

```bash
# First reduce to 5MB, then compress via API
cat large.jpg | compression_suite reduce-jpeg-size --max-size=4999 \
  | compression_suite compress-image --api-key=YOUR_KEY --metadata=strip > final.jpg
```
