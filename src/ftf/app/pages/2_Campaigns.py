"""Campaigns: how captures connect under each relation, and why two captures are linked (HACK-31).

Thin page: logic lives in `ftf.app.relations` and `ftf.app.figures`. Values from captures are
shown only as defanged plain text (tables, code), never as HTML or markdown links.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ftf.app import relations as R
from ftf.app.base import load_base
from ftf.app.figures import indicator_figure, network_figure
from ftf.contract import Relation


@st.cache_resource
def _base():
    return load_base()


def _plain(text: str) -> None:
    st.code(R.defang(text), language=None)


def _evidence(edge: R.Edge) -> None:
    st.caption(
        f"{R.defang(edge.link_id)} ·{R.RELATION_LABEL[edge.relation]} · {edge.tier} · "
        f"evidence in bits: {edge.score_bits:.1f}"
    )
    st.dataframe(pd.DataFrame(R.evidence_rows(edge)), hide_index=True, use_container_width=True)


st.title("Campaigns")
st.caption(
    "Captures linked under one relation at a time. Edge width is the evidence in bits, colour is "
    "the tier (Strong / Medium / Supplementary / Insufficient alone). Fields that describe the "
    "collector, not the scam, never draw an edge."
)

try:
    ds = _base()
except FileNotFoundError:
    st.warning("No campaign base found. Set FTF_BASE to a dataset.json or pull the fixture.")
    st.stop()

threshold = R.review_threshold_bits(ds)
relation = st.selectbox(
    "Relation", list(R.RELATIONS), format_func=lambda r: R.RELATION_LABEL[r].capitalize(),
    index=list(R.RELATIONS).index(Relation.tracking_account), key="relation",
)
graph = R.relation_graph(ds, relation, threshold)
groups = R.clusters(ds, relation, threshold)

summary = R.campaign_summary(ds, relation)
if summary:
    st.subheader("Known campaigns")
    st.dataframe(pd.DataFrame([{
        "campaign": s.label,
        "id": s.campaign_id,
        "relation": R.RELATION_LABEL[s.relation],
        "captures": s.n_captures,
        "incidents": s.n_incidents,
        "links": s.n_links,
        "strongest shared indicators": "; ".join(
            f"{t.kind}={R.defang(t.value)}" for t in s.top_indicators[:3]),
        "first seen": s.first_seen or "",
        "last seen": s.last_seen or "",
    } for s in summary]), hide_index=True, use_container_width=True)
else:
    st.info(f"No labelled campaigns under “{R.RELATION_LABEL[relation]}”.")

st.subheader("Graph")
choice = st.selectbox(
    "Highlight a group", [None] + groups,
    format_func=lambda c: "—" if c is None else f"{c.label} ({len(c.capture_ids)} captures)",
    key="group",
)
highlight = set(choice.capture_ids) if choice else set()
st.plotly_chart(network_figure(graph, highlight), use_container_width=True)
st.caption(f"Review threshold: {threshold:.1f} bits; a single link below it is “Insufficient alone”.")

if choice is not None:
    st.subheader("Group detail")
    st.write(f"{len(choice.capture_ids)} captures, {len(choice.link_ids)} links "
             f"({'labelled campaign' if choice.source == 'campaign' else 'unlabelled connected component'}).")
    sub = graph.subgraph([c for c in choice.capture_ids if c in graph])
    for _, _, d in sub.edges(data=True):
        for e in d["edges"]:
            with st.expander(f"{R.defang(e.a)} ↔ {R.defang(e.b)} · {e.tier} · {e.score_bits:.1f} bits"):
                _evidence(e)
    if choice.source == "campaign":
        piv = R.indicator_view(ds, choice.id)
        if any(d["side"] == "indicator" for _, d in piv.nodes(data=True)):
            st.markdown("**Pivots joining this campaign** (indicators shared by two or more of its captures)")
            st.plotly_chart(indicator_figure(piv), use_container_width=True)

st.subheader("Why are these two linked?")
captures = sorted(c.id for i in ds.incidents for c in i.captures)
col_a, col_b = st.columns(2)
a = col_a.selectbox("Capture A", captures, index=0, key="pair_a")
b = col_b.selectbox("Capture B", captures, index=min(1, len(captures) - 1), key="pair_b")
if a == b:
    st.info("Pick two different captures.")
else:
    answers = R.explain_pair(ds, a, b, threshold)
    if not answers:
        st.info("These captures are not linked under any relation.")
    for ans in answers:
        how = "directly linked" if ans.direct else f"linked through {len(ans.path) - 2} other capture(s)"
        st.markdown(f"**{ans.label.capitalize()}** — {how}")
        if not ans.direct:
            _plain(" → ".join(ans.path))
        for e in ans.edges:
            _evidence(e)
