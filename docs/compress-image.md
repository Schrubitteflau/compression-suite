# compress-image

Compress images via the [Tinify API](https://tinypng.com/developers) with selective metadata preservation.

Supports JPEG, PNG, WebP, and AVIF — any format accepted by Tinify. The API accepts files up to 500MB (the 5MB limit only applies to the web interface).

## Prerequisites

- **exiftool** >= 11.88 (major version 11): required for metadata preservation (`--metadata=keep`)
- **jpegoptim** >= 1.4.0 (major version 1): checked at startup alongside exiftool
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

- **stdout is reserved for binary image data** — all reporting goes to stderr
- Compression stats (size before/after, ratio, API compression count) are always printed to stderr
- Compatible with Unix pipes: `cat input.jpg | compress-image --api-key=KEY | other-tool`

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 10 | Network/connection error or Tinify server error |
| 11 | API client error (bad request, invalid key) |
| 12 | API quota exceeded |
| 50 | Other error (file not found, validation, etc.) |
| 130 | Interrupted by user (Ctrl+C) |

## Composability

`compress-image` can be combined with `reduce-jpeg-size` via pipes:

```bash
# First reduce to 5MB, then compress via API
cat large.jpg | compression_suite reduce-jpeg-size --max-size=4999 \
  | compression_suite compress-image --api-key=YOUR_KEY --metadata=strip > final.jpg
```
