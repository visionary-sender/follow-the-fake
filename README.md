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
| `scripts/vm.sh` | cloud dev loop (sync to the workbench VM, run tests or commands there) |
| `scripts/licences.py` | generates and checks `LICENSES-THIRD-PARTY.md` |

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
they can use the VM at the same time; set `FTF_VM_SLUG` to choose the name yourself.

## Dependencies and licences

Only permissively licensed open-source dependencies are allowed (MIT, BSD, Apache, PSF, ISC).
`LICENSES-THIRD-PARTY.md` is generated from installed package metadata by
`scripts/vm.sh licences`; a test fails if the table is stale or lists a licence outside the
allowlist. After changing dependencies in `pyproject.toml`, regenerate it and commit it.

## Methods

To be written: ingestion, feature extraction, deduplication, campaign grouping and the demo app.
