"""`ftf calibrate-similarity`: fit the content-view calibrations the ledger reads.

No labels are needed. Each dataset is grouped by the ledger itself (`ftf link`, identifier
evidence only, content views off, so a view never labels its own training pairs), under the
campaign and operator relations:

* positives: two captures of different incidents in one campaign group;
* null: two captures of different incidents in different campaign groups *and* different operator
  groups (the cross-operator criterion: two campaigns of one operator may share creatives, so
  they belong to neither side).

Pairs are compared on every view (`similarity.VIEWS`) both captures carry; `Calibration.fit` turns
the two samples into a capped similarity-to-bits map (logistic with enough positives, tail-LR
otherwise). The output holds every view's calibration and its quality (AUC, Cllr, Cllr_min and the
similarity that earns 1, 2 and 4 bits); the same quality figures go to a separate report.

The dataset is always re-linked here, with content views off and every view kind at 0 bits (an
equal perceptual hash is a campaign-evidence kind of the hand table), so neither a view nor an
earlier content-view run helps form the groups that label its pairs. The null is sampled
(`--max-null`, seeded) when a view has more pairs than that.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ftf.contract.models import Dataset, Layer, Relation, linkable
from ftf.linking.backend import Backend
from ftf.linking.grouping import canonical_map, link_dataset
from ftf.linking.ledger import ScoringContext
from ftf.linking.weights import HAND_TABLE, KindWeight, WeightTable
from ftf.linking.similarity import (
    CALIBRATION_FORMAT,
    DEFAULT_CAP_BITS,
    FACE_KIND,
    MIN_POSITIVES,
    VIEWS,
    Calibration,
    DatasetVectorStore,
    save_calibrations,
    similarity,
)

DEFAULT_MAX_NULL = 50_000
GROUP_RELATIONS = (Relation.campaign, Relation.operator)


def _blind_table() -> WeightTable:
    """The hand table with every content-view kind at 0 bits."""
    kinds = dict(HAND_TABLE.kinds)
    for kind in VIEWS:
        layer = kinds[kind].layer if kind in kinds else Layer.creative
        kinds[kind] = KindWeight(layer, 0.0)
    return replace(HAND_TABLE, kinds=kinds)


def _groups(ds: Dataset) -> tuple[Dataset, dict[Relation, dict[str, str]]]:
    """(linked dataset, relation -> canonical capture -> group id), grouped blind to the content views."""
    ds = link_dataset(ds, GROUP_RELATIONS, backend=Backend.from_context(ScoringContext.from_dataset(ds, _blind_table())))
    canon = canonical_map(ds)
    out: dict[Relation, dict[str, str]] = {r: {} for r in GROUP_RELATIONS}
    for camp in ds.campaigns:
        if camp.relation in out:
            for cap in camp.capture_ids:
                out[camp.relation].setdefault(canon.get(cap, cap), camp.id)
    return ds, out


def _pairs(n: int, limit: int, rng: random.Random) -> list[tuple[int, int]]:
    """All index pairs i < j of n items, or `limit` distinct ones drawn at random."""
    total = n * (n - 1) // 2
    if total <= limit:
        return [(i, j) for i in range(n) for j in range(i + 1, n)]
    seen: set[tuple[int, int]] = set()
    while len(seen) < limit:
        i, j = rng.randrange(n), rng.randrange(n)
        if i != j:
            seen.add((min(i, j), max(i, j)))
    return sorted(seen)


def collect(
    ds: Dataset, store: DatasetVectorStore, max_null: int = DEFAULT_MAX_NULL, seed: int = 0
) -> dict[str, tuple[list[float], list[float]]]:
    """View kind -> (null similarities, positive similarities) of one dataset."""
    ds, groups = _groups(ds)
    canon = canonical_map(ds)
    incident_of = {c.id: i.id for i in ds.incidents for c in i.captures}
    by_cap: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for inc in ds.incidents:
        for ind in inc.indicators:
            if ind.kind in VIEWS and linkable(ind):
                by_cap[canon[ind.capture_id]][ind.kind].append(ind)
    camp, oper = groups[Relation.campaign], groups[Relation.operator]
    rng = random.Random(seed)
    out: dict[str, tuple[list[float], list[float]]] = {}
    for kind in sorted({k for kinds in by_cap.values() for k in kinds}):
        view = VIEWS[kind]
        caps = sorted(c for c, kinds in by_cap.items() if kind in kinds)
        null: list[float] = []
        pos: list[float] = []
        # positives: every same-campaign pair (few); null: all or a sample of the other pairs
        by_group: dict[str, list[str]] = defaultdict(list)
        for c in caps:
            if c in camp:
                by_group[camp[c]].append(c)
        pos_pairs = [(x, y) for members in by_group.values() for i, x in enumerate(members) for y in members[i + 1 :]]
        cand = [(caps[i], caps[j]) for i, j in _pairs(len(caps), max_null, rng)]
        for x, y, side in [(x, y, pos) for x, y in pos_pairs] + [(x, y, null) for x, y in cand]:
            if incident_of[x] == incident_of[y]:
                continue
            if side is null and not (
                x in camp and y in camp and camp[x] != camp[y] and oper.get(x) is not None and oper.get(y) is not None and oper[x] != oper[y]
            ):
                continue
            s, _, _ = similarity(view, by_cap[x][kind], by_cap[y][kind], store)
            if s is not None:
                side.append(s)
        out[kind] = (null, pos)
    return out


def fit_all(
    samples: dict[str, tuple[list[float], list[float]]],
    cap_bits: float = DEFAULT_CAP_BITS,
    min_positives: int = MIN_POSITIVES,
    face_null_same_face: bool = False,
) -> tuple[dict[str, Calibration], dict[str, str]]:
    """(view -> calibration, view -> why it was skipped)."""
    cals: dict[str, Calibration] = {}
    skipped: dict[str, str] = {}
    for kind, (null, pos) in sorted(samples.items()):
        if not null:
            skipped[kind] = "no cross-campaign, cross-operator pair with a usable value"
            continue
        cal = Calibration.fit(null, pos, cap_bits, view=kind, min_positives=min_positives)
        if kind == FACE_KIND:
            cal.null_same_face = face_null_same_face
        cals[kind] = cal
    return cals, skipped


def register_calibrate(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "calibrate-similarity",
        help="fit the content-view calibrations (similarity -> bits) from a dataset's own groups",
        description="Fit one capped calibration per content view (text/image embeddings, perceptual hashes, "
        "MinHash) on DATASET: positives are same-campaign pairs, the null cross-campaign, cross-operator pairs, "
        "both from `ftf link` (run here if DATASET is not linked). Writes the calibrations for "
        "`--content-views` and a quality report (AUC, Cllr, Cllr_min, similarity at 1/2/4 bits).",
    )
    parser.add_argument("datasets", type=Path, nargs="+", metavar="DATASET.json",
                        help="contract dataset(s); several are pooled per view")
    parser.add_argument("--vectors-dir", type=Path, action="append", metavar="DIR",
                        help="extra directories holding vectors/ stores (repeatable; each dataset's own directory is always searched first)")
    parser.add_argument("-o", "--out", type=Path, required=True, metavar="calibration.json")
    parser.add_argument("--report", type=Path, metavar="quality.json",
                        help="quality report (default: next to --out, ending .quality.json)")
    parser.add_argument("--max-null", type=int, default=DEFAULT_MAX_NULL, help="null pairs per view and dataset (default: %(default)s)")
    parser.add_argument("--cap-bits", type=float, default=DEFAULT_CAP_BITS, help="cap per view in bits (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--face-null-same-face", action="store_true",
                        help="the null holds other scams showing the same face: lift the low face cap")
    parser.set_defaults(func=run_calibrate)


def run_calibrate(args: argparse.Namespace) -> int:
    if args.max_null <= 0 or args.cap_bits <= 0:
        print("ftf calibrate-similarity: --max-null and --cap-bits must be positive", file=sys.stderr)
        return 2
    pooled: dict[str, tuple[list[float], list[float]]] = {}
    for path in args.datasets:
        try:
            ds = Dataset.from_json(path.read_bytes())
        except (OSError, ValidationError) as exc:
            print(f"ftf calibrate-similarity: {path}: {exc}", file=sys.stderr)
            return 2
        store = DatasetVectorStore(path.parent, extra_dirs=args.vectors_dir or ())
        for kind, (null, pos) in collect(ds, store, args.max_null, args.seed).items():
            n, p = pooled.setdefault(kind, ([], []))
            n.extend(null)
            p.extend(pos)
    cals, skipped = fit_all(pooled, args.cap_bits, face_null_same_face=args.face_null_same_face)
    if not cals:
        print("ftf calibrate-similarity: no content view could be calibrated: "
              + ("; ".join(f"{k}: {v}" for k, v in skipped.items()) or "no view indicators in the input"), file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    save_calibrations(cals, args.out)
    report_path: Path = args.report or args.out.with_name(args.out.stem + ".quality.json")
    report = {
        "format": CALIBRATION_FORMAT + "/quality",
        "inputs": [str(p) for p in args.datasets],
        "views": {k: c.quality for k, c in sorted(cals.items())},
        "skipped": skipped,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{len(cals)} views -> {args.out} (quality: {report_path})")
    for k, c in sorted(cals.items()):
        q = c.quality
        at = ", ".join(f"{b}b@{_fmt(v)}" for b, v in q["sim_at_bits"].items())
        print(f"  {k}: {c.method}, n_null={c.n_null}, n_pos={c.n_pos}, auc={_fmt(q.get('auc'))}, "
              f"cllr={_fmt(q.get('cllr'))}, cllr_min={_fmt(q.get('cllr_min'))}, {at}")
    for k, why in skipped.items():
        print(f"  {k}: skipped ({why})")
    return 0


def _fmt(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.3f}"
