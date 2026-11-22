"""Static PNG of one organiser campaign: incidents as nodes, shared evidence as edges.

Reads a contract `Dataset` JSON (a built base document) and draws one campaign of relation
`campaign`: the one whose incident pairs carry the most varied shared evidence, or the one given
with `--campaign`. Nodes are labelled by case folder number. An edge joins two incidents; its label
gives the pair's total shared bits and its two strongest evidence layers, and its width scales with
those total bits. Each indicator counts once per pair. Collector-layer evidence is never drawn, and
no indicator value (URL, domain, id, hash) is drawn. Graph data comes from `ftf.app.relations`;
only the drawing lives here.

    python3 -I scripts/make_campaign_graph.py DATASET.json OUT.png [--campaign ID]

Run it in the project venv with the app and dev extras, on a cloud VM (see docs/contract.md).
Needs the `app` extra (networkx) and the `dev` extra (matplotlib).
"""

from __future__ import annotations

import argparse
import re
import textwrap
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import networkx as nx  # noqa: E402

from ftf.app import relations as R  # noqa: E402
from ftf.contract import Campaign, Dataset, Relation  # noqa: E402

WIDTH_PX = 1200
DPI = 100
NODE_FILL = "#1f4e79"
EDGE_COLOUR = "#7f8c8d"
IDEAL_INCIDENTS = (4, 8)
MAX_EXTRA_EDGES = 3  # strongest pairs drawn beyond the spanning tree, keeps labels readable
FOOTNOTE = (
    "Node: one incident, labelled by case folder number. Edge: shared evidence between two incidents; "
    "label: the pair's total bits and its two strongest layers. Collector-layer evidence is not drawn."
)
FOOTNOTE_WRAP = 150  # characters per footnote line at 9 pt on the 1200 px canvas

PairEvidence = tuple[float, list[tuple[str, float]]]  # total bits, (layer, bits) strongest first


def incident_of(ds: Dataset) -> dict[str, str]:
    """Capture id -> incident id."""
    return {c.id: inc.id for inc in ds.incidents for c in inc.captures}


CASE_FOLDER = re.compile(r"[A-Za-z]{0,2}\d{1,4}")


def case_label(ds: Dataset) -> dict[str, str]:
    """Incident id -> case folder number. Only folder-shaped names are drawn; anything else (URLs,
    keywords, hashes, ad-library ids) becomes a neutral index so no captured value reaches the figure."""
    labels: dict[str, str] = {}
    for n, inc in enumerate(sorted(ds.incidents, key=lambda i: i.id), 1):
        ref = inc.source_ref or ""
        labels[inc.id] = ref if CASE_FOLDER.fullmatch(ref) else f"I{n}"
    return labels


def pair_evidence(ds: Dataset, camp: Campaign, threshold: float) -> dict[tuple[str, str], PairEvidence]:
    """Incident pair -> total shared bits and per-layer bits, from the campaign's relation edges.
    An indicator shared by several capture links of the same pair counts once, at its strongest row."""
    rel_graph = R.relation_graph(ds, camp.relation, threshold)
    members = set(camp.capture_ids)
    inc = incident_of(ds)
    rows: dict[tuple[str, str], dict[tuple, R.Shared]] = defaultdict(dict)
    for u, v, data in rel_graph.edges(data=True):
        if u not in members or v not in members or inc[u] == inc[v]:
            continue
        pair = tuple(sorted((inc[u], inc[v])))
        for e in data["edges"]:
            for s in e.shared:
                kept = rows[pair].get(s.key)
                if kept is None or s.bits > kept.bits:
                    rows[pair][s.key] = s
    out: dict[tuple[str, str], PairEvidence] = {}
    for pair, shared in rows.items():
        per_layer: dict[str, float] = defaultdict(float)
        for s in shared.values():
            per_layer[s.layer] += s.bits
        ranked = sorted(per_layer.items(), key=lambda kv: (-kv[1], kv[0]))
        out[pair] = (sum(per_layer.values()), ranked)
    return out


def pick_campaign(ds: Dataset, threshold: float) -> Campaign:
    """Organiser campaign with the most varied shared evidence: most distinct layers (at least two),
    then 4-8 incidents, then most total bits."""
    found = []
    for camp in ds.campaigns:
        if camp.relation is not Relation.campaign:
            continue
        pairs = pair_evidence(ds, camp, threshold)
        layers = {layer for _, ranked in pairs.values() for layer, _ in ranked}
        if len(layers) < 2:
            continue
        n_inc = len({i for pair in pairs for i in pair})
        ideal = IDEAL_INCIDENTS[0] <= n_inc <= IDEAL_INCIDENTS[1]
        total = sum(t for t, _ in pairs.values())
        found.append((-len(layers), not ideal, -total, camp.id, camp))
    if not found:
        raise SystemExit("no organiser campaign with shared evidence in at least two layers")
    return min(found, key=lambda t: t[:4])[4]


def campaign_graph(ds: Dataset, camp: Campaign, threshold: float) -> nx.Graph:
    """Incident graph of one campaign. Nodes: its incidents. Edges: pairs with shared evidence, kept as
    a maximum spanning tree on total bits plus the strongest further pairs. Graph attributes record the
    incidents with no shared evidence (`isolated`, not drawn) and how many weaker pairs were left out."""
    inc = incident_of(ds)
    labels = case_label(ds)
    full = nx.Graph()
    for cid in sorted(set(camp.capture_ids)):  # sorted: layout must not depend on hash seeds
        full.add_node(inc[cid], label=labels[inc[cid]])
    for (a, b), (total, ranked) in pair_evidence(ds, camp, threshold).items():
        full.add_edge(a, b, bits=total, layers=ranked)
    isolated = sorted(n for n in full.nodes if full.degree(n) == 0)
    full.remove_nodes_from(isolated)

    tree = nx.maximum_spanning_tree(full, weight="bits")
    extra = sorted((e for e in full.edges if not tree.has_edge(*e)), key=lambda e: -full.edges[e]["bits"])
    g = nx.Graph(campaign=camp.id, relation=camp.relation.value)
    g.add_nodes_from(full.nodes(data=True))
    g.add_edges_from(tree.edges(data=True))
    for u, v in extra[:MAX_EXTRA_EDGES]:
        g.add_edge(u, v, **full.edges[u, v])
    g.graph["isolated"] = [labels[inc_id] for inc_id in isolated]
    g.graph["pruned"] = full.number_of_edges() - g.number_of_edges()
    return g


def _layer_text(layer: str) -> str:
    return layer.replace("_", " ")


def _num(x: float) -> str:
    return f"{round(x, 1):g}"


def _edge_text(d: dict) -> str:
    """'38 bits: tracker 22 · image 9': total pair bits, then the two strongest layers."""
    parts = [f"{_layer_text(layer)} {_num(bits)}" for layer, bits in d["layers"][:2]]
    return f"{_num(d['bits'])} bits: " + " · ".join(parts)


NODE_SIZE = 1700
NODE_HALF_PX = 36  # clearance around a node disc, in display pixels
# Edge-label slots tried in order: fraction along the edge, then perpendicular offset.
LABEL_T = (0.5, 0.36, 0.64, 0.24, 0.76, 0.14, 0.86)
LABEL_OFFSET = (0.0, 0.07, -0.07, 0.14, -0.14)


def _place_edge_labels(ax, fig, g: nx.Graph, pos: dict) -> None:
    """Put each edge label in the first free slot that covers no node and no other label. When no slot
    is free the label goes at the midpoint, so it may overlap; the figure still shows every edge."""
    from matplotlib.transforms import Bbox

    renderer = fig.canvas.get_renderer()
    blocked = []
    for n in g.nodes:
        x, y = ax.transData.transform(pos[n])
        blocked.append(Bbox.from_extents(x - NODE_HALF_PX, y - NODE_HALF_PX, x + NODE_HALF_PX, y + NODE_HALF_PX))
    box_style = {"boxstyle": "round,pad=0.3", "fc": "white", "ec": EDGE_COLOUR, "alpha": 0.97}
    for u, v, d in sorted(g.edges(data=True), key=lambda e: -e[2]["bits"]):
        (x0, y0), (x1, y1) = pos[u], pos[v]
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 or 1.0
        nx_, ny_ = -(y1 - y0) / length, (x1 - x0) / length  # unit normal to the edge
        text = _edge_text(d)
        placed = None
        for t in LABEL_T:
            for off in LABEL_OFFSET:
                x = x0 + t * (x1 - x0) + off * nx_
                y = y0 + t * (y1 - y0) + off * ny_
                artist = ax.text(
                    x, y, text, ha="center", va="center", fontsize=10, color="#222222", zorder=5, bbox=box_style
                )
                box = artist.get_window_extent(renderer)
                if not any(box.overlaps(b) for b in blocked):
                    blocked.append(box)
                    placed = artist
                    break
                artist.remove()
            if placed is not None:
                break
        if placed is None:  # no free slot: keep the label at the midpoint rather than drop evidence
            ax.text((x0 + x1) / 2, (y0 + y1) / 2, text, ha="center", va="center", fontsize=10, zorder=5, bbox=box_style)


def layout(g: nx.Graph) -> dict:
    """Largest connected component on a circle; the rest in a row below it (deterministic)."""
    pos: dict = {}
    comps = sorted(nx.connected_components(g), key=lambda c: (-len(c), sorted(c)[0]))
    if comps:
        pos.update(nx.circular_layout(g.subgraph(comps[0])))
    rest = [n for c in comps[1:] for n in sorted(c)]
    for i, n in enumerate(rest):
        x = -1.0 + 2.0 * (i + 0.5) / max(len(rest), 1)
        pos[n] = (x, -1.7)
    return pos


def draw(g: nx.Graph, out: Path) -> None:
    """Render the incident graph at 1200 px wide."""
    pos = layout(g)
    fig, ax = plt.subplots(figsize=(WIDTH_PX / DPI, 8.4), dpi=DPI)
    ax.set_axis_off()
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    ax.set_xlim(min(xs) - 0.3, max(xs) + 0.3)
    ax.set_ylim(min(ys) - 0.3, max(ys) + 0.3)
    max_bits = max((d["bits"] for _, _, d in g.edges(data=True)), default=1.0) or 1.0
    widths = [1.5 + 5.0 * d["bits"] / max_bits for _, _, d in g.edges(data=True)]
    nx.draw_networkx_edges(g, pos, ax=ax, width=widths, edge_color=EDGE_COLOUR)
    nx.draw_networkx_nodes(g, pos, ax=ax, node_size=NODE_SIZE, node_color=NODE_FILL)
    nx.draw_networkx_labels(
        g,
        pos,
        labels={n: d["label"] for n, d in g.nodes(data=True)},
        ax=ax,
        font_size=12,
        font_color="white",
        font_weight="bold",
    )
    _place_edge_labels(ax, fig, g, pos)
    n_inc = g.number_of_nodes()
    n_layers = len({layer for _, _, d in g.edges(data=True) for layer, _ in d["layers"]})
    ax.set_title(
        f"One campaign: {n_inc} incidents linked by {n_layers} evidence layers",
        fontsize=14,
        loc="left",
    )
    notes = [FOOTNOTE]
    if g.graph["pruned"]:
        notes.append(f"{g.graph['pruned']} weaker incident pairs not drawn (spanning tree plus the strongest others).")
    if g.graph["isolated"]:
        notes.append(
            f"Not drawn: {len(g.graph['isolated'])} campaign incident(s) with no shared evidence to another "
            f"incident ({', '.join(g.graph['isolated'])})."
        )
    lines = [line for note in notes for line in textwrap.wrap(note, FOOTNOTE_WRAP)]
    fig.subplots_adjust(top=0.9, bottom=0.05 + 0.03 * len(lines))
    fig.text(0.01, 0.01, "\n".join(lines), fontsize=9, color="#555555", va="bottom")
    fig.savefig(out, dpi=DPI, facecolor="white")
    plt.close(fig)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dataset", type=Path, help="contract Dataset JSON")
    p.add_argument("out", type=Path, help="output PNG")
    p.add_argument("--campaign", help="campaign id; default: organiser campaign with the most varied evidence")
    args = p.parse_args(argv)
    ds = Dataset.from_json(args.dataset.read_bytes())
    threshold = R.review_threshold_bits(ds)
    if args.campaign:
        matches = [c for c in ds.campaigns if c.id == args.campaign]
        if not matches:
            raise SystemExit(f"campaign {args.campaign} not in dataset")
        camp = matches[0]
    else:
        camp = pick_campaign(ds, threshold)
    g = campaign_graph(ds, camp, threshold)
    draw(g, args.out)
    print(
        f"{camp.id}: {g.number_of_nodes()} incidents, {g.number_of_edges()} edges drawn, "
        f"{g.graph['pruned']} pruned, {len(g.graph['isolated'])} isolated -> {args.out}"
    )


if __name__ == "__main__":
    main()
