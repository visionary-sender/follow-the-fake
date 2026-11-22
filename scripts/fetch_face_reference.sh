#!/bin/sh
# Fill docker/build-context/faces-reference/ with the face reference set (manifest.json, images/,
# embeddings.npy, build-report.json, HACK-83) so the jury image can bake it as-is (Dockerfile; the
# image reads it from FTF_FACE_REFERENCE). Runs only
# on the workbench VM: it refuses anywhere else, so the set never reaches the laptop. The folder is
# gitignored. An empty or missing bucket folder is not an error: the folder stays empty and the
# image builds; the face family then embeds faces but names no one.
#
#   FTF_FACES_REFERENCE_URI=gs://... scripts/fetch_face_reference.sh   (default: outputs/visual/face-reference)
set -eu
if [ "$(uname -s)" != Linux ]; then
  echo "fetch_face_reference: run this on the workbench VM, not on this machine" >&2
  exit 2
fi
host=$(hostname 2>/dev/null || echo unknown)
case "$host" in
  ftf-workbench|ftf-workbench-*) ;;
  *) echo "fetch_face_reference: runs only on ftf-workbench VMs (this host is $host)" >&2; exit 2 ;;
esac
uri=${FTF_FACES_REFERENCE_URI:-gs://follow-the-fake-data/outputs/visual/face-reference}
dest=${1:-docker/build-context/faces-reference}
mkdir -p "$dest"
# Only "not found" or an empty folder continues with a warning. Auth, permission or network
# errors stop the script, so a broken fetch never produces an image without the set unnoticed.
if ! err=$(gcloud storage ls "$uri/" 2>&1 >/dev/null); then
  case "$err" in
    *"matched no objects"*|*"HTTPError 404"*|*NotFound*)
      echo "fetch_face_reference: nothing at $uri; image builds without a reference set" >&2
      exit 0 ;;
    *)
      echo "fetch_face_reference: gcloud storage ls failed for $uri: $err" >&2
      exit 1 ;;
  esac
fi
gcloud storage rsync -r "$uri" "$dest" >/dev/null
for f in manifest.json embeddings.npy; do
  if [ ! -s "$dest/$f" ]; then
    echo "fetch_face_reference: $f missing in $uri; image builds without a reference set" >&2
    exit 0
  fi
done
echo "fetch_face_reference: ok ($(du -sk "$dest" | cut -f1) KB)"
