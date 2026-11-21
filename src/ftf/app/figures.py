"""Plotly figures for the campaign views (no Streamlit here, so they can be tested headlessly).

Every string that reaches a hover label or a node label goes through `_safe`: plotly renders a
subset of HTML in labels (including <a href>), and indicator values come from hostile pages.
"""

from __future__ import annotations

import html

import networkx as nx
import plotly.graph_objects as go

from ftf.app.relations import INSUFFICIENT, LEVEL_LABEL, defang
from ftf.contract import LinkLevel

TIER_COLOUR = {
    LEVEL_LABEL[LinkLevel.strict]: "#1b7837",
    LEVEL_LABEL[LinkLevel.medium]: "#2166ac",
    LEVEL_LABEL[LinkLevel.loose]: "#b2abd2",
    INSUFFICIENT: "#bdbdbd",
}
STAGE_SYMBOL = {"ad": "square", "pre_lander": "diamond", "lander": "circle", "decoy": "x", "unknown": "circle-open"}


def _safe(text: object, limit: int = 120) -> str:
    s = defang(str(text))
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return html.escape(s, quote=True)


def edge_hover(edge) -> str:
    """Hover text for one `Edge`: relation, tier, bits and the shared indicators."""
    lines = [
        f"<b>{_safe(edge.link_id)}</b> · {_safe(edge.relation.value)} · {_safe(edge.tier)}",
        f"evidence in bits: {edge.score_bits:.1f}",
    ]
    for s in edge.shared[:8]:
        lines.append(f"{_safe(s.layer)} / {_safe(s.kind)}: {_safe(s.value, 60)} ({s.bits:.1f} bits)")
    if len(edge.shared) > 8:
        lines.append(f"… {len(edge.shared) - 8} more")
    if edge.excluded:
        lines.append(f"{len(edge.excluded)} collector row(s) ignored")
    return "<br>".join(lines)


def _width(bits: float) -> float:
    return 1.0 + min(max(bits, 0.0), 30.0) / 3.0


def network_figure(g: nx.Graph, highlight: set[str] | None = None, seed: int = 7) -> go.Figure:
    """Capture graph from `relation_graph`: edge width by bits, colour by tier, hover with evidence."""
    fig = go.Figure()
    if g.number_of_nodes() == 0:
        fig.update_layout(annotations=[dict(text="No links under this relation", showarrow=False)])
        return fig
    pos = nx.spring_layout(g, seed=seed, weight="bits")
    highlight = highlight or set()
    for u, v, d in g.edges(data=True):
        (x0, y0), (x1, y1) = pos[u], pos[v]
        best = max(d["edges"], key=lambda e: e.score_bits)
        hover = "<br>—<br>".join(edge_hover(e) for e in d["edges"])
        fig.add_trace(go.Scatter(
            x=[x0, x1], y=[y0, y1], mode="lines", hoverinfo="skip", showlegend=False,
            line=dict(width=_width(best.score_bits), color=TIER_COLOUR[best.tier]),
        ))
        # Hover on line segments is unreliable in plotly; put it on an invisible midpoint marker.
        fig.add_trace(go.Scatter(
            x=[(x0 + x1) / 2], y=[(y0 + y1) / 2], mode="markers", showlegend=False,
            marker=dict(size=14, color=TIER_COLOUR[best.tier], opacity=0.25),
            hovertext=[hover], hoverinfo="text",
        ))
    nodes = list(g.nodes)
    fig.add_trace(go.Scatter(
        x=[pos[n][0] for n in nodes], y=[pos[n][1] for n in nodes], mode="markers+text",
        text=[_safe(n, 40) for n in nodes], textposition="top center", showlegend=False,
        marker=dict(
            size=[18 if n in highlight else 12 for n in nodes],
            symbol=[STAGE_SYMBOL.get(g.nodes[n].get("stage"), "circle") for n in nodes],
            color=["#d6604d" if n in highlight else "#4d4d4d" for n in nodes],
        ),
        hovertext=[
            f"{_safe(n)}<br>incident {_safe(g.nodes[n].get('incident_id'))}"
            f"<br>stage {_safe(g.nodes[n].get('stage'))}"
            f"<br>captured {_safe(g.nodes[n].get('captured_on') or 'unknown')}"
            f"<br>campaigns: {_safe(', '.join(g.nodes[n].get('campaign_ids', ())) or 'none')}"
            for n in nodes
        ],
        hoverinfo="text",
    ))
    for name, colour in TIER_COLOUR.items():  # legend for tiers
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name=name, line=dict(color=colour, width=4)))
    fig.update_layout(
        showlegend=True, hovermode="closest", margin=dict(l=10, r=10, t=10, b=10), height=520,
        xaxis=dict(visible=False), yaxis=dict(visible=False),
    )
    return fig


def indicator_figure(g: nx.Graph, seed: int = 7) -> go.Figure:
    """Bipartite capture ↔ indicator figure from `indicator_view`."""
    fig = go.Figure()
    if g.number_of_nodes() == 0:
        return fig
    pos = nx.spring_layout(g, seed=seed)
    xs, ys = [], []
    for u, v in g.edges:
        xs += [pos[u][0], pos[v][0], None]
        ys += [pos[u][1], pos[v][1], None]
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip", showlegend=False,
                             line=dict(width=1, color="#999999")))
    for side, colour, symbol in (("capture", "#4d4d4d", "circle"), ("indicator", "#e08214", "diamond")):
        ns = [n for n, d in g.nodes(data=True) if d["side"] == side]
        if side == "capture":
            labels = [_safe(n, 40) for n in ns]
            hover = [f"{_safe(n)}<br>stage {_safe(g.nodes[n].get('stage'))}" for n in ns]
        else:
            labels = [_safe(g.nodes[n]["kind"], 30) for n in ns]
            hover = [f"{_safe(g.nodes[n]['layer'])} / {_safe(g.nodes[n]['kind'])}<br>{_safe(g.nodes[n]['value'], 80)}"
                     f"<br>shared by {g.nodes[n]['n']} captures" for n in ns]
        fig.add_trace(go.Scatter(
            x=[pos[n][0] for n in ns], y=[pos[n][1] for n in ns], mode="markers+text", name=side,
            text=labels, textposition="top center", hovertext=hover, hoverinfo="text",
            marker=dict(size=12, color=colour, symbol=symbol),
        ))
    fig.update_layout(hovermode="closest", margin=dict(l=10, r=10, t=10, b=10), height=420,
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    return fig
