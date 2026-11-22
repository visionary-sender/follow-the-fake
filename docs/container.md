# Container: running the jury image

One image holds the `ftf` command, the demo web app and the safe URL fetcher. It needs a CPU
only (no GPU), none of our keys and none of our cloud. It contains no organiser data, no campaign
base and no secrets (`scripts/image_leak_check.py` checks the built image). Run every command from
the repository root, because the run command reads the Chromium seccomp profile from
`docker/seccomp-chromium.json`. The entry point is `docker/entrypoint.sh`: it runs `ftf <arguments>`,
or, with no arguments, builds the base if needed and serves the app.

## 1. Build

Building needs internet access for pip, apt and Playwright's Chromium. It takes about 5 minutes and
the image is 1.63 GB (1,631,734,508 bytes, measured on the workbench on 2026-11-22). The image
includes Playwright's headless Chromium (`/opt/ms-playwright/chromium_headless_shell-1248`) with
its Debian system libraries, which `ftf fetch` needs; their licences are in
[`licences.md`](licences.md) section 2a.

Before the build, fetch the brand stop-list (HACK-75). It is built from our own public corpus
(batch 1, the pages tagged as real outlets) and holds hashed values only. It lives in the private
bucket, so run the fetch on the workbench VM, where the bucket is reachable:

```bash
scripts/fetch-brand-stoplist.sh     # writes docker/brand-stoplist/brand-stoplist.json (gitignored)
docker build -t ftf .
```

The Dockerfile copies that folder to `/opt/ftf-brand/` and sets `FTF_BRAND_STOPLIST` to the file.
The folder always exists (it holds a `.gitkeep`), so the build does not fail when the file is
missing. The image then runs without the stop-list: `ftf build-base` prints
`brand stop-list not loaded` and scores as before, and the brand-negative result still holds
through the platform/creative gate. Fetch and build in the same VM call, because `scripts/vm.sh`
replaces the synced worktree on every call and removes the fetched file.

## 2. First run with the organiser's dataset folder

Mount the folder read-only at `/data/raw` and a writable volume at `/base`. On first start the
container builds the campaign base from the folder (`ftf build-base`: ingest → content features →
linking, about 2 minutes on 2 CPUs for the challenge set), then serves the app. Later starts reuse
the base in the volume.

```bash
docker volume create ftf-base
docker run --rm --name ftf \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=512m --tmpfs /home/ftf:rw,nosuid,nodev,size=256m,uid=10001,gid=10001 \
  --cap-drop=ALL --cap-add=SYS_CHROOT --security-opt no-new-privileges \
  --security-opt seccomp=docker/seccomp-chromium.json \
  --shm-size=1g --pids-limit=512 --memory=4g --cpus=2 \
  -p 127.0.0.1:8501:8501 \
  -v "/path/to/organiser-folder:/data/raw:ro" \
  -v ftf-base:/base \
  ftf
```

Open <http://127.0.0.1:8501>. Inside the container the app listens on `0.0.0.0` (Docker's port
publishing needs it); `-p 127.0.0.1:...` keeps it reachable from this machine only. The hardening
flags are the fetcher's (see [Running the fetcher in the container](fetcher.md#running-fetcher-in-container))
plus `uid=10001,gid=10001` on the `/home/ftf` tmpfs: Docker mounts it root-owned with mode 0700, and
the app (Streamlit, the Public Suffix List cache) needs a writable home.

## 3. Run with a prebuilt base

If you were handed a base bundle, mount it at `/base` (read-only is fine) and leave out
`/data/raw`; nothing is rebuilt:

```bash
docker run --rm --name ftf <same flags as above> -p 127.0.0.1:8501:8501 \
  -v "/path/to/base-bundle:/base:ro" ftf
```

To rebuild a base by hand, or to build one into a host folder (`chmod 777` it first, the
container user is uid 10001):

```bash
docker run --rm <same flags> -v "/path/to/organiser-folder:/data/raw:ro" -v ftf-base:/base \
  ftf build-base --raw /data/raw --out /base --force
```

`ftf build-base --raw DIR --out DIR [--offline] [--force]` exits 1 and writes no `dataset.json` if no
linkable indicator is left after ingest and features (an empty base never reaches the app), and 2
on bad arguments or an existing base without `--force`. With no arguments the container builds
(if needed) and serves; with arguments it runs `ftf <arguments>`, e.g. `ftf --help`. Arguments
also get `FTF_BASE=/base/dataset.json` when that file is mounted, so `ftf analyze` uses the base
without `--base`.

To analyse one item in the container (no network, base and raw folder read-only, results in `/out`,
which must be writable by uid 10001):

```bash
docker run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=512m --cap-drop=ALL \
  --security-opt no-new-privileges --security-opt seccomp=docker/seccomp-chromium.json \
  -v "/path/to/organiser-folder:/data/raw:ro" -v "/path/to/base-bundle:/base:ro" \
  -v "/path/to/out:/out" ftf analyze /data/raw/<case-folder> --out /out/result.json
```

## Base bundle layout

What `/base` holds:

```
dataset.json             contract Dataset: incidents, indicators, links, campaigns (FTF_BASE)
vectors/                 embeddings that VectorRef.store points to (only when feature models run)
enrichment/cache.json    domain enrichment cache (optional; used as FTF_ENRICH_CACHE if present)
build-report.json        which build stages ran or were skipped
```

The enrichment cache is derived from organiser domains, so it is never baked into the image; it
travels only inside a privately handed-over base bundle.

## Environment variables

| Variable | Default in the image | Meaning |
|---|---|---|
| `FTF_BASE` | `/base/dataset.json` if present | campaign base the app and `ftf analyze` use |
| `FTF_BASE_DIR` | `/base` | where the entry point looks for and builds the base |
| `FTF_RAW_ROOT` | `/data/raw` | organiser folder; artefact paths in the base are relative to it |
| `FTF_OFFLINE` | unset | `1` forces every network lookup off (enrichment, build-base) |
| `FTF_FETCH` | unset | `1` lets the app fetch a submitted URL live with the safe fetcher, only while "Online enrichment" is ticked and `FTF_OFFLINE` is not set |
| `FTF_ENRICH_CACHE` | `/base/enrichment/cache.json` if present | enrichment cache file |
| `FTF_PORT` | `8501` | app port inside the container |
| `FTF_BRAND_STOPLIST` | `/opt/ftf-brand/brand-stoplist.json` | brand stop-list (hashed values of real outlet pages, from the build's `docker/brand-stoplist/` folder); `off` switches it off, a missing file warns and scores without it |

## Offline and online

The jury's machine has internet access. In the app, "Online enrichment" (off by default) allows
registry and DNS lookups for submitted domains. Live page fetching is additionally off unless the
container gets `-e FTF_FETCH=1` and "Online enrichment" is ticked (`FTF_OFFLINE=1` overrides both).
It then runs only under the hardened flags above; the fetcher refuses to run as root and keeps
Chromium's sandbox on. A dead or cloaked page is a normal result, not an error. Building the base
makes no network requests.

On the workbench VM the same image runs with `--network none` or `-e FTF_OFFLINE=1`: nothing there
contacts scam infrastructure.

## Resources

2 CPUs and 4 GB memory are enough: the app idles at about 150 MB, and building the base for the
challenge set takes about 2 minutes. Disk: the image (1.63 GB) plus a few MB of base.

## Verified on the workbench (HACK-72, 2026-11-22)

Built on `ftf-workbench` from the branch with `sudo docker build -t ftf:dev .` (1.63 GB, Chromium
present as above). Checks, run with the hardening flags of section 2 (`--memory=4g --cpus=2`):

- **App:** `docker run -d` with `-e FTF_OFFLINE=1`, no organiser folder and no base (the entry point
  then starts the app without a base). `/_stcore/health` answers HTTP 200, the app page answers
  HTTP 200, the container is running with 0 restarts.
- **Fetch with no network:** `docker run --rm --network none <same flags> ftf:dev fetch --out /out
  https://fetch-check.invalid/` (a reserved `.invalid` name, so no real host is contacted). Outcome
  `dead / dns_failure`, exit 0. The output folder is a tmpfs owned by uid 10001.
- **Image leak check:** `scripts/image_leak_check.py ftf:dev` reports 0 hits across 28,438 entries.

## Checking the image before handing it over

Scan the image on the VM. It exits 1 on any hit and prints paths only, never contents:

```bash
python3 -I scripts/image_leak_check.py ftf:dev --raw /data/raw --docker "sudo docker"
```

It flags `.git`, capture formats, base files (`dataset.json`, `cache.json`, `vectors/`), credential
files, files under `/data` or `/base`, stray images, key and token patterns, organiser case folder
names, and byte copies of organiser files (512 bytes or larger). Afterwards remove dangling images
with `sudo docker image prune -f`.
