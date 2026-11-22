#!/usr/bin/env bash
# Run the ad-library scanner on the sandbox VM (the only machine that talks to ad libraries).
#
#   scripts/scanner-sandbox.sh install    copy tracked code to /opt/hack-20, build venv + Chromium, install the timer
#   scripts/scanner-sandbox.sh run        start one scan now (the same unit the timer starts)
#   scripts/scanner-sandbox.sh status     timer, runs from the journal, log tail, queue length
#   scripts/scanner-sandbox.sh authorize-workbench  let ftf-workbench read /opt/hack-20/data and
#                                         write the keyword file (once; re-run to add the keyword key)
#   scripts/scanner-sandbox.sh keywords   copy gs://follow-the-fake-data/config/scanner-keywords.txt
#                                         to /opt/hack-20/config, streamed by the workbench
#   scripts/scanner-sandbox.sh pull       copy /opt/hack-20/data to the workbench /data/derived/scanner/,
#                                         then rsync it to gs://follow-the-fake-data/outputs/scanner/
#   scripts/scanner-sandbox.sh uninstall  stop and remove the timer and service (data stays)
#
# Everything lives under /opt/hack-20 on the sandbox (venv, browsers, data, log, the scan user's
# home, the keyword file); outside it only the two systemd units and the users hack20 (runs the
# scan) and hack20xfer (transfer). The sandbox VM itself belongs to HACK-17 (scripts/sandbox.sh);
# this script never creates, starts or stops it. Output moves sandbox -> workbench directly over
# the VPC's internal network with a key that lives only on the workbench and is restricted on the
# sandbox to one fixed command (tar of /opt/hack-20/data); the keyword file moves workbench ->
# sandbox with a second key restricted to one fixed write. Nothing passes through the laptop.
set -euo pipefail

SANDBOX=${FTF_SANDBOX:-ftf-sandbox}
WORKBENCH=${FTF_VM:-ftf-workbench}
ZONE=${FTF_VM_ZONE:-europe-central2-a}
PROJECT=${FTF_VM_PROJECT:-follow-the-fake}
BUCKET_DEST=gs://follow-the-fake-data/outputs/scanner/
WB_DEST=/data/derived/scanner
KW_FILE=/opt/hack-20/config/scanner-keywords.txt
KEYWORDS_SRC=gs://follow-the-fake-data/config/scanner-keywords.txt
export CLOUDSDK_ACTIVE_CONFIG_NAME=${CLOUDSDK_ACTIVE_CONFIG_NAME:-follow-the-fake}

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[ $# -eq 1 ] || usage
mode=$1

cd "$(git rev-parse --show-toplevel)"

ssh_to() {  # ssh_to <vm> <command>
  gcloud compute ssh "$1" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet \
    --ssh-flag=-T --command "$2"
}
internal_ip() {
  gcloud compute instances describe "$SANDBOX" --project "$PROJECT" --zone "$ZONE" \
    --format='value(networkInterfaces[0].networkIP)'
}
workbench_pub() {  # workbench_pub <key path on the workbench, literal $HOME ok> <comment>: create once, print public key
  ssh_to "$WORKBENCH" "set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh;
    [ -f $1 ] || ssh-keygen -q -t ed25519 -N '' -C $2 -f $1; cat $1.pub"
}

# Tracked files only (as in the working tree): untracked local files never reach the sandbox.
files() {
  git ls-files -z -c | while IFS= read -r -d '' f; do
    [ -e "$f" ] && printf '%s\0' "$f"
  done
}

# Install, run as root on the sandbox; the code tarball arrives on stdin. Idempotent.
read -r -d '' INSTALL <<'EOF' || true
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
h=/opt/hack-20
dpkg -s python3-venv >/dev/null 2>&1 || { apt-get update -q; apt-get install -y -q python3-venv python3-pip ca-certificates; }
id hack20 >/dev/null 2>&1 || useradd --system --home-dir $h/home --shell /usr/sbin/nologin hack20
id hack20xfer >/dev/null 2>&1 || useradd --create-home --shell /bin/sh hack20xfer
usermod -a -G hack20 hack20xfer
install -d -o root -g root -m 755 $h
install -d -o hack20 -g hack20 -m 2750 $h/data $h/log $h/home
# Setgid on the group hack20: the keyword file written by hack20xfer is readable by the scan user.
install -d -o hack20xfer -g hack20 -m 2750 $h/config
rm -rf $h/src.new && mkdir $h/src.new
tar --warning=no-unknown-keyword --no-same-owner -xzf - -C $h/src.new
rm -rf $h/src && mv $h/src.new $h/src
chmod -R a+rX,go-w $h/src
export PLAYWRIGHT_BROWSERS_PATH=$h/ms-playwright
[ -x $h/venv/bin/python ] || python3 -m venv $h/venv
$h/venv/bin/pip install -q --disable-pip-version-check --upgrade "$h/src[fetch]"
$h/venv/bin/playwright install --with-deps --only-shell chromium >/dev/null
chmod -R a+rX $h/ms-playwright $h/venv
[ -f $h/scan.env ] || printf '# Extra arguments for the timer run, e.g. FTF_SCAN_ARGS=--max-ads 100\nFTF_SCAN_ARGS=\n' > $h/scan.env
install -m 644 $h/src/deploy/scanner/ftf-scan.service $h/src/deploy/scanner/ftf-scan.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable ftf-scan.timer >/dev/null 2>&1
systemctl restart ftf-scan.timer
echo "installed $($h/venv/bin/ftf --version)"
systemctl list-timers ftf-scan.timer --no-pager
EOF

read -r -d '' STATUS <<'EOF' || true
h=/opt/hack-20
systemctl list-timers ftf-scan.timer --no-pager
echo "--- runs (journal)"
sudo journalctl -u ftf-scan.service --no-pager -o short-iso | grep -E 'Starting|Finished|Failed|Deactivated|status=' | tail -n 12
echo "--- scan.log (tail)"
sudo tail -n 12 $h/log/scan.log 2>/dev/null
echo "--- queue: $(sudo cat $h/data/queue.jsonl 2>/dev/null | wc -l) items; runs: $(sudo ls $h/data/runs 2>/dev/null | wc -l)"
if sudo test -f $h/config/scanner-keywords.txt; then echo "--- keyword file: present ($(sudo wc -l $h/config/scanner-keywords.txt | cut -d" " -f1) lines)"; else echo "--- keyword file: missing (the timer skips its runs)"; fi
EOF

WB_KEY='$HOME/.ssh/hack20_sandbox'
WB_KNOWN='$HOME/.ssh/hack20_sandbox_known_hosts'
CFG_KEY='$HOME/.ssh/hack20_config'

case "$mode" in
  install)
    files | COPYFILE_DISABLE=1 tar -czf - --no-recursion --null -T - \
      | ssh_to "$SANDBOX" "sudo bash -c $(printf '%q' "$INSTALL")"
    ;;
  run)
    ssh_to "$SANDBOX" "sudo systemctl start --no-block ftf-scan.service && echo 'started ftf-scan.service'"
    ;;
  status)
    ssh_to "$SANDBOX" "bash -c $(printf '%q' "$STATUS")"
    ;;
  authorize-workbench)
    ip=$(internal_ip)
    hostkey=$(ssh_to "$SANDBOX" 'cat /etc/ssh/ssh_host_ed25519_key.pub' | awk '{print $1" "$2}')
    ssh_to "$WORKBENCH" "set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh; echo '$ip $hostkey' > $WB_KNOWN"
    pub=$(workbench_pub "$WB_KEY" hack20-workbench-to-sandbox)
    cfg=$(workbench_pub "$CFG_KEY" hack20-workbench-config-to-sandbox)
    line1="restrict,command=\"tar -C /opt/hack-20/data --exclude=./.lock -czf - .\" $pub"
    line2="restrict,command=\"umask 027 && cat > $KW_FILE.new && mv -f $KW_FILE.new $KW_FILE\" $cfg"
    # Base64 keeps the two lines one shell word on the sandbox, whatever characters the keys hold.
    keys_b64=$(printf '%s\n%s\n' "$line1" "$line2" | base64 | tr -d '\n')
    ssh_to "$SANDBOX" "sudo install -d -o hack20xfer -g hack20xfer -m 700 /home/hack20xfer/.ssh &&
      printf '%s' $keys_b64 | base64 -d | sudo tee /home/hack20xfer/.ssh/authorized_keys >/dev/null &&
      sudo chown hack20xfer: /home/hack20xfer/.ssh/authorized_keys && sudo chmod 600 /home/hack20xfer/.ssh/authorized_keys"
    echo "scanner-sandbox.sh: $WORKBENCH may now read /opt/hack-20/data and write $KW_FILE on $SANDBOX ($ip)" >&2
    ;;
  keywords)
    ip=$(internal_ip)
    ssh_to "$WORKBENCH" "set -euo pipefail;
      gcloud storage cat $KEYWORDS_SRC | ssh -i $CFG_KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$WB_KNOWN -o StrictHostKeyChecking=yes -o BatchMode=yes hack20xfer@$ip;
      echo 'pushed the keyword file to $SANDBOX (check with status)'"
    ;;
  pull)
    ip=$(internal_ip)
    ssh_to "$WORKBENCH" "set -euo pipefail; mkdir -p $WB_DEST;
      ssh -i $WB_KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$WB_KNOWN -o StrictHostKeyChecking=yes -o BatchMode=yes hack20xfer@$ip \
        | tar -xzf - -C $WB_DEST --no-same-owner --no-same-permissions;
      gcloud storage rsync -r $WB_DEST/ $BUCKET_DEST;
      echo \"pulled: \$(wc -l < $WB_DEST/queue.jsonl) queue items, \$(du -sh $WB_DEST | cut -f1) -> $BUCKET_DEST\""
    ;;
  uninstall)
    ssh_to "$SANDBOX" 'sudo systemctl disable --now ftf-scan.timer >/dev/null 2>&1 || true;
      sudo rm -f /etc/systemd/system/ftf-scan.timer /etc/systemd/system/ftf-scan.service;
      sudo systemctl daemon-reload; echo removed'
    ;;
  *) usage ;;
esac
