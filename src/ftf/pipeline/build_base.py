"""`ftf build-base`: the organiser's dataset folder in, a campaign base out.

Stages run in this fixed order, each through the owning project's published call:

| stage | owner | call |
|---|---|---|
| ingest | Ingestion | `ftf.ingest.ingest([raw], root=raw, name=...) -> Dataset` (with indicators) |
| features | Features | `ftf.features.pipeline.extract(incident, raw, out) -> Incident` |
| link | Linking | `ftf.linking.link_dataset(ds) -> Dataset` |

Features run before linking because `extract` refuses datasets that already carry links. A
stage whose package is not installed yet is skipped with a note. If no linkable indicator
(`ftf.contract.linkable`) is left after ingest and features, the command exits 1 and writes no
`dataset.json`, so a silently empty base never reaches the demo. The base bundle is one directory:

    <out>/dataset.json             contract Dataset (FTF_BASE points here)
    <out>/vectors/                 embeddings referenced by VectorRef.store (written by features)
    <out>/enrichment/cache.json    domain enrichment cache (FTF_ENRICH_CACHE; optional)
    <out>/build-report.json        which stages ran or were skipped

Captured files are hostile: no stage here opens them; the owning libraries read them as bytes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ftf.contract import Dataset, linkable
from ftf.pipeline.stages import _optional

DEFAULT_NAME = "campaign-base"
REPORT_NAME = "build-report.json"


class EmptyBaseError(ValueError):
    """The dataset has no linkable indicator after ingest and features: refuse to write a base."""


@dataclass
class StageNote:
    stage: str
    status: str  # "ok" | "skipped"
    note: str


@contextmanager
def _offline_env(offline: bool) -> Iterator[None]:
    """With `offline`, every stage sees FTF_OFFLINE=1 (the shared switch for network lookups)."""
    old = os.environ.get("FTF_OFFLINE")
    if offline:
        os.environ["FTF_OFFLINE"] = "1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("FTF_OFFLINE", None)
        else:
            os.environ["FTF_OFFLINE"] = old


def build_base(
    raw: Path, out: Path, *, offline: bool = False, log: Callable[[str], None] = lambda s: None
) -> tuple[Dataset, list[StageNote]]:
    """Run ingest → features → link on `raw` and return the base; vectors are written to `out`."""
    raw, out = Path(raw).resolve(), Path(out).resolve()
    if not raw.is_dir():
        raise ValueError(f"raw folder not found: {raw}")
    if out == raw or out.is_relative_to(raw):
        raise ValueError("--out must be outside --raw (the organiser folder stays untouched)")
    out.mkdir(parents=True, exist_ok=True)
    notes: list[StageNote] = []

    def note(stage: str, status: str, text: str) -> None:
        notes.append(StageNote(stage, status, text))
        log(f"ftf build-base: {stage}: {status}: {text}")

    with _offline_env(offline):
        ingest = _optional("ftf.ingest", "ingest")
        if ingest is None:
            ds = Dataset(name=DEFAULT_NAME, description="ingestion (ftf.ingest) not installed: no incidents")
            note("ingest", "skipped", "ftf.ingest is not installed; the base has no incidents")
        else:
            ds = ingest([raw], root=raw, name=DEFAULT_NAME)
            if ds.links or ds.campaigns or ds.assignments:
                raise ValueError("ingestion returned links/campaigns/assignments; expected incidents only")
            note("ingest", "ok", f"{len(ds.incidents)} incident(s)")

        extract = _optional("ftf.features.pipeline", "extract")
        if extract is None:
            note("features", "skipped", "ftf.features.pipeline.extract is not installed")
        elif not ds.incidents:
            note("features", "skipped", "no incidents")
        else:
            incidents = [extract(inc, raw, out) for inc in ds.incidents]
            ds = ds.model_copy(update={"incidents": incidents})
            n = sum(len(i.indicators) for i in incidents)
            note("features", "ok", f"{n} indicator(s) on {len(incidents)} incident(s)")

        n_linkable = sum(1 for i in ds.incidents for ind in i.indicators if linkable(ind))
        if n_linkable == 0:
            raise EmptyBaseError(
                f"no linkable indicators after ingest and features ({len(ds.incidents)} incident(s)); "
                "refusing to write an empty campaign base (check the dataset folder and that ingestion "
                "and features are installed)"
            )

        link_dataset = _optional("ftf.linking", "link_dataset")
        if link_dataset is None:
            note("link", "skipped", "ftf.linking.link_dataset is not installed; no links or campaigns")
        else:
            ds = link_dataset(ds)
            note("link", "ok", f"{len(ds.links)} link(s), {len(ds.campaigns)} campaign(s)")

    # Re-validate the whole document: stages may hand back model_copy'd objects.
    return Dataset.from_json(ds.to_json()), notes


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "build-base",
        help="build the campaign base (dataset.json + vectors/) from the organiser's dataset folder",
        description=(
            "Run ingest, features and linking on a dataset folder and write OUT/dataset.json "
            "(use it as FTF_BASE) plus OUT/vectors/. Stages whose package is not installed are "
            "skipped with a note. Exit 1, writing no dataset.json, if no linkable indicator is left after "
            "ingest and features. The folder is read only, never executed."
        ),
    )
    p.add_argument("--raw", type=Path, required=True, metavar="DIR", help="organiser dataset folder (read only)")
    p.add_argument("--out", type=Path, required=True, metavar="DIR", help="base directory to write")
    p.add_argument("--offline", action="store_true", help="no network lookups in any stage (sets FTF_OFFLINE=1)")
    p.add_argument("--force", action="store_true", help="overwrite an existing OUT/dataset.json")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    target = args.out / "dataset.json"
    if target.exists() and not args.force:
        print(f"ftf build-base: {target} exists; pass --force to rebuild", file=sys.stderr)
        return 2
    try:
        ds, notes = build_base(
            args.raw, args.out, offline=args.offline, log=lambda s: print(s, file=sys.stderr)
        )
    except EmptyBaseError as exc:
        print(f"ftf build-base: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ftf build-base: {exc}", file=sys.stderr)
        return 2
    _write_atomic(target, ds.to_json() + "\n")
    report = {"dataset": "dataset.json", "incidents": len(ds.incidents), "stages": [asdict(n) for n in notes]}
    _write_atomic(args.out / REPORT_NAME, json.dumps(report, indent=2) + "\n")
    print(target)
    return 0
