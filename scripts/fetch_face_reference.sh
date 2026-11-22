#!/bin/sh
# Fill docker/build-context/faces-reference/ with the face reference set (embeddings.npy, rows.json)
# so the jury image can bake it (Dockerfile; the image reads it from FTF_FACES_REFERENCE). Runs only
# on the workbench VM: it refuses anywhere else, so the set never reaches the laptop. The folder is
# gitignored. An empty or missing bucket folder is not an error: the folder stays empty and the
# image builds; the face family then embeds faces but names no one.
#
#   FTF_FACES_REFERENCE_URI=gs://... scripts/fetch_face_reference.sh
set -eu
host=$(hostname 2>/dev/null || echo unknown)
if [ "$host" != ftf-workbench ]; then
  echo "fetch_face_reference: runs only on ftf-workbench (this host is $host)" >&2
  exit 2
fi
uri=${FTF_FACES_REFERENCE_URI:-gs://follow-the-fake-data/outputs/visual/face-reference}
dest=${1:-docker/build-context/faces-reference}
mkdir -p "$dest"
if ! gcloud storage ls "$uri/" >/dev/null 2>&1; then
  echo "fetch_face_reference: nothing at $uri; image builds without a reference set" >&2
  exit 0
fi
gcloud storage rsync -r "$uri" "$dest" >/dev/null
for f in embeddings.npy rows.json; do
  if [ ! -s "$dest/$f" ]; then
    echo "fetch_face_reference: $f missing in $uri; image builds without a reference set" >&2
    exit 0
  fi
done
echo "fetch_face_reference: ok ($(du -sk "$dest" | cut -f1) KB)"
