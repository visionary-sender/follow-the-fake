#!/usr/bin/env bash
# Cloud dev loop: sync this worktree to the workbench VM and run things there.
#
#   scripts/vm.sh test [pytest args...]   run the test suite on the VM, return pytest's exit code
#   scripts/vm.sh run -- <cmd...>         run a command in the synced checkout, venv active
#   scripts/vm.sh licences                regenerate LICENSES-THIRD-PARTY.md (written locally; installs the features extra)
#
# Every call syncs the worktree's tracked and untracked-but-not-ignored files (never data/ or
# anything gitignored) to /work/<slug>/src on the VM, replacing the previous copy so files
# deleted locally disappear there too. The venv /work/<slug>/venv is pinned to /opt/venv-base
# versions and reinstalled only when pyproject.toml changes. <slug> is the worktree directory
# name (override with FTF_VM_SLUG), so several worktrees can use the VM at once. The venv installs
# the pip extras in FTF_VM_EXTRAS (default app,dev,fetch; the visual families need features too).
# Nothing is computed locally: the laptop only lists files, packs them and drives ssh.
# Runs on the VM always export FTF_OFFLINE=1 (analyze and enrichment stay offline there).
set -euo pipefail

VM=${FTF_VM:-workbench}
ZONE=${FTF_VM_ZONE:-europe-central2-a}
PROJECT=${FTF_VM_PROJECT:-${FTF_GCP_PROJECT:?set FTF_GCP_PROJECT or FTF_VM_PROJECT}}

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -ge 1 ] || usage
mode=$1; shift
case "$mode" in
  test|licences) ;;
  run) [ "${1:-}" = "--" ] && shift; [ $# -ge 1 ] || usage ;;
  *) usage ;;
esac

root=$(git rev-parse --show-toplevel)
cd "$root"
slug=${FTF_VM_SLUG:-$(basename "$root")}
slug=$(printf '%s' "$slug" | tr -c 'A-Za-z0-9._-' '-')
case "$slug" in ''|.|..|-*) echo "vm.sh: bad slug '$slug'" >&2; exit 2 ;; esac

# Runs on the VM as: bash -c "$remote" ftf-vm <slug> <mode> [args...]; the tarball is on stdin.
read -r -d '' remote <<'REMOTE' || true
set -euo pipefail
# FTF_OFFLINE=1 on our VMs, so analyze, enrichment and build-base make no lookups there.
# It does not guard `ftf fetch` or the scanner; live fetching stays off the workbench by rule.
export FTF_OFFLINE=1
slug=$1; mode=$2; shift 2
w=/work/$slug
mkdir -p "$w"
# Never take over a directory someone made by hand (the workbench guide also uses /work/<name>).
if [ ! -e "$w/.vm-sh" ] && [ ! -e "$w/venv/.pyproject-hash" ] && [ -n "$(ls -A "$w")" ]; then
  echo "vm.sh: $w exists and was not created by vm.sh; set FTF_VM_SLUG to another name" >&2
  exit 2
fi
touch "$w/.vm-sh"
exec 9>"$w/.lock"
flock -w 60 9 || { echo "vm.sh: $w is busy (another vm.sh call holds the lock)" >&2; exit 75; }
rm -rf "$w/src.new"
mkdir "$w/src.new"
tar --warning=no-unknown-keyword -xzf - -C "$w/src.new"
rm -rf "$w/src"
mv "$w/src.new" "$w/src"
# Rebuild the venv when the package metadata or the base venv's pins change.
extras=${FTF_VM_EXTRAS:-app,dev,fetch}
hash=$( { cat "$w/src/pyproject.toml" "$w/src/src/ftf/__init__.py"; echo "$extras torch=2.15.0+cpu"; /opt/venv-base/bin/pip freeze; } \
  | sha256sum | cut -d' ' -f1)
if [ ! -x "$w/venv/bin/python" ] || [ "$(cat "$w/venv/.pyproject-hash" 2>/dev/null)" != "$hash" ]; then
  echo "vm.sh: (re)installing venv $w/venv" >&2
  rm -rf "$w/venv"
  python3 -m venv "$w/venv"
  /opt/venv-base/bin/pip freeze --exclude-editable > "$w/base-constraints.txt"
  # CPU torch from the PyTorch index (pinned, as in the Dockerfile), so `features` pulls no CUDA wheels.
  case ",$extras," in *,features,*)
    "$w/venv/bin/pip" install -q --disable-pip-version-check \
      --extra-index-url https://download.pytorch.org/whl/cpu "torch==2.15.0+cpu" >&2 ;;
  esac
  "$w/venv/bin/pip" install -q --disable-pip-version-check \
    -c "$w/base-constraints.txt" -e "$w/src[$extras]" >&2
  echo "$hash" > "$w/venv/.pyproject-hash"
fi
cd "$w/src"
source "$w/venv/bin/activate"
case "$mode" in
  test) exec python -m pytest "$@" ;;
  licences) exec python scripts/licences.py ;;
  run) exec "$@" ;;
esac
REMOTE

files() {
  git ls-files -z -co --exclude-standard | while IFS= read -r -d '' f; do
    case "$f" in data/*) continue ;; esac
    [ -e "$f" ] || [ -L "$f" ] || continue   # tracked but deleted locally
    printf '%s\0' "$f"
  done
}

ssh_vm() {
  gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet \
    --ssh-flag=-T --command "$1"
}

# The licence table covers the `features` extra too, so `licences` installs it by default.
extras=${FTF_VM_EXTRAS:-app,dev,fetch}
if [ "$mode" = licences ]; then extras=${FTF_VM_EXTRAS:-app,dev,fetch,features}; fi
remote_cmd="FTF_VM_EXTRAS=$(printf '%q' "$extras") bash -c $(printf '%q' "$remote") ftf-vm $(printf '%q ' "$slug" "$mode" "$@")"
echo "vm.sh: $VM:/work/$slug ($mode)" >&2

if [ "$mode" = licences ]; then
  out=$(mktemp)
  trap 'rm -f "$out"' EXIT
  files | COPYFILE_DISABLE=1 tar -czf - --no-recursion --null -T - | ssh_vm "$remote_cmd" > "$out"
  cat "$out" > LICENSES-THIRD-PARTY.md
  echo "vm.sh: wrote LICENSES-THIRD-PARTY.md" >&2
else
  files | COPYFILE_DISABLE=1 tar -czf - --no-recursion --null -T - | ssh_vm "$remote_cmd"
fi
