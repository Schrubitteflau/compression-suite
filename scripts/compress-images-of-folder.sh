images_path=""

if [ -z "$images_path" ] || [ ! -d "$images_path" ]; then
    echo "Missing images path"
    exit 1
fi

for f in "$images_path"/*; do uv run compression_suite compress-image "$f" --output "$f" --overwrite --api-key=API_KEY; done
