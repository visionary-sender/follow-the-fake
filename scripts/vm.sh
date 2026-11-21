#!/usr/bin/env bash
# Cloud dev loop: sync this worktree to the workbench VM and run things there.
#
#   scripts/vm.sh test [pytest args...]   run the test suite on the VM, return pytest's exit code
#   scripts/vm.sh run -- <cmd...>         run a command in the synced checkout, venv active
#   scripts/vm.sh licences                regenerate LICENSES-THIRD-PARTY.md (written locally)
#
# Every call syncs the worktree's tracked and untracked-but-not-ignored files (never data/ or
# anything gitignored) to /work/<slug>/src on the VM, replacing the previous copy so files
# deleted locally disappear there too. The venv /work/<slug>/venv is pinned to /opt/venv-base
# versions and reinstalled only when pyproject.toml changes. <slug> is the worktree directory
# name (override with FTF_VM_SLUG), so several worktrees can use the VM at once.
# Nothing is computed locally: the laptop only lists files, packs them and drives ssh.
set -euo pipefail

VM=${FTF_VM:-ftf-workbench}
ZONE=${FTF_VM_ZONE:-europe-central2-a}
PROJECT=${FTF_VM_PROJECT:-follow-the-fake}
export CLOUDSDK_ACTIVE_CONFIG_NAME=${CLOUDSDK_ACTIVE_CONFIG_NAME:-follow-the-fake}

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
slug=$1; mode=$2; shift 2
w=/work/$slug
mkdir -p "$w"
exec 9>"$w/.lock"
flock 9
rm -rf "$w/src.new"
mkdir "$w/src.new"
tar --warning=no-unknown-keyword -xzf - -C "$w/src.new"
rm -rf "$w/src"
mv "$w/src.new" "$w/src"
hash=$(sha256sum "$w/src/pyproject.toml" | cut -d' ' -f1)
if [ ! -x "$w/venv/bin/python" ] || [ "$(cat "$w/venv/.pyproject-hash" 2>/dev/null)" != "$hash" ]; then
  echo "vm.sh: (re)installing venv $w/venv" >&2
  rm -rf "$w/venv"
  python3 -m venv "$w/venv"
  /opt/venv-base/bin/pip freeze --exclude-editable > "$w/base-constraints.txt"
  "$w/venv/bin/pip" install -q --disable-pip-version-check \
    -c "$w/base-constraints.txt" -e "$w/src[dev]" >&2
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

remote_cmd="bash -c $(printf '%q' "$remote") ftf-vm $(printf '%q ' "$slug" "$mode" "$@")"
echo "vm.sh: $VM:/work/$slug ($mode)" >&2

if [ "$mode" = licences ]; then
  out=$(mktemp)
  trap 'rm -f "$out"' EXIT
  files | COPYFILE_DISABLE=1 tar -czf - --null -T - | ssh_vm "$remote_cmd" > "$out"
  cat "$out" > LICENSES-THIRD-PARTY.md
  echo "vm.sh: wrote LICENSES-THIRD-PARTY.md" >&2
else
  files | COPYFILE_DISABLE=1 tar -czf - --null -T - | ssh_vm "$remote_cmd"
fi
