"""HACK-93 extra README charts: rarity rank-frequency and E6 sensitivity by layer.

Run on the workbench VM (needs matplotlib and the ftf package in the venv):

    /work/<slug>/venv/bin/python3 -I scripts/make_figures_extra.py \
        --corpus data/corpus/dataset.json \
        --evaluation data/evaluation/sensitivity/sensitivity-linking.json \
        --dataset /data/derived/ingest/organiser/dataset-indicators.json \
        --build-file /data/derived/ingest/organiser/BUILD.txt \
        --main-sha <sha7> --out-dir docs/figures

Writes rarity_rank_frequency.png and sensitivity.png. Aggregates only: no indicator values,
domains or ids reach the charts, the printed summary or the code.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from ftf.contract import Dataset, Layer  # noqa: E402
from ftf.evaluate.hidden import hidden_dataset  # noqa: E402
from ftf.evaluate.sensitivity import decisions, move, weight_keys  # noqa: E402
from ftf.linking.weights import HAND_TABLE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OKABE_ITO = {
    "black": "#000000", "orange": "#E69F00", "sky": "#56B4E9", "green": "#009E73",
    "blue": "#0072B2", "vermillion": "#D55E00", "purple": "#CC79A7",
}
TOP_KINDS = 6
DPI = 150
MOVE_DELTAS = (-2.0, -1.0, 1.0, 2.0)
MOVES = tuple(f"{d:+g}" for d in MOVE_DELTAS)
MOVE_COLOURS = {"-2": OKABE_ITO["blue"], "-1": OKABE_ITO["sky"], "+1": OKABE_ITO["orange"], "+2": OKABE_ITO["vermillion"]}
FOOTER_PREFIX = "build: "


def footer(build_file: Path, main_sha: str) -> str:
    line = build_file.read_text(encoding="utf-8").splitlines()[0].strip()
    if line.startswith(FOOTER_PREFIX):
        line = line[len(FOOTER_PREFIX):]
    return f"{FOOTER_PREFIX}{line}, main {main_sha}"


def add_footer(fig, text: str) -> None:
    fig.text(0.01, 0.01, textwrap.fill(text, 150), fontsize=6.5, color="#555555", va="bottom")


def rarity(corpus: Path) -> dict[str, dict[str, set[str]]]:
    """kind -> value -> distinct capture ids carrying it, over every incident of the corpus."""
    data = json.loads(corpus.read_text(encoding="utf-8"))
    carriers: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for incident in data["incidents"]:
        for ind in incident.get("indicators", []):
            value = ind.get("value")
            if value in (None, ""):
                continue
            carriers[ind["kind"]][value].add(ind["capture_id"])
    return carriers


def plot_rarity(carriers: dict[str, dict[str, set[str]]], out: Path, foot: str) -> None:
    ranked = sorted(carriers, key=lambda k: (-len(carriers[k]), k))[:TOP_KINDS]
    n_values = sum(len(carriers[k]) for k in ranked)
    captures = set().union(*(c for k in ranked for c in carriers[k].values()))
    singles = 0
    fig, ax = plt.subplots(figsize=(9, 5.6))
    palette = [OKABE_ITO[c] for c in ("orange", "sky", "green", "blue", "vermillion", "purple")]
    for kind, colour in zip(ranked, palette):
        counts = sorted((len(v) for v in carriers[kind].values()), reverse=True)
        singles += sum(1 for c in counts if c == 1)
        ranks = range(1, len(counts) + 1)
        ax.loglog(ranks, counts, marker="o", ms=2.5, lw=1.3, color=colour,
                  label=f"{kind} ({len(counts)} values)")
    ax.set_xlabel("rank of a value within its kind (1 = carried by the most captures), log scale")
    ax.set_ylabel("captures carrying the value (count), log scale")
    ax.grid(True, which="major", color="#DDDDDD", lw=0.6)
    ax.legend(title="indicator kind", fontsize=8, title_fontsize=8, frameon=False)
    ax.set_title(
        "most values are rare, a few are everywhere\n"
        f"background corpus batch-0: top {len(ranked)} kinds, n = {n_values} distinct values, "
        f"n = {len(captures)} captures", fontsize=10.5)
    fig.subplots_adjust(bottom=0.2, top=0.87)
    add_footer(fig, foot)
    fig.savefig(out, dpi=DPI, facecolor="white")
    plt.close(fig)
    print(f"rarity: kinds {ranked}; values {n_values}; captures {len(captures)}; "
          f"values carried by one capture {singles} of {n_values} ({100 * singles / n_values:.0f}%)")


def layer_of(key: str) -> str:
    """`kind:<name>` -> the kind's layer in the Linking table; `layer_default:<layer>` -> that layer."""
    kind, _, rest = key.partition(":")
    return HAND_TABLE.kinds[rest].layer.value if kind == "kind" else rest


def sensitivity_shares(dataset: Path, result: dict) -> tuple[dict[str, dict[str, float]], int]:
    """layer -> move -> percent of pair links that flip under a move of any one weight of the layer.

    Re-runs the E6 moves of `ftf.evaluate.sensitivity` on the same dataset (platform ids hidden, as
    the E6 default) and collects the distinct pair links that flip, so a link flipped by two
    weights of one layer counts once. The baseline and every per-weight flip count are checked
    against the E6 JSON (`result`) before anything is plotted.
    """
    ds = hidden_dataset(Dataset.model_validate_json(dataset.read_text(encoding="utf-8")))
    _, base = decisions(ds, HAND_TABLE)
    n_links = len(base["pairs"])
    json_links = sum(result["baseline"]["pair_links"].values())
    json_keys = {row["weight"]: row for row in result["ranking"]}
    keys = weight_keys(ds, HAND_TABLE)
    if n_links != json_links or set(keys) != set(json_keys):
        raise SystemExit(f"recomputed baseline ({n_links} links, {len(keys)} weights) does not match E6 JSON "
                         f"({json_links} links, {len(json_keys)} weights): wrong dataset?")
    flipped: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for key in keys:
        for d in MOVE_DELTAS:
            _, moved = decisions(ds, move(HAND_TABLE, key, d))
            pairs = base["pairs"] ^ moved["pairs"]
            if len(pairs) != json_keys[key]["by_move"][f"{d:+g}"]["pair_flips_total"]:
                raise SystemExit(f"flip count for {key} at {d:+g} differs from E6 JSON: wrong dataset?")
            flipped[layer_of(key)][f"{d:+g}"] |= pairs
    layers = [l.value for l in Layer if l.value in flipped]
    shares = {layer: {m: 100 * len(flipped[layer][m]) / n_links for m in MOVES} for layer in layers}
    return shares, n_links


def plot_sensitivity(shares: dict[str, dict[str, float]], n_links: int, out: Path, foot: str) -> None:
    layers = list(shares)
    width = 0.2
    fig, ax = plt.subplots(figsize=(9, 5.6))
    for i, move in enumerate(MOVES):
        xs = [j + (i - 1.5) * width for j in range(len(layers))]
        ys = [shares[layer][move] for layer in layers]
        ax.bar(xs, ys, width, color=MOVE_COLOURS[move], label=f"{move} bit")
        for x, y in zip(xs, ys):
            ax.text(x, y + 0.4, f"{y:.0f}", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(range(len(layers)), layers)
    ax.set_xlabel("layer of the moved weight (kind's layer)")
    ax.set_ylabel(f"pair links flipped (% of {n_links} links)")
    ax.set_ylim(0, max(max(v.values()) for v in shares.values()) * 1.2 + 1)
    ax.grid(True, axis="y", color="#DDDDDD", lw=0.6)
    ax.set_axisbelow(True)
    ax.legend(title="weight moved by", fontsize=8, title_fontsize=8, frameon=False, ncols=4)
    ax.set_title(
        "E6 sensitivity: moving one linking weight by 1 or 2 bits\n"
        f"n = {n_links} pair links in the report dataset; flips summed over the layer's weights",
        fontsize=10.5)
    fig.subplots_adjust(bottom=0.2, top=0.87)
    add_footer(fig, foot)
    fig.savefig(out, dpi=DPI, facecolor="white")
    plt.close(fig)
    for layer in layers:
        print("sensitivity", layer, {m: round(v, 1) for m, v in shares[layer].items()})
    print(f"sensitivity: n links {n_links}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=ROOT / "data/corpus/dataset.json")
    parser.add_argument("--evaluation", type=Path, default=ROOT / "data/evaluation/sensitivity/sensitivity-linking.json")
    parser.add_argument("--dataset", type=Path, default=Path("/data/derived/ingest/organiser/dataset-indicators.json"),
                        help="organiser dataset the E6 run used (checked against the E6 JSON)")
    parser.add_argument("--build-file", type=Path, default=Path("/data/derived/ingest/organiser/BUILD.txt"))
    parser.add_argument("--main-sha", required=True, help="7-character sha of main the charts were built from")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs/figures")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    foot = footer(args.build_file, args.main_sha)
    plot_rarity(rarity(args.corpus), args.out_dir / "rarity_rank_frequency.png", foot)
    result = json.loads(args.evaluation.read_text(encoding="utf-8"))
    shares, n_links = sensitivity_shares(args.dataset, result)
    plot_sensitivity(shares, n_links, args.out_dir / "sensitivity.png", foot)


if __name__ == "__main__":
    main()
