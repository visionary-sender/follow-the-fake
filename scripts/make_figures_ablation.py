"""README figure for HACK-93: leave-one-layer-out ablation of the Linking engine on E1.

Needs matplotlib (not in the project venv): `pip install matplotlib`. Run on the workbench VM.

    python scripts/make_figures_ablation.py --dataset DATASET.json [--spec SPEC.json] \
        --build-file BUILD.txt --main-sha SHA7 --out-dir docs/figures

Starts from the E1 setting (`ftf eval hidden`): platform ids hidden, Linking engine, scored on
the E1 scoring units with the functions of `ftf.evaluate.hidden`. The reference run uses every
remaining layer; each ablation drops every indicator of one evidence group, links again and is
scored the same way. Writes layer_ablation.png. The chart shows aggregates only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from ftf.contract import Dataset, Indicator, Layer  # noqa: E402
from ftf.evaluate.engine import get_engine  # noqa: E402
from ftf.evaluate.hidden import E1_LEVELS, hidden_dataset, score_levels, unit_map, unit_partition  # noqa: E402
from ftf.evaluate.labels import PLATFORM_LEVELS, build_labels  # noqa: E402
from ftf.evaluate.units import load_spec  # noqa: E402

# Okabe-Ito, colour-blind safe.
BLUE, ORANGE, VERMILION = "#0072B2", "#E69F00", "#D55E00"

TEXT_KINDS = {"ocr_text", "page_text", "text_embedding", "text_template", "keyword", "topic",
              "person_mention", "brand"}
IMAGE_KINDS = {"image_dhash", "image_pdq", "image_phash", "image_embedding"}
FACE_KINDS = {"person_face", "face_embedding"}

# Evidence group -> predicate on an indicator. Platform ids are already hidden in the reference.
GROUPS = {
    "tracking": lambda i: i.layer == Layer.tracking_account,
    "infrastructure": lambda i: i.layer in (Layer.infrastructure, Layer.landing),
    "kit/DOM": lambda i: i.layer == Layer.kit,
    "text": lambda i: i.layer == Layer.creative and i.kind in TEXT_KINDS,
    "image": lambda i: i.layer == Layer.creative and i.kind in IMAGE_KINDS,
    "faces": lambda i: i.layer == Layer.creative and i.kind in FACE_KINDS,
}


def footer_text(build_file: Path, main_sha: str) -> str:
    line = next(ln.strip() for ln in build_file.read_text().splitlines() if ln.startswith("build:"))
    return f"{line}, main {main_sha}"


def drop(ds: Dataset, pred) -> tuple[Dataset, int]:
    out = ds.model_copy(deep=True)
    removed = 0
    for inc in out.incidents:
        keep: list[Indicator] = [i for i in inc.indicators if not pred(i)]
        removed += len(inc.indicators) - len(keep)
        inc.indicators = keep
    return out, removed


def score(ds: Dataset, linked_input: Dataset, labels: dict, units: dict[str, str]) -> dict:
    """E1 rows for one engine input, exactly as run_hidden scores them."""
    clustered = get_engine("linking").link_dataset(linked_input)
    relations = sorted({PLATFORM_LEVELS[lv][1].value for lv in E1_LEVELS})
    parts = {rel: unit_partition(clustered, rel, ds, units) for rel in relations}
    rows = {r["level"]: r for r in score_levels(labels, parts, units, E1_LEVELS)}
    camp, owner = rows["campaign"], rows["owner"]
    return {
        "campaign": (camp["pairwise"]["tp"], camp["pairwise"]["true_pairs"]),
        "operator": (owner["pairwise"]["tp"], owner["pairwise"]["true_pairs"]),
        "mnl": (camp["must_not_link"]["violated"], camp["must_not_link"]["pairs"]),
    }


def chart(results: dict[str, dict], removed: dict[str, int], n_units: int, out: Path, footer: str,
          reference_ok: bool) -> None:
    names = list(results)
    series = [("campaign", "campaign recall (pairs)", BLUE),
              ("operator", "owner/operator recall (pairs)", ORANGE),
              ("mnl", "must-not-link violations (pairs)", VERMILION)]
    fig, ax = plt.subplots(figsize=(10, 5.4), dpi=150, facecolor="white")
    width = 0.27
    for s, (key, label, colour) in enumerate(series):
        xs = [i + (s - 1) * width for i in range(len(names))]
        fr = [results[n][key][0] / results[n][key][1] if results[n][key][1] else 0 for n in names]
        bars = ax.bar(xs, fr, width, label=label, color=colour)
        for b, n in zip(bars, names):
            k, tot = results[n][key]
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.015, f"{k}/{tot}",
                    ha="center", va="bottom", fontsize=7.5)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([n if n == "none" else f"{n}\n(-{removed[n]} ind.)" for n in names], fontsize=9)
    ax.set_xlabel("evidence layer removed (platform ids hidden in every run)")
    ax.set_ylabel("share of labelled pairs (k/n)")
    ax.set_ylim(0, 1.15)
    ax.legend(loc="upper right", fontsize=8, ncol=3, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    ref = results["none"]
    ax.set_title(f"Leave-one-layer-out ablation, Linking engine on E1 ({n_units} scoring units; "
                 f"n={ref['campaign'][1]} campaign pairs, {ref['operator'][1]} owner pairs, "
                 f"{ref['mnl'][1]} must-not-link pairs)", fontsize=9.5)
    note = footer if reference_ok else footer + "  |  WARNING: reference run does not reproduce E1 (11/11, 6/22, 0/37)"
    fig.text(0.01, 0.01, note, fontsize=6.5, color="#555555")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(out, dpi=150, facecolor="white")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--spec", type=Path, default=None)
    ap.add_argument("--build-file", type=Path, required=True)
    ap.add_argument("--main-sha", required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("docs/figures"))
    args = ap.parse_args()

    ds = Dataset.model_validate(json.loads(args.dataset.read_text()))
    spec = load_spec(args.spec)
    labels = build_labels(ds, spec)
    units = unit_map(ds, spec)
    n_units = len({units.get(c.id, c.id) for inc in ds.incidents for c in inc.captures if not c.duplicate_of})
    hidden = hidden_dataset(ds)

    results = {"none": score(ds, hidden, labels, units)}
    removed = {"none": 0}
    for name, pred in GROUPS.items():
        ablated, removed[name] = drop(hidden, pred)
        results[name] = score(ds, ablated, labels, units)
    reference_ok = (results["none"]["campaign"] == (11, 11) and results["none"]["operator"] == (6, 22)
                    and results["none"]["mnl"] == (0, 37))
    for name, r in results.items():
        print(f"{name:>15} -{removed[name]:>5}  campaign {r['campaign'][0]}/{r['campaign'][1]}  "
              f"operator {r['operator'][0]}/{r['operator'][1]}  mnl {r['mnl'][0]}/{r['mnl'][1]}")
    print("reference reproduces E1:", reference_ok)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    chart(results, removed, n_units, args.out_dir / "layer_ablation.png",
          footer_text(args.build_file, args.main_sha), reference_ok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
