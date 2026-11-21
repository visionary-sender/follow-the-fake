#!/bin/sh
# Container entry point. With arguments it is plain `ftf <args>`. Without arguments it starts the
# demo: build the campaign base on first start (only if /base has none and a dataset folder is
# mounted at /data/raw), then serve the app on 0.0.0.0:8501 inside the container (publish it on
# the host's loopback only: -p 127.0.0.1:8501:8501). All pipeline logic lives in `ftf build-base`.
set -eu

if [ "$#" -gt 0 ]; then
    exec ftf "$@"
fi

base_dir=${FTF_BASE_DIR:-/base}
raw_dir=${FTF_RAW_ROOT:-/data/raw}
port=${FTF_PORT:-8501}

if [ ! -f "$base_dir/dataset.json" ] && [ -d "$raw_dir" ] && [ -n "$(ls -A "$raw_dir" 2>/dev/null)" ]; then
    echo "ftf: no campaign base in $base_dir; building it from $raw_dir (first start only)" >&2
    ftf build-base --raw "$raw_dir" --out "$base_dir"
fi

if [ -f "$base_dir/dataset.json" ]; then
    export FTF_BASE="$base_dir/dataset.json"
    if [ -z "${FTF_ENRICH_CACHE:-}" ] && [ -f "$base_dir/enrichment/cache.json" ]; then
        export FTF_ENRICH_CACHE="$base_dir/enrichment/cache.json"
    fi
else
    echo "ftf: no campaign base ($base_dir/dataset.json) and no dataset folder at $raw_dir; the app shows the built-in fixture if one is installed" >&2
fi

exec ftf serve --address 0.0.0.0 --port "$port"
