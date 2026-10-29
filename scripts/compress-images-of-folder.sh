images_path="${1:-}"

if [ -z "$images_path" ]; then
    echo "Usage: $0 <images-path>" >&2
    echo "The Tinify key is read from ~/.config/compression-suite/config.toml (see the README)." >&2
    exit 1
fi

if [ ! -d "$images_path" ]; then
    echo "Images path is not a directory: $images_path" >&2
    exit 1
fi

for f in "$images_path"/*; do uv run compression_suite compress-image "$f" --output "$f" --overwrite; done
