#!/bin/sh
# Fetch the brand stop-list (HACK-75) into the Docker build context. Run it on the workbench VM
# (Linux) before `docker build`: the bucket is reachable there, the file is built from our public
# corpus and it never enters git (docker/brand-stoplist/*.json is gitignored). Refuses to run
# anywhere else, so it never puts a bucket file on a laptop.
#   scripts/fetch-brand-stoplist.sh            # bucket -> docker/brand-stoplist/brand-stoplist.json
# Without the file the image still builds; the image then warns at run time (see docs/container.md).
set -eu
if [ "$(uname -s)" != Linux ]; then
    echo "fetch-brand-stoplist: run this on the workbench VM, not on this machine" >&2
    exit 2
fi
export CLOUDSDK_ACTIVE_CONFIG_NAME=${CLOUDSDK_ACTIVE_CONFIG_NAME:-follow-the-fake}
src=${FTF_STOPLIST_GCS_URI:-gs://follow-the-fake-data/outputs/linking/brand-stoplist/brand-stoplist.json}
dest=$(dirname "$0")/../docker/brand-stoplist
mkdir -p "$dest"
tmp=$dest/.brand-stoplist.json.part
gcloud storage cp "$src" "$tmp" >&2
mv "$tmp" "$dest/brand-stoplist.json"
echo "fetch-brand-stoplist: wrote docker/brand-stoplist/brand-stoplist.json" >&2
