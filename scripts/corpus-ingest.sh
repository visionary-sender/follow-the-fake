#!/usr/bin/env bash
# Run `ftf ingest` over a corpus batch's fetcher folders and tag the result.
# Runs on the workbench, inside the synced checkout:  scripts/vm.sh run -- bash scripts/corpus-ingest.sh batch-0
# Re-run whenever new indicator extractors merge: it overwrites <batch>/ingested/ and re-syncs it.
set -euo pipefail
batch=${1:?usage: corpus-ingest.sh <batch name>}
case "$batch" in *[!A-Za-z0-9._-]*|.*) echo "bad batch name" >&2; exit 2 ;; esac
D=/data/derived/corpus/$batch
[ -f "$D/manifest.jsonl" ] || { echo "no $D/manifest.jsonl; run ftf corpus build first" >&2; exit 1; }
inputs=()
# scanner-v1: batch 0's scanner items in the v1 layout (ftf corpus convert-scanner). All folders go
# in ONE call, so ftf ingest can merge a victim-profile re-fetch with its default fetch.
for d in "$D"/scanner-v1 "$D"/fetch-* "$D"/picked-*; do  # fetch-* includes fetch-victim-* and fetch-egress-*
  [ -d "$d" ] && inputs+=("${d#"$D"/}")
done
[ ${#inputs[@]} -gt 0 ] || { echo "no fetch-*/picked-*/scanner-v1 folders under $D" >&2; exit 1; }
rm -rf "$D/ingested"
mkdir -p "$D/ingested"
cd "$D"
ftf ingest "${inputs[@]}" --root . --out ingested/dataset.json --timing ingested/timing.json
# Label lists live in the bucket (gs://$FTF_BUCKET/outputs/corpus/lists/), never in git.
L=/data/derived/corpus/lists
mkdir -p "$L"
for f in legit-domains.txt person-keywords.txt certpl-hosts.txt; do
  gcloud storage cp "gs://$FTF_BUCKET/outputs/corpus/lists/$f" "$L/$f" >/dev/null \
    || { echo "label list $f missing from the bucket (outputs/corpus/lists/)" >&2; exit 1; }
done
lists=(--legit-list $L/legit-domains.txt --person-keywords $L/person-keywords.txt --listed-domains $L/certpl-hosts.txt)
ftf corpus tag-ingested --dataset ingested/dataset.json --manifest manifest.jsonl --out ingested/corpus_kind.jsonl "${lists[@]}" \
  | tee ingested/summary.json
gcloud storage rsync -r --delete-unmatched-destination-objects "$D/ingested/" "gs://$FTF_BUCKET/outputs/corpus/$batch/ingested/" 2>&1 | tail -1
