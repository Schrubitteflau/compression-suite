# reduce-jpeg-size

Reduce JPEG file size using [jpegoptim](https://github.com/tjko/jpegoptim) with iterative compression.

An autonomous module that works as a standalone tool or as part of a pipeline with other compression-suite commands.

## Prerequisites

- **jpegoptim** >= 1.4.0 (major version 1): must be installed and available in PATH
  - Linux: `sudo apt install jpegoptim` (Ubuntu/Debian)
  - macOS: `brew install jpegoptim`

## Usage

```bash
# File to file
compression_suite reduce-jpeg-size photo.jpg --output reduced.jpg

# Custom target size (in KB)
compression_suite reduce-jpeg-size photo.jpg --output reduced.jpg --max-size=4000

# stdin to stdout (for piping)
cat photo.jpg | compression_suite reduce-jpeg-size --max-size=3000 > reduced.jpg

# Limit iterations
compression_suite reduce-jpeg-size photo.jpg --output reduced.jpg --max-size=2000 --max-iterations=5
```

## Options

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | stdin | Path to input JPEG file (omit to read from stdin) |
| `--output`, `-o` | stdout | Path to output file (omit to write to stdout) |
| `--max-size` | `4999` | Target maximum size in KB |
| `--max-iterations` | `10` | Maximum number of compression iterations |
| `--overwrite` | `false` | Overwrite output file if it exists |
| `--verbose`, `-v` | `false` | Enable verbose logging |

## How it works

1. **First pass**: `jpegoptim --size=<max_size>` targets the desired size directly
2. **Escalating passes**: if still over the target, the module tries progressively larger reductions:
   - Start at `--size=98%` (2% reduction)
   - If no progress, escalate to `--size=97%`, then `96%`, `95%`, etc.
   - As soon as progress is made, reset to `--size=99%` (1% reduction) for the next iteration
   - This ensures convergence even when jpegoptim can't achieve a small reduction in one step
3. **Stop conditions**: the loop stops when the target size is reached, `--max-iterations` is hit, or escalation reaches 50% without progress

jpegoptim works in-place, so the module uses a temporary directory for the working file regardless of the I/O mode (file or pipe).

## I/O design

- **stdout is reserved for binary image data** — all reporting goes to stderr
- Compression stats (size before/after, ratio, number of iterations) are printed to stderr
- Compatible with Unix pipes

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 50 | Error (file not found, validation, jpegoptim failure, etc.) |
| 130 | Interrupted by user (Ctrl+C) |

## Composability

```bash
# Reduce then compress via API
cat large.jpg | compression_suite reduce-jpeg-size --max-size=4999 \
  | compression_suite compress-image --api-key=YOUR_KEY --metadata=strip > final.jpg
```

## Limitations

- **JPEG only**: jpegoptim only supports JPEG files
- **Lossy**: reducing size below the original quality level is lossy and irreversible
- **Target may not be achievable**: jpegoptim cannot always reach the exact target size, especially for already-compressed images. The iterative loop mitigates this but has diminishing returns
