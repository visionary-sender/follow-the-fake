#!/usr/bin/env bash
# Sandbox VM for live fetching of untrusted URLs. The only place the VM's flags are defined.
#
#   scripts/sandbox.sh create              create ftf-sandbox if missing (idempotent), then setup
#   scripts/sandbox.sh setup               provision users, dirs and the transfer command (idempotent)
#   scripts/sandbox.sh sync                copy this worktree's code to /opt/ftf, (re)build venv + Chromium
#   scripts/sandbox.sh start|stop|status   power the VM on/off, show state and safety flags
#   scripts/sandbox.sh ssh                 interactive shell (IAP)
#   scripts/sandbox.sh run -- <cmd...>     run a venv command as the unprivileged user ftf in /srv/ftf
#   scripts/sandbox.sh authorize-workbench let ftf-workbench move files to/from the sandbox (see below)
#   scripts/sandbox.sh push-list <wb-path> <name>  copy a URL list workbench -> /srv/ftf/inbox/<name>
#   scripts/sandbox.sh pull [<run>]        copy /srv/ftf/captures[/<run>] sandbox -> workbench
#                                          /data/derived/fetcher/, then rsync it to the bucket
#
# Safety: the VM has no service account and no scopes (no cloud credentials at all), no inbound
# firewall rule of its own (SSH only through IAP via the existing allow-ssh-from-iap rule), an
# ephemeral external IP for outbound traffic, and stops itself after 8 h (--max-run-duration).
# The fetcher runs as user `ftf`, never root. Captured content never touches the laptop: it moves
# sandbox -> workbench directly over the VPC's internal network, using a key that lives only on
# the workbench and is restricted on the sandbox to a fixed command (/usr/local/bin/ftf-transfer).
# The laptop only orchestrates (gcloud, ssh commands); it never stores or opens captures.
#
# Cost (europe-central2, on-demand): e2-medium ~0.17 PLN/h while running; 20 GB pd-balanced
# ~0.3 PLN/day. Other issues may install their own software under /opt/<issue> on the same VM.
set -euo pipefail

VM=ftf-sandbox
ZONE=europe-central2-a
PROJECT=follow-the-fake
WORKBENCH=${FTF_VM:-ftf-workbench}
export CLOUDSDK_ACTIVE_CONFIG_NAME=${CLOUDSDK_ACTIVE_CONFIG_NAME:-follow-the-fake}
BUCKET_DEST=gs://follow-the-fake-data/outputs/fetcher/
WB_DEST=/data/derived/fetcher

create_vm() {
  gcloud compute instances create "$VM" --project "$PROJECT" --zone "$ZONE" \
    --machine-type e2-medium \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size 20GB --boot-disk-type pd-balanced \
    --provisioning-model STANDARD \
    --no-service-account --no-scopes \
    --labels ftf-issue=hack-17,purpose=sandbox \
    --tags ftf-sandbox \
    --max-run-duration 8h --instance-termination-action STOP \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
}

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -ge 1 ] || usage
mode=$1; shift

gc() { gcloud compute "$@" --project "$PROJECT" --zone "$ZONE"; }
ssh_to() {  # ssh_to <vm> <command>
  gcloud compute ssh "$1" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet \
    --ssh-flag=-T --command "$2"
}
state() { gc instances describe "$VM" --format='value(status)' 2>/dev/null || true; }
internal_ip() { gc instances describe "$VM" --format='value(networkInterfaces[0].networkIP)'; }
require_running() {
  local s; s=$(state)
  [ "$s" = RUNNING ] || { echo "sandbox.sh: $VM is ${s:-missing}; run: scripts/sandbox.sh start" >&2; exit 1; }
}
wait_ssh() {
  for _ in $(seq 1 30); do
    ssh_to "$VM" true >/dev/null 2>&1 && return 0
    sleep 10
  done
  echo "sandbox.sh: $VM not reachable over IAP SSH" >&2; exit 1
}

# Provisioning, run as root on the sandbox. Idempotent.
read -r -d '' SETUP <<'EOF' || true
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! dpkg -s python3-venv >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q python3-venv python3-pip ca-certificates
fi
id ftf >/dev/null 2>&1 || useradd --system --home-dir /srv/ftf --shell /usr/sbin/nologin ftf
id ftfxfer >/dev/null 2>&1 || useradd --create-home --shell /bin/sh ftfxfer
usermod -a -G ftf ftfxfer
install -d -o root -g root -m 755 /opt/ftf
install -d -o ftf -g ftf -m 2750 /srv/ftf /srv/ftf/captures
install -d -o ftfxfer -g ftf -m 2750 /srv/ftf/inbox
cat > /usr/local/bin/ftf-transfer <<'XFER'
#!/bin/sh
# Forced command for the workbench transfer key: nothing else can run with that key.
set -eu
umask 027
set -f
set -- ${SSH_ORIGINAL_COMMAND:-}
name_ok() { case "$1" in ''|.*|*[!A-Za-z0-9._-]*) return 1 ;; *) return 0 ;; esac; }
case "${1:-}" in
  pull)
    if [ $# -ge 2 ]; then name_ok "$2" || { echo "bad run name" >&2; exit 2; }; exec tar -C /srv/ftf/captures -czf - "$2"; fi
    exec tar -C /srv/ftf/captures -czf - . ;;
  put-list)
    [ $# -eq 2 ] && name_ok "$2" || { echo "usage: put-list <name>" >&2; exit 2; }
    head -c 10000000 > "/srv/ftf/inbox/.$2.tmp"; mv "/srv/ftf/inbox/.$2.tmp" "/srv/ftf/inbox/$2" ;;
  *) echo "allowed: pull [<run>] | put-list <name>" >&2; exit 2 ;;
esac
XFER
chmod 755 /usr/local/bin/ftf-transfer
install -d -o ftfxfer -g ftfxfer -m 700 /home/ftfxfer/.ssh
echo "setup ok"
EOF

# Code sync + venv, run as root on the sandbox; the tarball arrives on stdin.
read -r -d '' SYNC <<'EOF' || true
set -euo pipefail
rm -rf /opt/ftf/src.new && mkdir /opt/ftf/src.new
tar --warning=no-unknown-keyword --no-same-owner -xzf - -C /opt/ftf/src.new
rm -rf /opt/ftf/src && mv /opt/ftf/src.new /opt/ftf/src
chmod -R a+rX,go-w /opt/ftf/src
export PLAYWRIGHT_BROWSERS_PATH=/opt/ftf/ms-playwright
hash=$(cat /opt/ftf/src/pyproject.toml /opt/ftf/src/src/ftf/__init__.py | sha256sum | cut -d' ' -f1)
if [ ! -x /opt/ftf/venv/bin/python ] || [ "$(cat /opt/ftf/venv/.hash 2>/dev/null)" != "$hash" ]; then
  rm -rf /opt/ftf/venv
  python3 -m venv /opt/ftf/venv
  /opt/ftf/venv/bin/pip install -q --disable-pip-version-check -e "/opt/ftf/src[fetch]"
  /opt/ftf/venv/bin/playwright install --with-deps --only-shell chromium >/dev/null
  chmod -R a+rX /opt/ftf/ms-playwright
  echo "$hash" > /opt/ftf/venv/.hash
fi
echo "sync ok: $(/opt/ftf/venv/bin/ftf --version)"
EOF

files() {
  git ls-files -z -co --exclude-standard | while IFS= read -r -d '' f; do
    case "$f" in data/*) continue ;; esac
    [ -e "$f" ] || continue
    printf '%s\0' "$f"
  done
}

WB_KEY='$HOME/.ssh/ftf_sandbox'
WB_KNOWN='$HOME/.ssh/ftf_sandbox_known_hosts'
wb_ssh_sandbox() {  # command line, run on the workbench, that reaches ftf-transfer on the sandbox
  printf 'ssh -i %s -o IdentitiesOnly=yes -o UserKnownHostsFile=%s -o StrictHostKeyChecking=yes -o BatchMode=yes ftfxfer@%s' \
    "$WB_KEY" "$WB_KNOWN" "$(internal_ip)"
}

case "$mode" in
  create)
    s=$(state)
    if [ -z "$s" ]; then
      echo "sandbox.sh: creating $VM (e2-medium, no service account, auto-stop 8h)" >&2
      create_vm
    else
      echo "sandbox.sh: $VM already exists ($s); flags are not changed" >&2
      [ "$s" = RUNNING ] || exit 0
    fi
    wait_ssh
    ssh_to "$VM" "sudo bash -c $(printf '%q' "$SETUP")"
    ;;
  setup) require_running; ssh_to "$VM" "sudo bash -c $(printf '%q' "$SETUP")" ;;
  sync)
    require_running
    cd "$(git rev-parse --show-toplevel)"
    files | COPYFILE_DISABLE=1 tar -czf - --no-recursion --null -T - | ssh_to "$VM" "sudo bash -c $(printf '%q' "$SYNC")"
    ;;
  start) [ "$(state)" = RUNNING ] || gc instances start "$VM"; wait_ssh; echo "sandbox.sh: $VM running" >&2 ;;
  stop) [ "$(state)" = TERMINATED ] || gc instances stop "$VM" ;;
  status)
    gc instances describe "$VM" --format='table(name,status,machineType.basename(),scheduling.maxRunDuration.seconds:label=MAX_RUN_S,scheduling.instanceTerminationAction:label=ON_LIMIT,serviceAccounts.len():label=SERVICE_ACCOUNTS,labels)'
    ;;
  ssh) require_running; exec gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap ;;
  run)
    [ "${1:-}" = "--" ] && shift
    [ $# -ge 1 ] || usage
    require_running
    ssh_to "$VM" "sudo -u ftf env -i HOME=/srv/ftf LANG=C.UTF-8 PATH=/opt/ftf/venv/bin:/usr/bin:/bin PLAYWRIGHT_BROWSERS_PATH=/opt/ftf/ms-playwright sh -c 'cd /srv/ftf && exec \"\$@\"' ftf-run $(printf '%q ' "$@")"
    ;;
  authorize-workbench)
    require_running
    hostkey=$(ssh_to "$VM" 'cat /etc/ssh/ssh_host_ed25519_key.pub' | awk '{print $1" "$2}')
    ip=$(internal_ip)
    pub=$(ssh_to "$WORKBENCH" "set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh; [ -f ~/.ssh/ftf_sandbox ] || ssh-keygen -q -t ed25519 -N '' -C ftf-workbench-to-sandbox -f ~/.ssh/ftf_sandbox; echo '$ip $hostkey' > ~/.ssh/ftf_sandbox_known_hosts; cat ~/.ssh/ftf_sandbox.pub")
    line="restrict,command=\"/usr/local/bin/ftf-transfer\" $pub"
    ssh_to "$VM" "echo $(printf '%q' "$line") | sudo tee /home/ftfxfer/.ssh/authorized_keys >/dev/null && sudo chown ftfxfer:ftfxfer /home/ftfxfer/.ssh/authorized_keys && sudo chmod 600 /home/ftfxfer/.ssh/authorized_keys"
    echo "sandbox.sh: $WORKBENCH can now run ftf-transfer on $VM ($ip)" >&2
    ;;
  push-list)
    [ $# -eq 2 ] || usage
    require_running
    case "$2" in ''|.*|*[!A-Za-z0-9._-]*) echo "sandbox.sh: bad list name" >&2; exit 2 ;; esac
    ssh_to "$WORKBENCH" "$(wb_ssh_sandbox) put-list $2 < $(printf '%q' "$1")"
    echo "sandbox.sh: copied $WORKBENCH:$1 to $VM:/srv/ftf/inbox/$2" >&2
    ;;
  pull)
    require_running
    run=${1:-}
    case "$run" in *[!A-Za-z0-9._-]*|.*) echo "sandbox.sh: bad run name" >&2; exit 2 ;; esac
    ssh_to "$WORKBENCH" "set -euo pipefail; mkdir -p $WB_DEST; $(wb_ssh_sandbox) pull $run | tar -xzf - -C $WB_DEST --no-same-owner --no-same-permissions; gcloud storage rsync -r $WB_DEST/ $BUCKET_DEST; du -sh $WB_DEST"
    ;;
  *) usage ;;
esac
