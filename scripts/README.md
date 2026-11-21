# scripts

## orca-setup.sh: worktree setup

Makes a new worktree of this repo ready to work with the right accounts and prints one line
per check (`[ ok ]`, `[warn]`, `[FAIL]`, `[info]`), then a summary: `ready`,
`ready with N warning(s)` or `NOT READY: <what is broken>`.

What it does, in order:

1. Prints the worktree, branch and main checkout. The main checkout is always derived from
   git (`--git-common-dir`); `ORCA_ROOT_PATH` is only cross-checked (warning if it differs).
2. Refuses (exit 2, nothing changed) outside a git worktree or unless `origin` is
   `visionary-sender/follow-the-fake` on host `github.com` (credentials in the URL are never printed).
3. Git identity, repo-local only: if no local `user.email` is set, sets the visionary-sender
   GitHub noreply address (`<id>+visionary-sender@users.noreply.github.com`, from `gh api user`).
   An existing local value is left alone. Global git config is never touched.
4. Push credentials: the repo-local `credential.helper` uses `gh auth token -u visionary-sender`,
   and that command works (the token is never printed).
5. Hooks: runs the follow-the-fake-workflow skill's `install-hooks.sh` for the main checkout, then
   `--check` (commit-msg + pre-commit). A missing skill directory is a failure.
6. Branch and issue: reads `vis-<n>` from the branch name and shows the Linear issue title,
   state and `Type:` line (Orca CLI, 8 s timeout, never fatal; control, bidi and invisible
   characters in the Linear text are removed). Warns on a non-issue branch, and when the
   branch differs from the issue's `branchName` it prints the `git branch -m '<branchName>'`
   command (never renames itself).
7. Cloud accounts: `direnv allow` for the `.envrc` of this worktree and of the main checkout
   when it is new or changed (direnv "not allowed"); a denied `.envrc` or an unreadable direnv
   status is only reported. Either still blocked is a failure. Checks
   that the gcloud configuration `follow-the-fake` points at `follow-the-fake` (reads the
   local config file).
8. Linear API key for `linear_api.py` is in the keychain (existence only).
9. Fails if challenge data (BACKUP/, do_udostępnienia/, data/, PDFs, HARs) is tracked in git.
10. Prints three working rules.

Exit code: 0 = ready (warnings allowed); 1 = an essential check failed (git identity, push
credentials, hooks, direnv/.envrc, gcloud configuration); 2 = refused or not applicable (not a
git worktree, or not this repo), nothing changed. If the visionary-sender `gh` login has no
token, the script runs in check-only mode: it reports everything and changes nothing.
A second run changes nothing. Network: the Linear lookup, plus `gh api user` only when the
identity is missing.

### How Orca runs it

`orca.yaml` at the repo root:

```yaml
scripts:
  setup: |
    bash scripts/orca-setup.sh
```

Orca runs `scripts.setup` in the new worktree after creating it, with `ORCA_ROOT_PATH`
(main checkout), `ORCA_WORKTREE_PATH` and `ORCA_WORKSPACE_NAME` set. The first time (and
whenever the setup script in `orca.yaml` changes) Orca asks once to trust it; approve it in
the Orca UI. Until then new worktrees are created without running it.

### Run by hand

From any worktree of this repo:

```bash
bash scripts/orca-setup.sh
```

### Verify

```bash
bash -n scripts/orca-setup.sh          # syntax, bash 3.2 compatible
bash scripts/orca-setup.sh; echo $?    # last line "ready" and exit 0
bash scripts/orca-setup.sh             # second run: same output, nothing installed or set
orca repo show --repo path:$HOME/nask-fake-collector --json   # Orca's view of the repo and its setup policy
```

## .envrc

Tracked, loaded by direnv: sets `CLOUDSDK_ACTIVE_CONFIG_NAME=follow-the-fake` and, when the
visionary-sender `gh` login has a token, `GH_TOKEN`, so plain `gh` and `gcloud` target the
right accounts inside the repo.

### After merging a change to .envrc

direnv blocks an `.envrc` whose content changed until it is allowed again. While blocked,
`CLOUDSDK_ACTIVE_CONFIG_NAME` is not set and `gcloud` falls back to the global default
configuration, which points at a different project. So after a change to `.envrc` reaches
`main`, run `bash scripts/orca-setup.sh` (or `direnv allow`) in `~/nask-fake-collector` and in every open
worktree once it has the new `.envrc`. The setup script reports a blocked `.envrc` as a failure.
