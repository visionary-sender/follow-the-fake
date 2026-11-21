"""Scanner output directory: seen-id state, the assignment queue and per-run documents.

Layout under `--out` (the capture folders match the URL fetcher's, so one ingestion adapter
reads both):

    <capture id>/capture.json  manifest (tool, capture id, times, source, ad id) with a provisional
                               contract Incident under "incident"; capture id = `<run stamp>-<source>-<ad id>`.
                               The folder and ad.json are the stable output; Ingestion's adapter
                               builds the canonical Incident from them.
    <capture id>/ad.json       the ad-library fields as found (page, advertiser, text, URLs, dates)
    <capture id>/creative-<n>.<ext>  creative images (only with --download-creatives)
    dataset.json               contract Dataset of every incident under this root
    queue.jsonl                one line per new ad waiting for assignment
    state/seen.json            {"<source>:<ad id>": "<first run id>"}: every key ever queued
    runs/<run id>/summary.json per-source status, counts and evidence
    .lock                      held for the whole run; a second run exits instead of waiting

Artefact paths are relative to `--out`. A run writes the capture folders first, then appends the queue, then saves `seen.json`. If it dies in
between, the next run also treats every key already in the queue as seen, so nothing is
queued twice.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Iterable

QUEUE = "queue.jsonl"
SEEN = "state/seen.json"


class Locked(RuntimeError):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock_fd = None

    def __enter__(self) -> "Store":
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Locked(f"another scan holds {self.root / '.lock'}") from None
        self._lock_fd = fd
        return self

    def __exit__(self, *exc) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    # --- state -----------------------------------------------------------------------------
    def load_seen(self) -> dict[str, str]:
        seen: dict[str, str] = {}
        path = self.root / SEEN
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"{path} is not a JSON object")
            seen.update({str(k): str(v) for k, v in data.items()})
        for item in self.read_queue():
            key = item.get("key")
            if isinstance(key, str):
                seen.setdefault(key, str(item.get("run_id", "")))
        return seen

    def save_seen(self, seen: dict[str, str]) -> None:
        _atomic_write(self.root / SEEN, json.dumps(seen, indent=0, sort_keys=True) + "\n")

    # --- queue -----------------------------------------------------------------------------
    def read_queue(self) -> list[dict]:
        path = self.root / QUEUE
        if not path.exists():
            return []
        items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line from a crash; the key is still in seen.json or re-found
        return items

    def append_queue(self, items: Iterable[dict]) -> int:
        lines = [json.dumps(i, ensure_ascii=False, sort_keys=True) + "\n" for i in items]
        if not lines:
            return 0
        path = self.root / QUEUE
        with open(path, "a", encoding="utf-8") as fh:
            fh.writelines(lines)
            fh.flush()
            os.fsync(fh.fileno())
        return len(lines)

    # --- run documents ---------------------------------------------------------------------
    def run_dir(self, run_id: str) -> Path:
        d = self.root / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_json(self, path: Path, data: str) -> None:
        _atomic_write(path, data)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


MANIFEST_VERSION = 1
QUEUE_STATUSES = ("pending", "assigned", "rejected")


def write_capture(root: Path, capture_id: str, manifest: dict, ad: dict) -> Path:
    d = Path(root) / capture_id
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / "ad.json", json.dumps(ad, indent=2, ensure_ascii=False) + "\n")
    _atomic_write(d / "capture.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return d


def load_incident(capture_dir: Path):
    """The contract Incident of one capture folder (same reader as for the URL fetcher)."""
    from ftf.contract import Incident

    manifest = json.loads((Path(capture_dir) / "capture.json").read_text(encoding="utf-8"))
    return Incident.model_validate(manifest["incident"])


def update_dataset(root: Path, name: str, new_incidents: list) -> None:
    """Add incidents to `<root>/dataset.json` (created on first use)."""
    from ftf.contract import Dataset

    path = Path(root) / "dataset.json"
    if path.exists():
        ds = Dataset.from_json(path.read_text(encoding="utf-8"))
        known = {i.id for i in ds.incidents}
        incidents = ds.incidents + [i for i in new_incidents if i.id not in known]
    else:
        incidents = list(new_incidents)
    ds = Dataset(name=name, description="Ads found by `ftf scan`, one incident per ad", incidents=incidents)
    _atomic_write(path, ds.to_json() + "\n")


def set_queue_status(root: Path, keys: Iterable[str], status: str) -> int:
    """Mark queue items (by key) as assigned / rejected / pending; returns how many changed.

    Takes the store lock, so it never interleaves with a running scan.
    """
    if status not in QUEUE_STATUSES:
        raise ValueError(f"status must be one of {QUEUE_STATUSES}")
    wanted = set(keys)
    with Store(root) as store:
        items = store.read_queue()
        changed = 0
        for item in items:
            if item.get("key") in wanted and item.get("status") != status:
                item["status"] = status
                changed += 1
        if changed:
            text = "".join(json.dumps(i, ensure_ascii=False, sort_keys=True) + "\n" for i in items)
            _atomic_write(store.root / QUEUE, text)
        return changed
