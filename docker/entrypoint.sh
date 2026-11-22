#!/bin/sh
# Container entry point. With arguments it runs `ftf <args>`, with the base and package settings
# below. Without arguments it starts the demo: build the campaign base on first start (only if no
# base is found and a dataset folder is mounted at /data/raw), then serve the app on 0.0.0.0:8501
# inside the container (publish it on the host's loopback only: -p 127.0.0.1:8501:8501). All
# pipeline logic lives in `ftf build-base`.
# FTF_PACKAGE=<dir> points at the offline package (docs/container.md); each setting it provides is
# used only when that setting is unset, so an explicit -e value always wins.
set -eu

package=${FTF_PACKAGE:-}
package_cache=
if [ -n "$package" ] && [ ! -d "$package" ]; then
    echo "ftf: FTF_PACKAGE=$package is not a directory; the offline package is not used" >&2
elif [ -n "$package" ]; then
    if [ -z "${FTF_BASE:-}" ] && [ -f "$package/base/dataset.json" ]; then
        FTF_BASE=$package/base/dataset.json
    fi
    if [ -z "${FTF_MODEL_DIR:-}" ] && [ -d "$package/models" ]; then
        FTF_MODEL_DIR=$package/models
    fi
    if [ -z "${FTF_FACE_REFERENCE:-}" ] && [ -d "$package/face-reference" ]; then
        FTF_FACE_REFERENCE=$package/face-reference
    fi
    if [ -z "${FTF_BRAND_STOPLIST:-}" ] && [ -f "$package/brand-stoplist/brand-stoplist.json" ]; then
        FTF_BRAND_STOPLIST=$package/brand-stoplist/brand-stoplist.json
    fi
    if [ -f "$package/enrichment/cache.json" ]; then
        package_cache=$package/enrichment/cache.json
    fi
    export FTF_BASE FTF_MODEL_DIR FTF_FACE_REFERENCE FTF_BRAND_STOPLIST
fi

# Online lookups update the enrichment cache, and /base and the package may be read-only, so the
# app and the CLI work on a copy in a fresh directory under /tmp.
copy_cache() {
    cache_dir=$(mktemp -d /tmp/ftf-enrichment.XXXXXX)
    cp "$1" "$cache_dir/cache.json"
    FTF_ENRICH_CACHE=$cache_dir/cache.json
    export FTF_ENRICH_CACHE
}

if [ -z "${FTF_ENRICH_CACHE:-}" ] && [ -n "$package_cache" ]; then
    copy_cache "$package_cache"
fi

if [ "$#" -gt 0 ]; then
    # Commands such as `ftf analyze` use the base in /base when one is there, as the app does.
    if [ -z "${FTF_BASE:-}" ] && [ -f "${FTF_BASE_DIR:-/base}/dataset.json" ]; then
        FTF_BASE="${FTF_BASE_DIR:-/base}/dataset.json"
        export FTF_BASE
    fi
    exec ftf "$@"
fi

# The documented --tmpfs /home/ftf needs uid=10001,gid=10001; without it the home is not writable,
# so fall back to a private directory under /tmp (Streamlit and tldextract write to $HOME).
if [ ! -w "${HOME:-/nonexistent}" ]; then
    HOME=$(mktemp -d /tmp/ftf-home.XXXXXX)
    export HOME
fi
cd "$HOME"

base_dir=${FTF_BASE_DIR:-/base}
raw_dir=${FTF_RAW_ROOT:-/data/raw}
port=${FTF_PORT:-8501}

if [ -z "${FTF_BASE:-}" ] && [ ! -f "$base_dir/dataset.json" ] && [ -d "$raw_dir" ] && [ -n "$(ls -A "$raw_dir" 2>/dev/null)" ]; then
    echo "ftf: no campaign base in $base_dir; building it from $raw_dir (first start only)" >&2
    ftf build-base --raw "$raw_dir" --out "$base_dir"
fi

base_from_dir=0
if [ -z "${FTF_BASE:-}" ] && [ -f "$base_dir/dataset.json" ]; then
    FTF_BASE="$base_dir/dataset.json"
    base_from_dir=1
fi

if [ -n "${FTF_BASE:-}" ]; then
    export FTF_BASE
    # The /base cache belongs to the /base base: an explicit or package base does not take it.
    if [ "$base_from_dir" = 1 ] && [ -z "${FTF_ENRICH_CACHE:-}" ] && [ -f "$base_dir/enrichment/cache.json" ]; then
        copy_cache "$base_dir/enrichment/cache.json"
    fi
else
    echo "ftf: no campaign base ($base_dir/dataset.json) and no dataset folder at $raw_dir; the app shows the built-in fixture if one is installed" >&2
fi

exec ftf serve --address 0.0.0.0 --port "$port"
