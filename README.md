# Follow the Fake

HackNation challenge by NASK PIB ("Kolekcjoner fejków"). Scam ads that abuse the likeness of
well-known people usually arrive one at a time. This tool turns single suspicious ads and their
landing pages into campaigns: it extracts content, visual and public infrastructure features
(domains, redirects, links, keywords, faces), deduplicates them and groups related incidents,
showing the similarities and links in a readable form.

Challenge data lives in a private bucket and never in this repository.

## Layout

| Path | What |
|---|---|
| `src/ftf/` | the Python package; `ftf` is the command-line entry point |
| `tests/` | pytest suite |
| `scripts/spend.py` | read-only cloud spend report (see Cloud spend) |
| `scripts/vm.sh` | cloud dev loop (sync to the workbench VM, run tests or commands there) |
| `scripts/licences.py` | generates and checks `LICENSES-THIRD-PARTY.md` |
| `Dockerfile`, `.dockerignore` | container image that runs the `ftf` command |

## Running the tests

All computation runs on the workbench VM, not on the laptop.

Prerequisites:

- `gcloud` with the configuration `follow-the-fake` (the script sets
  `CLOUDSDK_ACTIVE_CONFIG_NAME=follow-the-fake` if it is unset) and IAP SSH access to the VM
  `ftf-workbench` (`europe-central2-a`). The VM must be running.
- `git` and `tar` locally.

```bash
scripts/vm.sh test                 # run the whole suite on the VM; exits with pytest's code
scripts/vm.sh test -k cli -x       # extra arguments go to pytest
scripts/vm.sh run -- ftf --help    # any command in the synced checkout, venv active
scripts/vm.sh licences             # regenerate LICENSES-THIRD-PARTY.md
```

Each call copies the worktree's tracked and untracked-but-not-ignored files (never `data/` or
anything gitignored) to `/work/<worktree-dir-name>/src` on the VM, replacing the previous copy.
The venv `/work/<worktree-dir-name>/venv` is pinned to the VM's base package versions and is
rebuilt only when `pyproject.toml` changes. Different worktrees get different directories, so
they can use the VM at the same time; set `FTF_VM_SLUG` to choose the name yourself. The script
refuses to take over an existing `/work/<name>` that it did not create.

## Dependencies and licences

Only permissively licensed open-source dependencies are allowed (MIT, BSD, Apache, PSF, ISC, HPND).
`LICENSES-THIRD-PARTY.md` is generated from installed package metadata by
`scripts/vm.sh licences`; a test fails if the table is stale or lists a licence outside the
allowlist. After changing dependencies in `pyproject.toml`, regenerate it and commit it.

## Container

The image contains only the installed `ftf` package (no data, no secrets, no git metadata; the
build context is default-deny in `.dockerignore`). It runs as a non-root user, and the
entry point is `ftf`. Building needs internet access, because pip fetches the dependencies.

For the jury, from a clean checkout of the repository root:

```bash
docker build -t ftf .
docker run --rm ftf --help
docker run --rm ftf --version
```

For the team, on the workbench VM. The sync copies the worktree to `/work/<slug>/src`, where
`<slug>` is the worktree directory name (replace it, e.g. `hack-3-container`):

```bash
scripts/vm.sh run -- true      # sync the worktree to the VM
gcloud compute ssh ftf-workbench --project follow-the-fake --zone europe-central2-a \
  --tunnel-through-iap --quiet \
  --command 'cd /work/<slug>/src && sudo docker build -t ftf:dev . && sudo docker run --rm ftf:dev --help'
```

Docker on the VM is a one-time setup: `sudo apt-get install -y docker.io`.
Remove dangling images afterwards with `sudo docker image prune -f`.

## Cloud spend

```bash
python3 -I scripts/spend.py
```

Read-only report of what GCP itself records (resource state plus the admin-activity and
system-event audit logs), per project, since 2026-11-21: every VM and disk of ours, including
deleted ones, with run intervals, an estimated cost in USD and PLN, the total against the
100 PLN cap, the split by the issue label, unlabelled resources as a warning, and everything
still running with the time its automatic stop fires. It only lists and reads; it never
creates, changes or deletes anything. Standard library only; it runs on the laptop because it
is just gcloud calls plus arithmetic.

- Costs are an **estimate**: pinned on-demand list prices (`VM_USD_PER_HOUR`,
  `DISK_USD_PER_GB_MONTH` at the top of the script) and a fixed USD to PLN rate of 4.00, so the
  same inputs give the same numbers. Buckets, egress and taxes are not included. A machine or
  disk type missing from the table is flagged and counted as 0 until it is added.
- `follow-the-fake` is read with gcloud configuration `follow-the-fake`; launches carry the
  label `ftf-issue=hack-<n>`.
- `attic-212115` is read with gcloud configuration `default` (override with
  `FTF_ATTIC_GCLOUD_CONFIG`). Nothing there may name this project, so our resources use names
  in the numeric range `8xxxxxxx` (e.g. `vm-80000034-1`) and the label `k=<issue number>`
  (e.g. `k=34`); only resources matching either are counted. This convention is provisional
  and set in one place (`ATTIC_LABEL_KEY`, `ATTIC_NAME_PATTERN`).
- Backstop: billing data lags by hours, no BigQuery billing export is set up and no GCP budget
  alert is known on the billing account (the Budget API is not readable here), so the script is the primary check.

## Methods

To be written: ingestion, feature extraction, deduplication, campaign grouping and the demo app.
