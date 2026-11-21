# Follow the Fake (HackNation, NASK PIB challenge "Kolekcjoner fejków")

Goal: a tool that turns single suspicious ads (celebrity-likeness scams) and their landing
pages into campaigns: extract content, visual and public infrastructure features (domains,
redirects, links, keywords, faces), deduplicate, and group related incidents, showing the
similarities and links in a readable form.

Workflow: load the `follow-the-fake-workflow` skill before starting a Linear issue, branching,
committing, pushing, opening/merging a PR, or launching paid GCP work.

## Data (never in git)
- Bucket `gs://follow-the-fake-data` (GCP project `follow-the-fake`, europe-central2, private).
  Raw challenge materials: `raw/2026-11-21/` (BACKUP/, do_udostępnienia/, challenge PDFs).
- Local copies sit in the main checkout and are gitignored; in a worktree pull what you need:
  `gcloud storage rsync -r gs://follow-the-fake-data/raw/2026-11-21/do_udostępnienia data/do_udostępnienia`
- Derived outputs go to `gs://follow-the-fake-data/outputs/`, not git.
- `.envrc` (direnv) sets gcloud config `follow-the-fake` (account visionary.sender@gmail.com).

Everything written for the project (code, commits, PRs, Linear) is in English.
