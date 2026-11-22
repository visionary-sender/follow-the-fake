"""Follow the Fake demo: analyse one item and show how it links to known campaigns.

Thin Streamlit layer over `ftf.pipeline.analyze` and `ftf.app.ledger`. Captured content is never
rendered: values are shown as plain text in tables (defanged), never as Markdown, HTML or links;
only an uploaded PNG/JPG screenshot is shown as an image.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import streamlit as st

from ftf.app import ledger
from ftf.app.base import base_dir, load_base
from ftf.pipeline import AnalysisInput, analyze

st.set_page_config(page_title="Follow the Fake", layout="wide")

UPLOAD_TYPES = {
    "screenshot": ["png", "jpg", "jpeg", "webp"],
    "har": ["har"],
    "html": ["html", "htm"],
    "mhtml": ["mhtml", "mht"],
    "media": ["mp4", "webm", "mov", "png", "jpg", "jpeg", "gif"],
}
KIND_LABELS = {
    "dataset_item": "Dataset item",
    "url": "URL",
    "keyword": "Keyword",
    "screenshot": "Screenshot",
    "har": "HAR",
    "html": "HTML",
    "mhtml": "MHTML",
    "media": "Ad media",
}
SHOWABLE_IMAGES = {".png", ".jpg", ".jpeg"}


@st.cache_resource(show_spinner=False)
def _base(source: str):
    return load_base(source or None)


def _table(rows, **kwargs) -> None:
    if rows:
        st.dataframe(rows, hide_index=True, **kwargs)
    else:
        st.text("(none)")


source = os.environ.get("FTF_BASE", "")
try:
    base = _base(source)
except (OSError, ValueError) as exc:
    st.error("Cannot load the campaign base. Set FTF_BASE to a contract dataset.json or pull the fixture.")
    st.text(str(exc))
    st.stop()

st.title("Follow the Fake")
st.caption("From one suspicious ad to its campaign: extracted features, comparison with known cases, grouping and the grounds of every link.")
with st.sidebar:
    st.subheader("Campaign base")
    st.text(f"{'FTF_BASE' if source else 'fixture'}: {base.name}")
    st.text(f"{len(base.incidents)} incidents, {len(base.links)} links, {len(base.campaigns)} campaigns")
    online = st.checkbox("Online enrichment", value=True, key="online",
                         help="Registry and DNS lookups (RDAP, DNS over HTTPS, crt.sh, CERT Polska list). "
                              "If the network is down, answers come from the offline cache. "
                              "Live page fetching runs only in the jury image (FTF_FETCH=1), inside its hardened container.")


def _clear_result() -> None:
    old = st.session_state.pop("result", None)
    if old is not None and old.run_dir is not None:
        shutil.rmtree(old.run_dir, ignore_errors=True)


# -- 1. input ------------------------------------------------------------------------------------
st.header("1. Input")
kind = st.radio("What do you want to analyse?", list(KIND_LABELS), format_func=KIND_LABELS.get, horizontal=True, key="kind",
                on_change=_clear_result)
text = None
upload = None
if kind == "dataset_item":
    ids = [i.id for i in base.incidents]
    text = st.selectbox("Incident from the base", ids, key="item") if ids else None
elif kind == "url":
    text = st.text_input(
        "Suspicious URL (never opened; " + ("online registry and DNS lookups)" if online else "offline: cached enrichment only)"),
        key="url")
elif kind == "keyword":
    text = st.text_input("Keyword or phrase from the ad", key="keyword")
else:
    upload = st.file_uploader(f"{KIND_LABELS[kind]} file", type=UPLOAD_TYPES[kind], key=f"upload_{kind}")

if st.button("Analyse", type="primary", key="analyze"):
    tmp = None
    try:
        path = None
        name = None
        if upload is not None:
            tmp = Path(tempfile.mkdtemp(prefix="ftf-upload-"))
            name = upload.name
            path = tmp / "upload"
            path.write_bytes(upload.getvalue())
        inp = AnalysisInput(kind=kind, path=path, text=text or None, name=name)
    except ValueError as exc:
        st.warning("Cannot analyse this input.")
        st.text(str(exc))
    else:
        _clear_result()  # one run directory at a time: the previous upload copy is deleted
        with st.spinner("Running the pipeline…"):
            forced_offline = os.environ.get("FTF_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}
            fetch_on = os.environ.get("FTF_FETCH", "").strip().lower() in {"1", "true", "yes", "on"}
            st.session_state["result"] = analyze(inp, base, offline=not online or forced_offline,
                                                 base_dir=base_dir(source or None), fetch=fetch_on)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

result = st.session_state.get("result")
if result is None:
    st.info("Pick an input and press Analyse.")
    st.stop()

incident = result.incident
try:
    dataset = result.to_dataset()
except ValueError as exc:
    st.error("The result does not form a valid contract dataset; showing the stages only.")
    st.text(str(exc))
    _table(ledger.stage_rows(result.stages))
    st.stop()
threshold = ledger.review_threshold_bits(base)

# -- 2. stages -----------------------------------------------------------------------------------
st.header("2. Pipeline stages")
_table(ledger.stage_rows(result.stages))
st.text(f"enrichment: {result.enrichment or 'not run'}")

# -- 3. features ---------------------------------------------------------------------------------
st.header("3. Extracted features")
st.text(f"Incident {incident.id} · source {incident.source} · {len(incident.captures)} capture(s) · {len(incident.indicators)} indicator(s)")
if result.input_root is not None and result.input.kind in ("screenshot", "media"):
    for cap in incident.captures:
        for art in cap.artefacts:
            p = result.input_root / (art.path or "")
            if art.path and p.suffix.lower() in SHOWABLE_IMAGES and p.is_file():
                st.image(str(p), caption="submitted screenshot", width=360)
cap_rows = [
    {
        "capture": c.id,
        "stage": c.stage.value,
        "observed": c.observed.value,
        "artefacts": ", ".join(sorted({a.type.value for a in c.artefacts})),
        "missing": ", ".join(m.modality.value for m in c.missing),
    }
    for c in incident.captures
]
_table(cap_rows)
groups = ledger.feature_rows(incident)
if not groups:
    st.text("No indicators extracted (see the stage list for what is not available yet).")
for layer, rows in groups.items():
    with st.expander(f"{layer} · {len(rows)}", expanded=layer != "collector"):
        if layer == "collector":
            st.text("Describes the collector, not the scam: never used as evidence.")
        _table(rows)

# -- 4. decision ---------------------------------------------------------------------------------
st.header("4. Decision")
card = ledger.decision_summary(result.assignment, dataset)
c1, c2 = st.columns(2)
c1.metric("Decision", card["label"])
c2.metric("Evidence capacity", f"{card['evidence_capacity_bits']:.1f} bits")
if card["campaign_id"]:
    st.text(f"Campaign {card['campaign_id']}: {card['campaign_label']}")
if card["note"]:
    st.text(card["note"])
st.subheader("Candidate campaigns")
_table(card["candidates"])
st.subheader("What would change the decision")
for line in ledger.what_would_change(result.assignment, ledger.missing_modalities(incident)):
    st.text(f"• {line}")

# -- 5. evidence ledger --------------------------------------------------------------------------
st.header("5. Evidence ledger")
if not result.links:
    st.text("No links to known cases.")
for link in sorted(result.links, key=lambda l: -(l.score_bits or 0)):
    head = ledger.defang(f"{link.a} ↔ {link.b} · {ledger.relation_sentence(link, threshold)}")
    with st.expander(head, expanded=False):
        st.text(f"Method: {link.method} · link id {link.id}")
        _table([r.as_dict() for r in ledger.ledger_rows(link, dataset, population=base)])

# -- 6. similar incidents ------------------------------------------------------------------------
st.header("6. Similar incidents")
_table(ledger.similar_rows(result.similar, base))
