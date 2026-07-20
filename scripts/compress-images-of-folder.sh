images_path="${1:-}"
api_key="${2:-}"

if [ -z "$images_path" ] || [ -z "$api_key" ]; then
    echo "Usage: $0 <images-path> <api-key>" >&2
    exit 1
fi

if [ ! -d "$images_path" ]; then
    echo "Images path is not a directory: $images_path" >&2
    exit 1
fi

for f in "$images_path"/*; do uv run compression_suite compress-image "$f" --output "$f" --overwrite --api-key="$api_key"; done
