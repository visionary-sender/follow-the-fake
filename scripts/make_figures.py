"""README figures: evidence separation and baseline vs model.

Needs matplotlib (not in project venv): `pip install matplotlib`. Run on workbench VM.

python scripts/make_figures.py --dataset DATASET.json --labels labels.json
    --eval-dir EVAL_DIR --build-file BUILD.txt --main-sha SHA7 --out-dir docs/figures

Writes evidence_separation.png baseline_vs_model.png. Charts show aggregates only.
"""

from __future__ import annotations

import argparse
import json
import random
import textwrap
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
BLUE, ORANGE, BLACK, GREY = "#0072B2", "#E69F00", "#000000", "#999999"
SEED = 93
RANDOM_PAIRS = 200


def footer_text(build_file: Path, main_sha: str) -> str:
    line = next(ln.strip() for ln in build_file.read_text().splitlines() if ln.startswith("build:"))
    return f"{line}, main {main_sha}"


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def evidence_separation(dataset: Path, labels: dict, out: Path, footer: str) -> None:
    ds, _hidden = hide_platform_ids(Dataset.model_validate(json.loads(dataset.read_text())))
    ctx = ScoringContext.from_dataset(ds)
    tau = threshold(ctx, Relation.campaign)
    canon = {cid: meta["canonical"] for cid, meta in labels["captures"].items()}
    # Scoring units as the evaluator counts them (same-incident captures collapse, in E1).
    units = unit_map(ds, load_spec())

    def unit(c: str) -> str:
        c = canon.get(c, c)
        return units.get(c, c)

    def unit_pairs(raw: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[str, str]]:
        """Unit pair -> one representative capture pair; pairs inside one unit are dropped."""
        out_map: dict[tuple[str, str], tuple[str, str]] = {}
        for a, b in raw:
            ua, ub = unit(a), unit(b)
            if ua != ub:
                out_map.setdefault(_pair(ua, ub), (canon.get(a, a), canon.get(b, b)))
        return out_map

    # Should-link: every pair inside an E1 campaign label group.
    campaign_groups = [g["members"] for g in labels["groups"] if g["level"] == "campaign"]
    should_map = unit_pairs([p for g in campaign_groups for p in combinations(g, 2)])
    must_map = unit_pairs(
        [(p["a"], p["b"]) for p in labels["must_not_link"] if p["relation"] == "campaign"]
    )

    # Random: seeded sample of capture pairs from different truth groups (singletons own group).
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

    def bits(pairs: list[tuple[str, str]]) -> list[float]:
        return [score_pair(ctx, a, b, relation=Relation.campaign).bits for a, b in pairs]

    should_bits, must_bits, random_bits = bits(should), bits(must), bits(random_set)
    above = sum(1 for x in random_bits if x > tau)
    n_random = len(random_bits)

    # (strip title, values, colour); strips are the top three panels, histograms the bottom three.
    panels = [
        (f"campaign should-link pairs (n={len(should)})", should_bits, BLUE),
        (f"campaign must-not-link pairs (n={len(must)})", must_bits, ORANGE),
        (f"different-group random pairs (n={n_random}): {above} of {n_random} above τ",
         random_bits, GREY),
    ]
    all_values = [x for _, vals, _ in panels for x in vals]
    lo, hi = min(all_values), max(all_values)
    pad = (hi - lo) * 0.05
    xlim = (lo - pad, hi + pad)

    fig = plt.figure(figsize=(9, 8.2), dpi=150, facecolor="white")
    gs = fig.add_gridspec(6, 1, height_ratios=[1, 1, 1, 1.6, 1.6, 1.6], hspace=0.55)
    first = fig.add_subplot(gs[0])
    axes = [first] + [fig.add_subplot(gs[i], sharex=first) for i in range(1, 6)]
    strips, hists = axes[:3], axes[3:]
    first.set_xlim(*xlim)

    rng_jit = random.Random(SEED)
    for ax, (title, vals, colour) in zip(strips, panels):
        ys = [rng_jit.uniform(-0.25, 0.25) for _ in vals]
        ax.scatter(vals, ys, s=14, color=colour, alpha=0.8, edgecolors="none")
        ax.set_yticks([])
        ax.set_ylim(-0.5, 0.5)
        ax.set_title(title, loc="left", fontsize=9)
        ax.axvline(tau, color=BLACK, ls="--", lw=1)
        ax.tick_params(labelbottom=False, labelsize=8)

    for ax, (_title, vals, colour) in zip(hists, panels):
        ax.hist(vals, bins=40, range=xlim, color=colour, alpha=0.85)
        ax.axvline(tau, color=BLACK, ls="--", lw=1)
        ax.set_ylabel("pairs (count)", fontsize=8)
        ax.tick_params(labelbottom=False, labelsize=8)
    hists[-1].tick_params(labelbottom=True)
    hists[-1].set_xlabel("evidence under campaign relation (bits); dashed line = τ", fontsize=8)

    strips[0].text(tau, 0.84, f" τ = {tau:g} bits (campaign link threshold)", fontsize=8,
                   transform=strips[0].get_xaxis_transform(), va="bottom")
    fig.suptitle(
        f"Evidence separation, platform ids hidden (n={len(should) + len(must) + n_random} pairs)",
        fontsize=11, x=0.02, ha="left",
    )
    fig.text(0.02, 0.008, "\n".join(textwrap.wrap(footer, 120)), fontsize=6.5, color="#444444")
    fig.subplots_adjust(left=0.08, right=0.97, top=0.92, bottom=0.08)
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)
    print(f"evidence: tau={tau:.3f} should={len(should)} must={len(must)} "
          f"random={n_random} random_above_tau={above} -> {out}")


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _campaign(report: dict) -> dict:
    return next(lv for lv in report["levels"] if lv["level"] == "campaign")


def baseline_vs_model(eval_dir: Path, out: Path, footer: str) -> None:
    # (recall, precision, label, colour, label offset in points, label alignment)
    points: list[tuple[float, float, str, str, tuple[int, int], str]] = []

    # Label offsets are in points from the marker; they keep the labels of the two
    # points at recall 1.0 and 0.875 (precision 1.0) apart.
    for engine, colour, offset, align in (
        ("baseline", ORANGE, (-8, 0), "right"),
        ("linking", BLUE, (4, 14), "right"),
    ):
        lv = _campaign(_load(eval_dir / "hidden" / f"hidden-{engine}.json"))["pairwise"]
        points.append((lv["recall"], lv["precision"],
                       f"E1 {engine}: {lv['tp']}/{lv['true_pairs']} pairs found\n"
                       f"precision {lv['tp']}/{lv['predicted_pairs']}",
                       colour, offset, align))
    n_e1 = lv["true_pairs"]

    for engine, colour, offset, align in (
        ("baseline", ORANGE, (-8, 0), "right"),
        ("linking", BLUE, (-8, -16), "right"),
    ):
        row = next(r for r in _load(eval_dir / "openset" / f"{engine}.json")["summary"]
                   if r["protocol"] == "loo_item" and r["mode"] == "full")
        assigned = row["correct"] + row["wrong"]
        points.append((row["correct"] / row["items"],
                       row["correct"] / assigned if assigned else 0.0,
                       f"E2 {engine}, leave-one-incident-out:\n"
                       f"{row['correct']}/{row['items']} linked correctly, "
                       f"{row['correct']}/{assigned} assigned right",
                       colour, offset, align))
    n_e2 = row["items"]

    fig, ax = plt.subplots(figsize=(8, 6), dpi=150, facecolor="white")
    for rec, prec, label, colour, offset, align in points:
        ax.scatter(rec, prec, s=70, color=colour, edgecolors=BLACK, linewidths=0.5, zorder=3)
        ax.annotate(label, (rec, prec), xytext=offset, textcoords="offset points",
                    fontsize=8, ha=align, va="center")
        print(f"  point recall={rec:.3f} precision={prec:.3f} {label}")
    ax.set_xlim(-0.05, 1.12)
    ax.set_ylim(-0.05, 1.12)
    ax.set_xlabel("recall (share of labelled items found, 0-1)")
    ax.set_ylabel("precision (share of assigned links correct, 0-1)")
    ax.grid(alpha=0.3)
    ax.set_title(f"Baseline vs linker: E1 campaign pairs (n={n_e1}) and "
                 f"E2 leave-one-incident-out (n={n_e2})", fontsize=10, loc="left")
    fig.text(0.02, 0.008, "\n".join(textwrap.wrap(footer, 120)), fontsize=6.5, color="#444444")
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
