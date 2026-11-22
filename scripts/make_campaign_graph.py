"""Static PNG of one campaign: incidents as nodes, shared evidence as edges.

Reads a contract `Dataset` JSON (a built base document), picks the
largest campaign that carries platform-layer evidence (or the one given with `--campaign`), and
draws its incidents. Nodes are labelled by case folder number. Each edge carries the strongest
shared evidence layer of its pair and that row's bits. No indicator value (URL, domain, id,
hash) is drawn. Graph data comes from `ftf.app.relations`; only the drawing lives here.

    python3 -I scripts/make_campaign_graph.py DATASET.json OUT.png [--campaign ID]

Run it in the project venv with the app and dev extras, on the cloud workbench (see docs/contract.md).
Needs the `app` extra (networkx) and the `dev` extra (matplotlib).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import networkx as nx  # noqa: E402

from ftf.app import relations as R  # noqa: E402
from ftf.contract import Campaign, Dataset, Layer  # noqa: E402

WIDTH_PX = 1200
DPI = 100
NODE_FILL = "#1f4e79"
EDGE_COLOUR = "#7f8c8d"
FOOTNOTE = (
    "Node: one incident, labelled by case folder number. Edge: strongest shared evidence layer "
    "and its bits; collector-layer evidence is not drawn."
)


def incident_of(ds: Dataset) -> dict[str, str]:
    """Capture id -> incident id."""
    return {c.id: inc.id for inc in ds.incidents for c in inc.captures}


def case_label(ds: Dataset) -> dict[str, str]:
    """Incident id -> case folder number (the folder name the submission came from)."""
    return {inc.id: (inc.source_ref or inc.id) for inc in ds.incidents}


def pick_campaign(ds: Dataset, threshold: float) -> Campaign:
    """Largest campaign (by incidents, then captures) with at least one platform-layer edge."""
    platform_links = {
        e.link_id
        for e in R.edges(ds, threshold=threshold)
        if any(s.layer == Layer.platform.value for s in e.shared)
    }
    inc = incident_of(ds)
    found = []
    for camp in ds.campaigns:
        incidents = {inc[c] for c in camp.capture_ids}
        if len(incidents) >= 2 and platform_links.intersection(camp.link_ids):
            found.append((-len(incidents), -len(camp.capture_ids), camp.id, camp))
    if not found:
        raise SystemExit("no campaign with two incidents and platform-layer evidence")
    return min(found, key=lambda t: t[:3])[3]


def campaign_graph(ds: Dataset, camp: Campaign, threshold: float) -> nx.Graph:
    """Incident-level graph of one campaign; each edge keeps its strongest shared row."""
    rel_graph = R.relation_graph(ds, camp.relation, threshold)
    members = set(camp.capture_ids)
    inc = incident_of(ds)
    labels = case_label(ds)
    g = nx.Graph(campaign=camp.id, relation=camp.relation.value)
    for cid in members:
        g.add_node(inc[cid], label=labels[inc[cid]])
    for u, v, data in rel_graph.edges(data=True):
        if u not in members or v not in members or inc[u] == inc[v]:
            continue
        shared = [s for e in data["edges"] for s in e.shared]
        best = max(shared, key=lambda s: s.bits)
        a, b = inc[u], inc[v]
        if g.has_edge(a, b) and g[a][b]["bits"] >= best.bits:
            continue
        g.add_edge(a, b, layer=best.layer, bits=best.bits)
    return g


def _layer_text(layer: str) -> str:
    return layer.replace("_", " ")


NODE_SIZE = 1700
NODE_HALF_PX = 36  # clearance around a node disc, in display pixels
# Edge-label slots tried in order: fraction along the edge, then perpendicular offset.
LABEL_T = (0.5, 0.36, 0.64, 0.24, 0.76, 0.14, 0.86)
LABEL_OFFSET = (0.0, 0.07, -0.07, 0.14, -0.14)


def _place_edge_labels(ax, fig, g: nx.Graph, pos: dict) -> None:
    """Put each edge label in the first free slot; no label may cover a node or another label."""
    from matplotlib.transforms import Bbox

    renderer = fig.canvas.get_renderer()
    blocked = []
    for n in g.nodes:
        x, y = ax.transData.transform(pos[n])
        blocked.append(Bbox.from_extents(x - NODE_HALF_PX, y - NODE_HALF_PX, x + NODE_HALF_PX, y + NODE_HALF_PX))
    for u, v, d in sorted(g.edges(data=True), key=lambda e: -e[2]["bits"]):
        (x0, y0), (x1, y1) = pos[u], pos[v]
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 or 1.0
        nx_, ny_ = -(y1 - y0) / length, (x1 - x0) / length  # unit normal to the edge
        text = f"{_layer_text(d['layer'])} · {d['bits']:.1f} bits"
        placed = None
        for t in LABEL_T:
            for off in LABEL_OFFSET:
                x = x0 + t * (x1 - x0) + off * nx_
                y = y0 + t * (y1 - y0) + off * ny_
                artist = ax.text(
                    x,
                    y,
                    text,
                    ha="center",
                    va="center",
                    fontsize=10,
                    color="#222222",
                    zorder=5,
                    bbox={"boxstyle": "round,pad=0.3", "fc": "white", "ec": EDGE_COLOUR, "alpha": 0.97},
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
            ax.text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text,
                ha="center",
                va="center",
                fontsize=10,
                zorder=5,
                bbox={"boxstyle": "round,pad=0.3", "fc": "white", "ec": EDGE_COLOUR, "alpha": 0.97},
            )


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


def draw(g: nx.Graph, camp: Campaign, out: Path) -> None:
    """Render the incident graph at 1200 px wide."""
    pos = layout(g)
    fig, ax = plt.subplots(figsize=(WIDTH_PX / DPI, 8.4), dpi=DPI)
    ax.set_axis_off()
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    ax.set_xlim(min(xs) - 0.3, max(xs) + 0.3)
    ax.set_ylim(min(ys) - 0.3, max(ys) + 0.3)
    widths = [1.0 + 0.35 * d["bits"] for _, _, d in g.edges(data=True)]
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
    n_cap = len(camp.capture_ids)
    ax.set_title(
        f"Campaign {camp.id.split(':')[-1]} ({R.RELATION_LABEL[camp.relation]}): "
        f"{n_inc} incidents, {n_cap} captures",
        fontsize=14,
        loc="left",
    )
    fig.text(0.01, 0.01, FOOTNOTE, fontsize=9, color="#555555")
    fig.savefig(out, dpi=DPI, facecolor="white")
    plt.close(fig)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("dataset", type=Path, help="contract Dataset JSON")
    p.add_argument("out", type=Path, help="output PNG")
    p.add_argument("--campaign", help="campaign id; default: largest with platform-layer evidence")
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
    draw(g, camp, args.out)
    print(f"{camp.id}: {g.number_of_nodes()} incidents, {g.number_of_edges()} edges -> {args.out}")


if __name__ == "__main__":
    main()
