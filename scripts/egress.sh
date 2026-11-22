#!/usr/bin/env bash
# Egress workers: short-lived sandbox clones that give data collection fresh Google Cloud IPs.
# The only place their flags are defined.
#
#   scripts/egress.sh up <N>                   create workers 1..N if missing (idempotent), provision them,
#                                              let the workbench pull from them
#   scripts/egress.sh sync [<scanner-dir>]     ship this worktree to /opt/ftf on every worker (venv + Chromium);
#                                              with <scanner-dir>, also ship that ftf tree (with ftf.scanner) to /opt/scan
#   scripts/egress.sh status                   workers, zone, state, time left, egress record
#   scripts/egress.sh run <vm|all> -- <cmd...> run a command as user ftf in /srv/ftf (foreground)
#   scripts/egress.sh job <vm> <name> -- <cmd...>  start a background job as user ftf; log /srv/ftf/jobs/<name>.log
#   scripts/egress.sh jobs [<vm>]              show each job's state and the last log line
#   scripts/egress.sh push-list <wb-path> <name> <vm>  copy a URL list workbench -> <vm>:/srv/ftf/inbox/<name>
#   scripts/egress.sh rotate <vm>              give a worker a new ephemeral IP (refused while a job runs)
#   scripts/egress.sh pull <run>               copy /srv/ftf/captures/<run> from every worker to the workbench
#                                              /data/derived/egress/, then rsync it to the bucket
#   scripts/egress.sh down                     delete every worker and confirm none is left
#   scripts/egress.sh flags                    dry run: print the create command (no cloud call)
#
# Workers are named ftf-egress-<n>; worker n goes to zone n of $EGRESS_ZONES (cycled; default: the
# three Warsaw zones, then Frankfurt, Eemshaven, Hamina). Each one has sandbox VM's safety model:
# no service account and no scopes, no firewall rule of its own (SSH only through IAP), an
# ephemeral external IP, the fetcher runs as user `ftf`, captures leave only to the workbench over
# the VPC through a forced-command key. Workers delete themselves after $EGRESS_MAX_RUN (2h).
#
# Egress record: /etc/ftf/egress.json holds the worker name, region, zone and a salted SHA-256 of
# its external IP (salt in the VM metadata only, shared by one rollout). `ftf fetch` copies it into
# every capture manifest (collector data). The raw IP is never written anywhere.
#
set -euo pipefail

PROJECT=${FTF_GCP_PROJECT:?set FTF_GCP_PROJECT}
FLEET_LABEL=ftf-fleet=egress
PREFIX=ftf-egress-
MACHINE=${EGRESS_MACHINE:-e2-medium}
MAX_RUN=${EGRESS_MAX_RUN:-2h}
read -r -a ZONES <<< "${EGRESS_ZONES:-europe-central2-a europe-central2-b europe-central2-c europe-west3-a europe-west4-a europe-north1-a}"
WORKBENCH=${FTF_VM:-workbench}
WORKBENCH_ZONE=europe-central2-a
WB_DEST=/data/derived/egress
BUCKET_DEST=gs://${FTF_BUCKET:-FTF_BUCKET-not-set}/outputs/egress/
MAX_WORKERS=12

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
die() { echo "egress.sh: $*" >&2; exit 1; }
[ $# -ge 1 ] || usage
mode=$1; shift

name_ok() { case "$1" in ''|.*|*[!A-Za-z0-9._-]*) return 1 ;; *) return 0 ;; esac; }

# "<name> <zone> <status>" for every worker of this fleet.
workers() {
  gcloud compute instances list --project "$PROJECT" --filter="labels.${FLEET_LABEL%%=*}=${FLEET_LABEL#*=} AND name~^${PREFIX}" \
    --format='value(name,zone.basename(),status)' | sort -t- -k3,3n
}
zone_of() {
  local z; z=$(workers | awk -v n="$1" '$1==n {print $2}')
  [ -n "$z" ] || die "no worker named $1"
  echo "$z"
}
ssh_to() {  # ssh_to <vm> <zone> <command>
  gcloud compute ssh "$1" --project "$PROJECT" --zone "$2" --tunnel-through-iap --quiet --ssh-flag=-T --command "$3"
}
wb() { ssh_to "$WORKBENCH" "$WORKBENCH_ZONE" "$1"; }
wait_ssh() {
  for _ in $(seq 1 30); do
    ssh_to "$1" "$2" true >/dev/null 2>&1 && return 0
    sleep 10
  done
  die "$1 not reachable over IAP SSH"
}
targets() {  # targets <vm|all> -> names of running workers
  if [ "$1" = all ]; then workers | awk '$3=="RUNNING" {print $1}'
  else zone_of "$1" >/dev/null; echo "$1"; fi
}
# Run "<fn> <vm> <zone>" for each named worker in parallel, prefixing output; fail if any failed.
each() {
  local fn=$1; shift
  local pids=() vm z rc=0
  for vm in "$@"; do
    z=$(zone_of "$vm")
    ( "$fn" "$vm" "$z" 2>&1 | sed -u "s/^/[$vm] /" ; exit "${PIPESTATUS[0]}" ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || rc=1; done
  return $rc
}

create_args() {  # create_args <name> <zone> <salt>: the full create command, one place for every flag
  printf '%s\0' gcloud compute instances create "$1" --project "$PROJECT" --zone "$2" \
    --machine-type "$MACHINE" \
    --create-disk "boot=yes,auto-delete=yes,image-family=debian-12,image-project=debian-cloud,size=15GB,type=pd-balanced" \
    --provisioning-model STANDARD \
    --no-service-account --no-scopes \
    --labels "$FLEET_LABEL,purpose=egress" \
    --tags ftf-egress \
    --metadata "ftf-egress-salt=$3" \
    --max-run-duration "$MAX_RUN" --instance-termination-action DELETE \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
}
create_vm() {
  local args=()
  while IFS= read -r -d '' a; do args+=("$a"); done < <(create_args "$@")
  "${args[@]}"
}

# Provisioning, run as root on a worker. Idempotent. Same users and transfer command as sandbox VM.
read -r -d '' SETUP <<'EOF' || true
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! dpkg -s python3-venv >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q python3-venv python3-pip ca-certificates curl
fi
id ftf >/dev/null 2>&1 || useradd --system --home-dir /srv/ftf --shell /usr/sbin/nologin ftf
id ftfxfer >/dev/null 2>&1 || useradd --create-home --shell /bin/sh ftfxfer
usermod -a -G ftf ftfxfer
install -d -o root -g root -m 755 /opt/ftf /opt/scan /etc/ftf
install -d -o ftf -g ftf -m 2750 /srv/ftf /srv/ftf/captures /srv/ftf/jobs
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
    [ $# -eq 2 ] && name_ok "$2" || { echo "usage: pull <run>" >&2; exit 2; }
    [ -d "/srv/ftf/captures/$2" ] || { tar -czf - -T /dev/null; exit 0; }
    exec tar -C /srv/ftf/captures -czf - "$2" ;;
  put-list)
    [ $# -eq 2 ] && name_ok "$2" || { echo "usage: put-list <name>" >&2; exit 2; }
    head -c 10000001 > "/srv/ftf/inbox/.$2.tmp"
    if [ "$(wc -c < "/srv/ftf/inbox/.$2.tmp")" -gt 10000000 ]; then rm -f "/srv/ftf/inbox/.$2.tmp"; echo "list larger than 10 MB" >&2; exit 2; fi
    mv "/srv/ftf/inbox/.$2.tmp" "/srv/ftf/inbox/$2" ;;
  *) echo "allowed: pull <run> | put-list <name>" >&2; exit 2 ;;
esac
XFER
chmod 755 /usr/local/bin/ftf-transfer
# Egress record: worker name, region, zone and a salted hash of the external IP (never the IP).
cat > /usr/local/sbin/ftf-egress-refresh <<'REFRESH'
#!/bin/sh
set -eu
md() { curl -fsS --max-time 5 -H Metadata-Flavor:Google "http://metadata.google.internal/computeMetadata/v1/$1"; }
name=$(md instance/name)
zone=$(md instance/zone); zone=${zone##*/}
region=${zone%-*}
salt=$(md instance/attributes/ftf-egress-salt)
ip=$(md instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || true)
if [ -z "$ip" ] || [ -z "$salt" ]; then rm -f /etc/ftf/egress.json; echo "no external IP" >&2; exit 1; fi
h=$(printf '%s\n%s' "$salt" "$ip" | sha256sum | cut -c1-32)
umask 022
printf '{"name": "%s", "region": "%s", "zone": "%s", "ip_hash": "sha256:%s"}\n' "$name" "$region" "$zone" "$h" > /etc/ftf/egress.json.tmp
mv /etc/ftf/egress.json.tmp /etc/ftf/egress.json
grep -qx "sha256:$h" /etc/ftf/egress.history 2>/dev/null || echo "sha256:$h" >> /etc/ftf/egress.history
cat /etc/ftf/egress.json
REFRESH
chmod 755 /usr/local/sbin/ftf-egress-refresh
/usr/local/sbin/ftf-egress-refresh
install -d -o ftfxfer -g ftfxfer -m 700 /home/ftfxfer/.ssh
echo "setup ok"
EOF

# Code sync + venv, run as root on a worker: SYNC <dir> with the tarball on stdin.
read -r -d '' SYNC <<'EOF' || true
set -euo pipefail
d=$1
rm -rf "$d/src.new" && mkdir "$d/src.new"
tar --warning=no-unknown-keyword --no-same-owner -xzf - -C "$d/src.new"
rm -rf "$d/src" && mv "$d/src.new" "$d/src"
chmod -R a+rX,go-w "$d/src"
export PLAYWRIGHT_BROWSERS_PATH=/opt/ftf/ms-playwright
hash=$(cat "$d/src/pyproject.toml" "$d/src/src/ftf/__init__.py" | sha256sum | cut -d' ' -f1)
if [ ! -x "$d/venv/bin/python" ] || [ "$(cat "$d/venv/.hash" 2>/dev/null)" != "$hash" ]; then
  rm -rf "$d/venv"
  python3 -m venv "$d/venv"
  "$d/venv/bin/pip" install -q --disable-pip-version-check -e "$d/src[fetch]"
  "$d/venv/bin/playwright" install --with-deps --only-shell chromium >/dev/null
  chmod -R a+rX /opt/ftf/ms-playwright
  echo "$hash" > "$d/venv/.hash"
fi
echo "sync ok: $d $("$d/venv/bin/ftf" --version)"
EOF

files() {  # tracked and untracked-but-not-ignored files of the git tree in the current directory
  git ls-files -z -co --exclude-standard | while IFS= read -r -d '' f; do
    case "$f" in data/*) continue ;; esac
    [ -e "$f" ] || continue
    printf '%s\0' "$f"
  done
}

AS_FTF='sudo -u ftf env -i HOME=/srv/ftf LANG=C.UTF-8 PATH=/opt/ftf/venv/bin:/usr/bin:/bin PLAYWRIGHT_BROWSERS_PATH=/opt/ftf/ms-playwright'
WB_KEY='$HOME/.ssh/ftf_egress'
WB_KNOWN='$HOME/.ssh/ftf_egress_known_hosts'
internal_ip() { gcloud compute instances describe "$1" --project "$PROJECT" --zone "$2" --format='value(networkInterfaces[0].networkIP)'; }
wb_ssh_worker() {  # command line, run on the workbench, that reaches ftf-transfer on a worker
  printf 'ssh -i %s -o IdentitiesOnly=yes -o UserKnownHostsFile=%s -o StrictHostKeyChecking=yes -o BatchMode=yes ftfxfer@%s' \
    "$WB_KEY" "$WB_KNOWN" "$(internal_ip "$1" "$2")"
}

provision() {  # provision <vm> <zone>: setup + workbench transfer key
  wait_ssh "$1" "$2"
  ssh_to "$1" "$2" "sudo bash -c $(printf '%q' "$SETUP")"
  local hostkey ip pub line
  hostkey=$(ssh_to "$1" "$2" 'cat /etc/ssh/ssh_host_ed25519_key.pub' | awk '{print $1" "$2}')
  ip=$(internal_ip "$1" "$2")
  # Host keys learnt over IAP; one line per internal IP (a recreated worker replaces its line).
  pub=$(wb "set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh; [ -f ~/.ssh/ftf_egress ] || ssh-keygen -q -t ed25519 -N '' -C workbench-to-egress -f ~/.ssh/ftf_egress; touch ~/.ssh/ftf_egress_known_hosts; flock ~/.ssh/ftf_egress_known_hosts sh -c \"grep -v '^$ip ' ~/.ssh/ftf_egress_known_hosts > ~/.ssh/ftf_egress_known_hosts.new || true; echo '$ip $hostkey' >> ~/.ssh/ftf_egress_known_hosts.new; mv ~/.ssh/ftf_egress_known_hosts.new ~/.ssh/ftf_egress_known_hosts\"; cat ~/.ssh/ftf_egress.pub")
  line="restrict,command=\"/usr/local/bin/ftf-transfer\" $pub"
  ssh_to "$1" "$2" "echo $(printf '%q' "$line") | sudo tee /home/ftfxfer/.ssh/authorized_keys >/dev/null && sudo chown ftfxfer:ftfxfer /home/ftfxfer/.ssh/authorized_keys && sudo chmod 600 /home/ftfxfer/.ssh/authorized_keys"
  echo "provisioned"
}

sync_one() {  # sync_one <vm> <zone>; uses $root and $scanner_dir
  (cd "$root" && files | COPYFILE_DISABLE=1 tar -czf - --no-recursion --null -T -) \
    | ssh_to "$1" "$2" "sudo bash -c $(printf '%q' "$SYNC") sync /opt/ftf"
  if [ -n "$scanner_dir" ]; then
    (cd "$scanner_dir" && find . \( -name .git -o -name build -o -name '*.egg-info' -o -name __pycache__ -o -name data \) -prune \
       -o -type f -print0 | COPYFILE_DISABLE=1 tar -czf - --null -T -) \
      | ssh_to "$1" "$2" "sudo bash -c $(printf '%q' "$SYNC") sync /opt/scan"
  fi
}

show='cd /srv/ftf/jobs || exit 0; for p in *.pid; do [ -e "$p" ] || continue; j=${p%.pid}; if kill -0 "$(cat "$p")" 2>/dev/null; then s=running; else s=finished; fi; echo "$j $s: $(tail -n 1 "$j.log" 2>/dev/null | cut -c1-160)"; done'
running_jobs='for p in /srv/ftf/jobs/*.pid; do [ -e "$p" ] || continue; kill -0 "$(cat "$p")" 2>/dev/null && basename "$p" .pid; done; true'

case "$mode" in
  flags)  # dry run: print the create command for worker 1 (no cloud call)
    create_args "${PREFIX}1" "${ZONES[0]}" "<salt>" | tr '\0' ' '; echo
    ;;
  up)
    [ $# -eq 1 ] && [ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le $MAX_WORKERS ] || die "usage: up <N>, 1 <= N <= $MAX_WORKERS"
    existing=$(workers)
    # One salt per rollout: reuse the salt of a live worker so their IP hashes stay comparable.
    salt=""
    first=$(echo "$existing" | awk 'NF {print $1" "$2; exit}')
    if [ -n "$first" ]; then
      salt=$(gcloud compute instances describe "${first% *}" --project "$PROJECT" --zone "${first#* }" --format=json \
        | python3 -I -c 'import json,sys; print(next((i["value"] for i in json.load(sys.stdin).get("metadata",{}).get("items",[]) if i.get("key")=="ftf-egress-salt"),""))')
      [ -n "$salt" ] || die "${first% *} has no ftf-egress-salt; run down first"
    fi
    [ -n "$salt" ] || salt=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')
    new=()
    for i in $(seq 1 "$1"); do
      vm="$PREFIX$i"; z=${ZONES[$(( (i - 1) % ${#ZONES[@]} ))]}
      if echo "$existing" | awk -v n="$vm" '$1==n {f=1} END {exit !f}'; then
        echo "egress.sh: $vm exists; flags are not changed" >&2
      else
        echo "egress.sh: creating $vm in $z ($MACHINE, no service account, deleted after $MAX_RUN)" >&2
        create_vm "$vm" "$z" "$salt" >/dev/null
        # Disks do not inherit instance labels: label the boot disk too.
        gcloud compute disks add-labels "$vm" --project "$PROJECT" --zone "$z" --labels "$FLEET_LABEL,purpose=egress" >/dev/null
      fi
      new+=("$vm")
    done
    each provision "${new[@]}"
    ;;
  sync)
    root=$(git rev-parse --show-toplevel)
    scanner_dir=""
    if [ $# -ge 1 ]; then
      scanner_dir=$(cd "$1" && pwd)
      [ -d "$scanner_dir/src/ftf/scanner" ] || die "$1 has no src/ftf/scanner"
    fi
    mapfile_vms=$(targets all)
    [ -n "$mapfile_vms" ] || die "no running workers"
    # shellcheck disable=SC2086
    each sync_one $mapfile_vms
    ;;
  status)
    gcloud compute instances list --project "$PROJECT" --filter="labels.${FLEET_LABEL%%=*}=${FLEET_LABEL#*=}" \
      --format='table(name,zone.basename(),status,machineType.basename(),scheduling.maxRunDuration.seconds:label=MAX_RUN_S,scheduling.instanceTerminationAction:label=ON_LIMIT,serviceAccounts.len():label=SA,lastStartTimestamp)'
    for vm in $(targets all); do
      echo "$vm: $(ssh_to "$vm" "$(zone_of "$vm")" 'cat /etc/ftf/egress.json 2>/dev/null || echo no egress record')"
    done
    ;;
  run)
    [ $# -ge 3 ] && [ "$2" = "--" ] || usage
    target=$1; shift 2
    cmd="$AS_FTF sh -c 'cd /srv/ftf && exec \"\$@\"' ftf-run $(printf '%q ' "$@")"
    run_one() { ssh_to "$1" "$2" "$cmd"; }
    # shellcheck disable=SC2046
    each run_one $(targets "$target")
    ;;
  job)
    [ $# -ge 4 ] && [ "$3" = "--" ] || usage
    vm=$1; job=$2; shift 3
    name_ok "$job" || die "bad job name"
    z=$(zone_of "$vm")
    inner="cd /srv/ftf && echo \$\$ > jobs/$job.pid && exec \"\$@\" > jobs/$job.log 2>&1"
    ssh_to "$vm" "$z" "sudo test ! -e /srv/ftf/jobs/$job.pid || { echo 'egress.sh: job $job already started' >&2; exit 1; }; $AS_FTF setsid nohup sh -c $(printf '%q' "$inner") ftf-job $(printf '%q ' "$@") < /dev/null > /dev/null 2>&1 &"
    echo "egress.sh: started $job on $vm" >&2
    ;;
  jobs)
    show_jobs() {
      ssh_to "$1" "$2" "sudo sh -c $(printf '%q' "$show")"
    }
    # shellcheck disable=SC2046
    each show_jobs $(targets "${1:-all}")
    ;;
  push-list)
    [ $# -eq 3 ] || usage
    name_ok "$2" || die "bad list name"
    z=$(zone_of "$3")
    wb "$(wb_ssh_worker "$3" "$z") put-list $2 < $(printf '%q' "$1")"
    echo "egress.sh: copied $WORKBENCH:$1 to $3:/srv/ftf/inbox/$2" >&2
    ;;
  rotate)
    [ $# -eq 1 ] || usage
    vm=$1; z=$(zone_of "$vm")
    busy=$(ssh_to "$vm" "$z" "sudo sh -c $(printf '%q' "$running_jobs")")
    [ -z "$busy" ] || die "$vm has running jobs ($busy); rotate between jobs"
    hash_now() { ssh_to "$vm" "$z" 'cat /etc/ftf/egress.json 2>/dev/null' | sed -n 's/.*"ip_hash": "\([^"]*\)".*/\1/p'; }
    # Every IP any worker of this rollout has had (salted hashes, one salt per rollout). A released
    # ephemeral IP is often handed straight back, or later to another worker of the same region:
    # retry until the IP is new to the rollout.
    used=$(for w in $(targets all); do
             ssh_to "$w" "$(zone_of "$w")" 'cat /etc/ftf/egress.history 2>/dev/null; cat /etc/ftf/egress.json 2>/dev/null'
           done | sed -n 's/.*\(sha256:[0-9a-f]*\).*/\1/p' | sort -u)
    for attempt in 1 2 3 4; do
      ac=$(gcloud compute instances describe "$vm" --project "$PROJECT" --zone "$z" --format='value(networkInterfaces[0].accessConfigs[0].name)')
      if [ -n "$ac" ]; then
        gcloud compute instances delete-access-config "$vm" --project "$PROJECT" --zone "$z" --access-config-name "$ac" --quiet
      fi
      gcloud compute instances add-access-config "$vm" --project "$PROJECT" --zone "$z" --access-config-name external-nat
      ssh_to "$vm" "$z" 'sudo /usr/local/sbin/ftf-egress-refresh' >/dev/null
      new=$(hash_now)
      if [ -n "$new" ] && ! printf '%s\n' "$used" | grep -qx "$new"; then
        echo "egress.sh: $vm has a new IP ($new)" >&2; exit 0
      fi
      echo "egress.sh: $vm got an IP it used before (attempt $attempt)" >&2
    done
    die "$vm got no unused IP after 4 attempts"
    ;;
  pull)
    [ $# -eq 1 ] && name_ok "$1" || die "usage: pull <run>"
    pull_one() { wb "set -euo pipefail; mkdir -p $WB_DEST; $(wb_ssh_worker "$1" "$2") pull $run | tar -xzf - -C $WB_DEST --no-same-owner --no-same-permissions"; }
    run=$1
    # shellcheck disable=SC2046
    each pull_one $(targets all)
    wb "gcloud storage rsync -r $WB_DEST/ $BUCKET_DEST && du -sh $WB_DEST/$run"
    ;;
  down)
    list=$(workers)
    if [ -n "$list" ]; then
      echo "$list" | awk '{print $2}' | sort -u | while read -r z; do
        # shellcheck disable=SC2046
        gcloud compute instances delete $(echo "$list" | awk -v z="$z" '$2==z {print $1}') \
          --project "$PROJECT" --zone "$z" --quiet
      done
    fi
    left=$(gcloud compute instances list --project "$PROJECT" --filter="labels.${FLEET_LABEL%%=*}=${FLEET_LABEL#*=}" --format='value(name)')
    [ -z "$left" ] || die "still present: $left"
    echo "egress.sh: no instance labelled $FLEET_LABEL is left" >&2
    ;;
  *) usage ;;
esac
