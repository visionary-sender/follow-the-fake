#!/bin/sh
# Lists the Debian packages that the image's Chromium step adds (`playwright install --with-deps`),
# with the licence their copyright file records (HACK-68). Run on the VM, after building the image:
#   scripts/vm.sh run -- sh scripts/chromium_packages.sh ftf:dev > chromium-rows.tsv
# Output: `package<TAB>licence` rows. Compare with the committed list:
#   scripts/vm.sh run -- python3 -I scripts/licences.py --audit-image chromium-rows.tsv
# Nothing is printed from the image beyond package names and licence names.
set -eu
image=${1:?usage: chromium_packages.sh IMAGE}
base=${FTF_BASE_IMAGE:-python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
dpkg_names="dpkg-query -W -f='\${Package}\n'"
sudo docker run --rm --entrypoint sh "$base" -c "$dpkg_names" > "$work/base.raw"
sudo docker run --rm --entrypoint sh "$image" -c "$dpkg_names" > "$work/image.raw"
# Each listing must be non-empty: an empty base list would make the whole image look "added".
for f in base image; do
  [ -s "$work/$f.raw" ] || { echo "chromium_packages.sh: empty package list for $f" >&2; exit 1; }
  sort "$work/$f.raw" > "$work/$f.txt"
done
comm -13 "$work/base.txt" "$work/image.txt" > "$work/added.txt"
echo "packages added by the Chromium step: $(wc -l < "$work/added.txt" | tr -d ' ')" >&2
sudo docker run --rm -i --entrypoint python3 -v "$work/added.txt:/added.txt:ro" "$image" -I - \
  < "$(dirname "$0")/chromium_packages.py"
