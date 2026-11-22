#!/bin/sh
# Fetch the brand stop-list (HACK-75) into the Docker build context. Run it on the workbench VM
# before `docker build`: the bucket is reachable there, the file is built from our public corpus
# and it never enters git (docker/brand-stoplist/*.json is gitignored).
#   scripts/fetch-brand-stoplist.sh            # from the bucket into docker/brand-stoplist/
# Without the file the image still builds; the image then warns at run time (see docs/container.md).
set -eu
src=${FTF_BRAND_STOPLIST_SRC:-gs://follow-the-fake-data/outputs/linking/brand-stoplist/brand-stoplist.json}
dest=$(dirname "$0")/../docker/brand-stoplist
mkdir -p "$dest"
gcloud storage cp "$src" "$dest/brand-stoplist.json" >&2
echo "fetch-brand-stoplist: wrote docker/brand-stoplist/brand-stoplist.json" >&2
