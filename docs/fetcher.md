The README summarises the fetcher and links here. This page holds the outcome categories, the safety
model, the sandbox VM and the hardened container run.

Given a URL, `ftf fetch` opens it in a locked-down headless Chromium (Playwright) and saves
what a visitor would get: the network traffic (HAR), the final HTML, a full-page screenshot and
the redirect chain. It never fills in or submits a form and never clicks.

```bash
ftf fetch https://example.org/landing --out captures/           # one or more URLs
ftf fetch --list urls.txt --out captures/                       # batch: every http(s) URL in the file
ftf fetch --list urls.txt --out captures/ --timeout 30 --settle 6 --max-mb 64
ftf fetch --profile victim-pl-mobile --referer https://l.facebook.com/ --fbclid --list urls.txt --out captures/
```

Scam pages are often cloaked: they show the scam only to visitors who look like targets and a
decoy to everyone else. The opt-in `victim-pl-mobile` profile presents the visit as a Polish phone
user (Android Chrome user agent and client hints, `pl-PL`, Europe/Warsaw, mobile viewport and
touch); `--referer` and `--fbclid` make it look like a click from a Meta ad. No proxies or IP
rotation are used, and the network guard and lockdown are the same. Every capture records its
profile in `capture.json` (`profile`); this describes our visit, so it never links captures. All
profiles send client hints matching the user agent, without Chromium's `HeadlessChrome` brand.

Each visit becomes a capture folder laid out like an organiser case folder, so the ingestion step reads
both the same way:

```
captures/<capture_id>/            e.g. <timestamp>-landing.example-1a2b3c4d
    url.txt          requested URL, then every main-frame URL visited, one per line
    page.har         network traffic, response bodies embedded
    page.html        final DOM
    screenshot.png   full page (height capped at 12,000 px)
    capture.json     manifest: outcome, reason, redirect chain, blocked requests, limits,
                     and the contract Incident (source live_fetch, one Capture, observed=live)
captures/dataset.json             contract Dataset with every capture in the folder
```

Artefact paths in the contract are relative to the output folder, as organiser paths are
relative to the raw data root.

### Dead and cloaked pages are results, not errors

Every URL gives a capture folder and an outcome. The command exits 0 and prints one line per URL
plus a summary.

| Category | Outcome | When |
|---|---|---|
| live | `live` | a page loaded |
| dead | `http_error` | the final response is 4xx/5xx, e.g. 404 or a CDN's "origin unreachable" 52x (the error page is still saved) |
| cloaked | `bot_wall` | Cloudflare/DDoS-Guard/Imperva/PerimeterX/DataDome challenge, captcha, JS verification |
| cloaked | `decoy` | parked domain, default server page, directory listing, suspended host, or a redirect off-site to a well-known site (the cloaker's "white page") |
| cloaked | `empty` | almost no visible text and no media |
| dead | `dns_failure`, `connection_failed`, `timeout`, `navigation_error` | the site did not answer |
| dead | `blocked_address` | the guard refused the destination (private, loopback, metadata, odd port) |
| dead | `size_limit` | the page's byte budget ran out before the document arrived |
| dead | `download` | the URL served a file download (refused; nothing is saved) |
| dead | `invalid_url`, `fetcher_error` | not an http(s) URL / the fetcher itself failed |

In the contract: dead pages list `html` and `screenshot` (and `har` if nothing was recorded) in
`Capture.missing` with the reason (`"dns_failure: ..."`); decoys get `stage = decoy`; every
capture carries one `fetch_outcome` indicator in the `collector` layer (it describes our visit,
so it never links captures). Content heuristics only fire on well-known markers, and the reason
names the marker.

### Safety model

Scam pages are hostile. The fetcher assumes the page will try to attack the machine it runs on.

- **Network guard.** All browser traffic goes through a small proxy inside the fetcher
  (`src/ftf/fetch/guard.py`). It allows only http/https on web ports, resolves each host once,
  refuses it if any address is not globally routable (RFC 1918, loopback, link-local including
  `169.254.169.254`, CGNAT, `::1`, `fc00::/7`, `fe80::/10`, and IPv4-mapped/6to4/NAT64 forms of
  those) or if the name is internal (`localhost`, `*.internal`, `metadata.google.internal`, ...),
  and then connects to the exact address it checked. Every redirect hop is a new request through
  the guard, and a DNS answer that changes after the check (DNS rebinding) is never used.
  WebRTC may not send UDP outside the proxy, QUIC is off, and Chromium's loopback bypass is
  disabled.
- **Nothing is submitted.** Only GET/HEAD/OPTIONS requests leave the browser; `form.submit()`,
  `requestSubmit()` and submit events are neutralised before page scripts run; the fetcher never
  clicks. Blocked requests are listed in `capture.json`, so their URLs are still evidence.
- **Browser lockdown.** JavaScript is on (scam pages need it), but downloads are refused,
  permissions denied, service workers blocked and popups closed at once. Requests other than
  http(s) are aborted (`file://`, `chrome://`, ...); Chromium itself blocks pages from navigating
  the top frame to `data:` URLs, and `ftf fetch` refuses non-http(s) start URLs. Chromium's own sandbox stays on, the fetcher refuses to run as
  root, and the browser gets a minimal environment with a throwaway home directory, so it never
  sees host variables or credentials.
- **Hard limits.** Navigation timeout (30 s), settle time (6 s), 64 MB received per page, 1,500
  requests, 15 MB of HTML, 12,000 px of screenshot. Each visit runs under its own small
  supervisor process (a child subreaper with a private temp directory). After a wall-clock limit
  (timeout + settle + 45 s) the supervisor kills every process of the visit, Chromium included,
  and removes the temp directory; the capture then says `timeout`.
- **Where it runs.** During the build, live URLs are fetched only on the sandbox VM
  (`scripts/sandbox.sh`): no service account and no scopes (no cloud credentials at all), SSH
  only through IAP, outbound traffic through an ephemeral IP, automatic stop after 8 h, fetcher
  running as an unprivileged user. Captures move from the sandbox straight to the cloud VM over
  the internal network with a key that can only run a fixed transfer command, and from there to
  `gs://<DATA_BUCKET>/outputs/fetcher/`. They never pass through a laptop.

### Sandbox VM

```bash
scripts/sandbox.sh create                 # once: <SANDBOX_VM>, e2-medium, no service account, auto-stop 8 h
scripts/sandbox.sh start                  # later sessions
scripts/sandbox.sh sync                   # code -> /opt/ftf, venv + Chromium (rebuilt when pyproject changes)
scripts/sandbox.sh authorize-workbench    # once: cloud VM key, restricted to the transfer command
scripts/sandbox.sh push-list /data/raw/<link-list>.txt links.txt   # cloud VM -> sandbox
scripts/sandbox.sh run -- ftf fetch --list /srv/ftf/inbox/links.txt --out /srv/ftf/captures/run1
scripts/sandbox.sh pull run1              # sandbox -> cloud VM /data/derived/fetcher/ -> bucket
scripts/sandbox.sh stop
```

The fetcher's browser tests run against a local test server only. The `dev` extra does not
install Playwright, so they are skipped by default. To run them on the cloud VM, add the extra to
your worktree's venv once (and Chromium for your user, `playwright install --only-shell chromium`):

```bash
scripts/vm.sh run -- pip install -c /work/<slug>/base-constraints.txt -e '.[fetch]'
scripts/vm.sh test tests/fetch
```

### Egress workers (optional, corpus building)

Ad libraries and cloaked scam pages rate-limit or block a single cloud IP. While building the
corpus we spread collection over a few short-lived Google Cloud VMs (`scripts/egress.sh`), each a
clone of the sandbox's safety model (no service account, IAP-only SSH, no firewall rule of its own,
deleted automatically after 2 h), each with its own ephemeral IP, at low volume per IP. Workers are
sandbox VMs in the sense of the rule: live scam pages are fetched there and nowhere else
besides the sandbox VM, and captures go straight to the cloud VM. This was
used only to build the corpus. It is opt-in: the container and the demo never
use it, and nothing changes when it is absent.

```bash
scripts/egress.sh up 6                    # workers 1..6 (Warsaw x3, Frankfurt, Eemshaven, Hamina)
scripts/egress.sh sync                    # code -> /opt/ftf on every worker
scripts/egress.sh push-list <local-file> urls.txt <EGRESS_VM>
scripts/egress.sh job <EGRESS_VM> r1 -- ftf fetch --list /srv/ftf/inbox/urls.txt --out /srv/ftf/captures/run1/<EGRESS_VM>
scripts/egress.sh rotate <EGRESS_VM>     # new IP between jobs
scripts/egress.sh pull run1               # workers -> cloud VM /data/derived/egress/ -> bucket
scripts/egress.sh down                    # delete all workers, confirm none is left
```

Each capture made on a worker records which egress it used, in the manifest (`egress`: worker,
region, zone, salted SHA-256 of the IP) and as a collector-layer `analyst_ip` indicator, so it can
never link two captures. The raw IP is never written; the salt lives only in the workers' metadata
and disappears with them.

### Running the fetcher in the container

The image includes Playwright's headless Chromium. Run the fetcher with these flags:

```bash
mkdir -p captures && chmod 777 captures      # the container user (uid 10001) writes here
docker run --rm \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=512m --tmpfs /home/ftf:rw,nosuid,nodev,size=256m \
  --cap-drop=ALL --cap-add=SYS_CHROOT --security-opt no-new-privileges \
  --security-opt seccomp=docker/seccomp-chromium.json \
  --shm-size=1g --pids-limit=512 --memory=2g --cpus=2 \
  -v "$PWD/captures:/out" \
  ftf fetch --out /out https://example.org/
```

- `--read-only` plus two small tmpfs mounts: the root filesystem cannot be changed; the only
  host directory the container sees is the output folder.
- `--cap-drop=ALL --cap-add=SYS_CHROOT`: no capabilities except the one Chromium's
  user-namespace sandbox needs in its bounding set. The process runs as the non-root user `ftf`
  and `no-new-privileges` stops it from ever gaining privileges.
- `seccomp=docker/seccomp-chromium.json`: Docker's default seccomp profile blocks the namespace
  calls Chromium's sandbox needs ("No usable sandbox!"). This file is Playwright's published
  profile (`utils/docker/seccomp_profile.json` at v1.64.1, Apache-2.0, unchanged).
- No other volumes, no Docker socket, no host network, no environment variables or keys.
  Internet access is needed only to reach the pages being fetched.
