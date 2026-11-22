"""README figures for HACK-93: evidence separation and baseline vs model.

Needs matplotlib (not in the project venv): `pip install matplotlib`. Run on the workbench VM.

    python scripts/make_figures.py --dataset DATASET.json --labels labels.json \
        --eval-dir EVAL_DIR --build-file BUILD.txt --main-sha SHA7 --out-dir docs/figures

Writes evidence_separation.png and baseline_vs_model.png. Charts show aggregates only.
"""

from __future__ import annotations

import argparse
import json
import random
from itertools import combinations
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from ftf.contract.models import Dataset, Relation  # noqa: E402
from ftf.evaluate.hidden import unit_map  # noqa: E402
from ftf.evaluate.leak import hide_platform_ids  # noqa: E402
from ftf.evaluate.units import load_spec  # noqa: E402
from ftf.linking.ledger import ScoringContext, score_pair, threshold  # noqa: E402

# Okabe-Ito, colour-blind safe.
BLUE, ORANGE, SKY, BLACK, GREY = "#0072B2", "#E69F00", "#56B4E9", "#000000", "#999999"
SEED = 93
RANDOM_PAIRS = 200


def footer_text(build_file: Path, main_sha: str) -> str:
    line = next(ln.strip() for ln in build_file.read_text().splitlines() if ln.startswith("build:"))
    return f"{line}, main {main_sha}"


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def evidence_separation(dataset: Path, labels: dict, out: Path, footer: str) -> None:
    ds, _hidden_values = hide_platform_ids(Dataset.model_validate(json.loads(dataset.read_text())))
    ctx = ScoringContext.from_dataset(ds)
    tau = threshold(ctx, Relation.campaign)
    canon = {cid: meta["canonical"] for cid, meta in labels["captures"].items()}
    # Scoring units as the evaluator counts them (same-incident captures collapse, as in E1).
    units = unit_map(ds, load_spec())

    def unit(c: str) -> str:
        c = canon.get(c, c)
        return units.get(c, c)

    def unit_pairs(raw: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[str, str]]:
        """Unit pair -> one representative capture pair; pairs inside one unit are dropped."""
        out: dict[tuple[str, str], tuple[str, str]] = {}
        for a, b in raw:
            ua, ub = unit(a), unit(b)
            if ua != ub:
                out.setdefault(_pair(ua, ub), (canon.get(a, a), canon.get(b, b)))
        return out

    # Should-link: every pair inside an E1 campaign label group (the campaign level).
    campaign_groups = [g["members"] for g in labels["groups"] if g["level"] == "campaign"]
    should_map = unit_pairs([p for g in campaign_groups for p in combinations(g, 2)])
    must_map = unit_pairs([(p["a"], p["b"]) for p in labels["must_not_link"] if p["relation"] == "campaign"])

    # Random: seeded sample of capture pairs from different truth groups (singletons are their own group).
    truth: dict[str, str] = {}
    for gi, g in enumerate(campaign_groups):
        for m in g:
            truth[unit(m)] = f"group{gi}"
    rng = random.Random(SEED)
    reps = sorted({canon.get(c, c) for c in labels["captures"]})
    random_map: dict[tuple[str, str], tuple[str, str]] = {}
    while len(random_map) < RANDOM_PAIRS:
        a, b = rng.sample(reps, 2)
        ua, ub = unit(a), unit(b)
        p = _pair(ua, ub)
        if ua == ub or p in should_map or p in must_map or p in random_map:
            continue
        if truth.get(ua, ua) == truth.get(ub, ub):
            continue
        random_map[p] = (a, b)

    should = list(should_map.values())
    must = list(must_map.values())
    random_set = list(random_map.values())
    sets = {
        f"campaign should-link pairs (n={len(should)})": (should, BLUE),
        f"campaign must-not-link pairs (n={len(must)})": (must, ORANGE),
        f"different-group random pairs (n={len(random_set)})": (random_set, GREY),
    }
    values = {
        name: [score_pair(ctx, a, b, relation=Relation.campaign).bits for a, b in pairs]
        for name, (pairs, _c) in sets.items()
    }

    fig = plt.figure(figsize=(9, 8.2), dpi=150, facecolor="white")
    gs = fig.add_gridspec(6, 1, height_ratios=[1, 1, 1, 1.6, 1.6, 1.6], hspace=0.12)
    strips = [fig.add_subplot(gs[i]) for i in range(3)]
    hists = [fig.add_subplot(gs[i + 3], sharex=strips[0]) for i in range(3)]
    rng_jit = random.Random(SEED)
    lo = min(min(v) for v in values.values())
    hi = max(max(v) for v in values.values())
    pad = (hi - lo) * 0.05
    for ax, (name, (_pairs, colour)) in zip(strips, sets.items()):
        v = values[name]
        ys = [rng_jit.uniform(-0.25, 0.25) for _ in v]
        ax.scatter(v, ys, s=14, color=colour, alpha=0.8, edgecolors="none")
        ax.set_yticks([])
        ax.set_ylim(-0.5, 0.5)
        ax.set_title(name, loc="left", fontsize=9)
        ax.axvline(tau, color=BLACK, ls="--", lw=1)
        ax.tick_params(labelbottom=False)
    for ax, (name, (_pairs, colour)) in zip(hists, sets.items()):
        ax.hist(values[name], bins=40, range=(lo - pad, hi + pad), color=colour, alpha=0.85)
        ax.axvline(tau, color=BLACK, ls="--", lw=1)
        ax.set_ylabel("pairs (count)", fontsize=8)
        ax.tick_params(labelsize=8)
    strips[0].text(tau, 0.55, f" τ = {tau:g} bits (campaign link threshold)", fontsize=8,
                   transform=strips[0].get_xaxis_transform(), va="bottom")
    hists[-1].set_xlabel("evidence for the pair under the campaign relation (bits)")
    fig.suptitle(
        f"Evidence separation with platform ids hidden (n={len(should) + len(must) + len(random_set)} pairs)",
        fontsize=11, x=0.02, ha="left",
    )
    fig.text(0.02, 0.012, footer, fontsize=7, color="#444444")
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"evidence: tau={tau} should={len(should)} must={len(must)} random={len(random_set)} -> {out}")


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _campaign(report: dict) -> dict:
    return next(lv for lv in report["levels"] if lv["level"] == "campaign")


def baseline_vs_model(eval_dir: Path, out: Path, footer: str) -> None:
    points: list[tuple[float, float, str, str]] = []  # (recall, precision, label, colour)

    for engine, colour, mark in (("baseline", ORANGE, "E1"), ("linking", BLUE, "E1")):
        lv = _campaign(_load(eval_dir / "hidden" / f"hidden-{engine}.json"))["pairwise"]
        points.append((lv["recall"], lv["precision"],
                       f"{mark} {engine} {lv['tp']}/{lv['true_pairs']} "
                       f"(P {lv['tp']}/{lv['predicted_pairs']})", colour))

    for engine, colour in (("baseline", ORANGE), ("linking", BLUE)):
        row = next(r for r in _load(eval_dir / "openset" / f"{engine}.json")["summary"]
                   if r["protocol"] == "loo_item" and r["mode"] == "full")
        assigned = row["correct"] + row["wrong"]
        points.append((row["correct"] / row["items"], row["correct"] / assigned if assigned else 0.0,
                       f"E2 {engine} {row['correct']}/{row['items']} "
                       f"(assigned {row['correct']}/{assigned})", colour))

    cand = eval_dir / "candidates" / "main-5f9da73" / "hidden" / "hidden-linking.json"
    if cand.exists():
        lv = _campaign(_load(cand))["pairwise"]
        points.append((lv["recall"], lv["precision"],
                       f"E1 linking main-5f9da73 (candidate) {lv['tp']}/{lv['true_pairs']}", SKY))

    fig, ax = plt.subplots(figsize=(8, 6), dpi=150, facecolor="white")
    for rec, prec, label, colour in points:
        ax.scatter(rec, prec, s=70, color=colour, edgecolors=BLACK, linewidths=0.5, zorder=3)
        ax.annotate(label, (rec, prec), xytext=(7, 5), textcoords="offset points", fontsize=8)
    ax.set_xlim(0, 1.08)
    ax.set_ylim(0, 1.08)
    ax.set_xlabel("recall (share of labelled items found, 0-1)")
    ax.set_ylabel("precision (share of assigned links correct, 0-1)")
    ax.grid(alpha=0.3)
    ax.set_title(f"Baseline vs linker: E1 campaign pairs (n=11) and E2 leave-one-item-out (n=8)",
                 fontsize=10, loc="left")
    fig.text(0.02, 0.012, footer, fontsize=7, color="#444444")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"baseline_vs_model: {len(points)} points -> {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--eval-dir", type=Path, required=True)
    p.add_argument("--build-file", type=Path, required=True)
    p.add_argument("--main-sha", required=True)
    p.add_argument("--out-dir", type=Path, default=Path("docs/figures"))
    p.add_argument("--only", choices=["evidence", "baseline"], default=None)
    a = p.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    footer = footer_text(a.build_file, a.main_sha)
    if a.only in (None, "evidence"):
        evidence_separation(a.dataset, _load(a.labels), a.out_dir / "evidence_separation.png", footer)
    if a.only in (None, "baseline"):
        baseline_vs_model(a.eval_dir, a.out_dir / "baseline_vs_model.png", footer)


if __name__ == "__main__":
    main()
