"""Build the private contract-v0 fixture from raw cases and prototype signal outputs.

Runs on the workbench VM only (reads /data/raw). Captured files are read as inert bytes.
Writes <out>/dataset.json and <out>/contract-schema.json; never writes into the repo.

    python3 -I scripts/build_fixture.py --raw /data/raw --derived <dir with signals.json,
        cases-shared.json, cases-backup.json> --out /data/derived/<name>/fixture/v0

Links, campaigns and assignments use illustrative D1 prior weights (bits); they show the
contract's shape, not a tuned model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ftf.contract import (  # noqa: E402
    Dataset,
    Layer,
    json_schema,
)

EXTRACTOR = "fixture-builder@0.1.0"

# (incident id, source, folder, [(capture suffix, stage, selector, signals node)])
# selector: "top-image" = screenshots at folder top; "top-page" = other top files + *_files/;
# any other string = that sub-folder or file.
CASES = [
    ("do-1", "do_udostepnienia", "1", [("ad", "ad", "top-image", None), ("lander", "lander", "top-page", "1")]),
    ("backup-p15", "BACKUP", "p15", [("ad", "ad", "top-image", None), ("lander", "lander", "top-page", "p15")]),
    ("do-48", "do_udostepnienia", "48", [("ad", "ad", "top-image", None), ("lander", "lander", "top-page", "48")]),
    (
        "do-61",
        "do_udostepnienia",
        "61",
        [
            ("ad", "ad", "reklama_1.png", "61/ad"),
            ("s1", "lander", "strona_1", "61/s1"),
            ("s2", "lander", "strona_2", "61/s2"),
            ("s3", "decoy", "strona_3", "61/s3"),
        ],
    ),
]

# signals.json subfamily -> (layer, level, kind, max values)
SIG_MAP = {
    "fb_pixel": ("tracking_account", "operator", "meta_pixel", 3),
    "gads_id": ("tracking_account", "operator", "google_ads_id", 3),
    "capi_gateway": ("tracking_account", "operator", "capi_gateway", 3),
    "xid_machine": ("tracker_instance", "operator", "xid_machine", 3),
    "landing_cdn_account": ("tenant", "tenant", "aio_cdn_tenant", 3),
    "landing_cdn_landing": ("landing", "landing", "aio_landing", 3),
    "action_files_id": ("platform", "platform", "aio_action_files", 2),
    "aio_sdk_version": ("platform", "platform", "aio_sdk_asset", 2),
    "hue_rotate": ("platform", "platform", "uniqueizer_hue_rotate", 1),
    "macros_file": ("kit", "tenant", "aio_macros_file", 2),
    "title": ("creative", "campaign", "page_title", 1),
    "server_ip": ("infrastructure", "ecosystem", "server_ip", 2),
    "capture_day": ("collector", "capture", "capture_day", 1),
}
# case-level ids from cases-*.json -> (layer, level, kind)
CASE_MAP = {
    "aio_owner": ("owner", "operator", "aio_owner"),
    "aio_owner_uuid": ("owner", "operator", "aio_owner"),
    "aio_lander_template": ("kit", "platform", "aio_lander_template"),
    "aio_template_uuid": ("kit", "platform", "aio_lander_template"),
    "meta_campaign": ("distribution_account", "campaign", "meta_campaign_id"),
    "meta_campaign_id": ("distribution_account", "campaign", "meta_campaign_id"),
}
# illustrative prior weights in bits (07 §D1); single-valued kinds penalise conflicts
WEIGHTS = {
    "aio_cdn_tenant": 6.0,
    "aio_landing": 4.0,
    "aio_owner": 3.0,
    "meta_pixel": 3.0,
    "google_ads_id": 3.0,
    "capi_gateway": 3.0,
    "xid_machine": 3.0,
    "aio_lander_template": 2.0,
    "aio_macros_file": 2.0,
    "page_title": 2.0,
    "advertiser_page_name": 2.0,
    "meta_campaign_id": 2.0,
}
SINGLE_VALUED = {"aio_owner": -4.0, "aio_cdn_tenant": -4.0}
PLATFORM_BITS = 1.7

# (link id, capture a, capture b, relation, count platform layer as evidence?)
LINKS = [
    ("L1", "do-1/lander", "backup-p15/lander", "campaign", False),
    ("L2", "do-48/lander", "do-61/s1", "template", False),
    ("L3", "do-1/lander", "do-48/lander", "ecosystem", True),
]
CAMPAIGNS = [
    ("A1", "Case 1 + p15: same landing, tenant and pixel", "campaign", ["do-1", "backup-p15"], ["L1"]),
    ("A2", "Case 48 + 61: same tenant and lander template", "template", ["do-48", "do-61"], ["L2"]),
]
ASSIGN = ["backup-p15", "do-61"]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def art_type(p: Path, stage: str) -> str:
    if any(part.endswith("_files") for part in p.parts[:-1]):
        return "asset"
    name = p.name.lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        return "ad_screenshot" if stage == "ad" else "screenshot"
    return {".har": "har", ".html": "html", ".htm": "html", ".mhtml": "mhtml", ".mp4": "video",
            ".webm": "video", ".txt": "text"}.get(ext, "other")


def select(folder: Path, selector: str) -> list[Path]:
    top = sorted(p for p in folder.iterdir())
    if selector == "top-image":
        return [p for p in top if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")]
    if selector == "top-page":
        out = []
        for p in top:
            if p.is_file() and p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                out.append(p)
            elif p.is_dir() and p.name.endswith("_files"):
                out.extend(sorted(q for q in p.rglob("*") if q.is_file()))
        return out
    target = folder / selector
    return [target] if target.is_file() else sorted(q for q in target.rglob("*") if q.is_file())


def find_folder(raw: Path, source: str, folder: str) -> Path:
    if source == "BACKUP":
        return raw / "BACKUP" / folder
    (do_dir,) = [p for p in raw.iterdir() if unicodedata.normalize("NFC", p.name) == "do_udostępnienia"]
    return do_dir / folder


EXPECTED = {"ad": ["ad_screenshot", "video", "ad_library"], "lander": ["html", "har"], "decoy": ["html", "har"]}


def build(raw: Path, derived: Path) -> Dataset:
    signals = json.loads((derived / "signals.json").read_text())
    case_meta: dict[str, dict] = {}
    for c in json.loads((derived / "cases-shared.json").read_text())["cases"]:
        case_meta[c["case_id"]] = c
    for c in json.loads((derived / "cases-backup.json").read_text()):
        case_meta[c["case_id"]] = c

    incidents, blobs = [], {}  # blobs: capture id -> [(artefact id, type, bytes)] for har/html
    for inc_id, source, folder, caps in CASES:
        root = find_folder(raw, source, folder)
        captures, indicators = [], []
        for suffix, stage, selector, node in caps:
            cap_id = f"{inc_id}/{suffix}"
            artefacts, cap_blobs = [], []
            for i, p in enumerate(select(root, selector)):
                t = art_type(p.relative_to(root), stage)
                aid = f"{cap_id}/a{i:02d}"
                artefacts.append({"id": aid, "type": t, "path": f"{source}/{folder}/{p.relative_to(root).as_posix()}",
                                  "sha256": sha256(p), "size": p.stat().st_size})
                if t in ("har", "html"):
                    cap_blobs.append((aid, t, p.read_bytes()))
            present = {a["type"] for a in artefacts}
            missing = [{"modality": m, "reason": "not in submission"} for m in EXPECTED[stage] if m not in present]
            day = case_meta.get(folder, {}).get("capture_date") or case_meta.get(folder, {}).get("artifacts", {}).get("capture_date")
            captures.append({"id": cap_id, "stage": stage, "observed": "static", "captured_on": day,
                             "artefacts": artefacts, "missing": missing})
            blobs[cap_id] = cap_blobs
            seen = set()

            def add(layer, level, kind, value, prov_note=None, cap_id=cap_id, cap_blobs=cap_blobs, seen=seen):
                key = (kind, value)
                if key in seen:
                    return
                seen.add(key)
                observed, aid = "static", None
                needle = value.encode()
                for a_id, t, data in sorted(cap_blobs, key=lambda b: b[1] != "har"):
                    if needle in data:
                        observed, aid = ("live" if t == "har" else "static"), a_id
                        break
                indicators.append({
                    "id": f"{cap_id}/{kind}/{len([k for k in seen if k[0] == kind]) - 1}",
                    "capture_id": cap_id, "layer": layer, "kind": kind, "level": level, "value": value,
                    "observed": observed, "provenance": {"artefact_id": aid, "note": prov_note},
                    "extractor": EXTRACTOR,
                })

            node_sig = signals.get(node, {}) if node else {}
            platform_ids = set(node_sig.get("action_files_id", []))
            for sub, (layer, level, kind, n) in SIG_MAP.items():
                vals = [v for v in node_sig.get(sub, []) if not (sub == "landing_cdn_account" and v in platform_ids)]
                for v in vals[:n]:
                    add(layer, level, kind, "present" if sub == "hue_rotate" else str(v), "prototype signals.json")
            for a_id, t, data in cap_blobs:
                if t != "har":
                    continue
                for m in set(re.findall(rb"fingerprint=([0-9a-f]{32})", data)):
                    add("collector", "capture", "aio_fingerprint", m.decode(), "HAR form body")
                for w, h in set(re.findall(rb"browser_window_width=(\d+)&browser_window_height=(\d+)", data)):
                    add("collector", "capture", "viewport", f"{w.decode()}x{h.decode()}", "HAR form body")
            if stage == "ad":
                page = case_meta.get(folder, {}).get("advertiser_page")
                if page:
                    add("distribution_account", "operator", "advertiser_page_name", page, "read from ad screenshot")
                if day:
                    add("collector", "capture", "screenshot_date", day, "screenshot file name")
            if stage in ("lander", "decoy") and "html" in present:
                indicators.append({
                    "id": f"{cap_id}/text_embedding/0", "capture_id": cap_id, "layer": "creative",
                    "kind": "text_embedding", "level": "campaign",
                    "vector_ref": {"store": "vectors/v0/text.f32", "key": cap_id,
                                   "model": "intfloat/multilingual-e5-small", "dim": 384},
                    "observed": "static",
                    "provenance": {"artefact_id": next(a for a, t, _ in cap_blobs if t == "html"),
                                   "note": "reference only; vectors are not shipped in fixture v0"},
                    "extractor": EXTRACTOR,
                })

        # case-level ids: attach to the capture whose files contain the value, else the first lander
        meta = case_meta.get(folder, {})
        ids = meta.get("notable_ids", {})
        pairs = ids.items() if isinstance(ids, dict) else [tuple(s.split(":", 1)) for s in ids if ":" in s]
        for key, val in pairs:
            if key not in CASE_MAP or not isinstance(val, str):
                continue
            val = val.split(" ")[0]
            layer, level, kind = CASE_MAP[key]
            target = next((c for c in caps if any(val.encode() in d for _, _, d in blobs[f"{inc_id}/{c[0]}"])),
                          next(c for c in caps if c[1] == "lander"))
            cap_id = f"{inc_id}/{target[0]}"
            if any(i["capture_id"] == cap_id and i["kind"] == kind and i["value"] == val for i in indicators):
                continue
            hit = next(((a, t) for a, t, d in blobs[cap_id] if val.encode() in d), (None, None))
            n = sum(1 for i in indicators if i["capture_id"] == cap_id and i["kind"] == kind)
            indicators.append({
                "id": f"{cap_id}/{kind}/{n}", "capture_id": cap_id, "layer": layer, "kind": kind, "level": level,
                "value": val, "observed": "live" if hit[1] == "har" else "static",
                "provenance": {"artefact_id": hit[0], "note": "case card (cases-*.json)"},
                "extractor": EXTRACTOR,
            })
        incidents.append({"id": inc_id, "source": source, "source_ref": folder,
                          "captures": captures, "indicators": indicators})

    by_cap: dict[str, list[dict]] = {}
    for inc in incidents:
        for ind in inc["indicators"]:
            by_cap.setdefault(ind["capture_id"], []).append(ind)

    def ledger(a: str, b: str, count_platform: bool) -> tuple[list[dict], float]:
        rows, score = [], 0.0
        kinds = sorted({i["kind"] for i in by_cap[a] + by_cap[b] if "value" in i and i.get("value") is not None})
        for kind in kinds:
            ia = [i for i in by_cap[a] if i["kind"] == kind and i.get("value") is not None]
            ib = [i for i in by_cap[b] if i["kind"] == kind and i.get("value") is not None]
            layer = (ia or ib)[0]["layer"]
            common = {i["value"] for i in ia} & {i["value"] for i in ib}
            if common:
                ids = [i["id"] for i in ia + ib if i["value"] in common]
                val = sorted(common)[0][:40]
                if layer == "collector":
                    rows.append({"layer": layer, "kind": kind, "status": "collector_excluded", "value": val,
                                 "indicator_ids": ids, "note": "describes the capturing analyst, never links"})
                elif layer == "platform":
                    bits = PLATFORM_BITS if count_platform else 0.0
                    rows.append({"layer": layer, "kind": kind, "status": "shared" if count_platform else "hub_subsumed",
                                 "value": val, "bits": bits, "indicator_ids": ids})
                    score += bits
                elif kind in WEIGHTS:
                    rows.append({"layer": layer, "kind": kind, "status": "shared", "value": val,
                                 "bits": WEIGHTS[kind], "indicator_ids": ids})
                    score += WEIGHTS[kind]
            elif ia and ib and kind in SINGLE_VALUED and not count_platform:
                rows.append({"layer": layer, "kind": kind, "status": "conflict",
                             "value": f"{ia[0]['value'][:12]} vs {ib[0]['value'][:12]}",
                             "bits": SINGLE_VALUED[kind], "indicator_ids": [ia[0]["id"], ib[0]["id"]]})
                score += SINGLE_VALUED[kind]
            elif (ia or ib) and not (ia and ib) and kind in WEIGHTS and not count_platform:
                rows.append({"layer": layer, "kind": kind, "status": "missing",
                             "note": f"only on {'a' if ia else 'b'}"})
        return rows, round(score, 2)

    def level(score: float, rows: list[dict]) -> str:
        layers = {r["layer"] for r in rows if r["status"] == "shared"}
        if score >= 10 and len(layers) >= 2:
            return "strict"
        return "medium" if score >= 4 else "loose"

    links = []
    for lid, a, b, rel, count_platform in LINKS:
        rows, score = ledger(a, b, count_platform)
        links.append({"id": lid, "a": a, "b": b, "relation": rel, "level": level(score, rows),
                      "score_bits": score, "method": f"{EXTRACTOR} D1 priors (illustrative)", "evidence": rows})

    caps_of = {inc["id"]: [c["id"] for c in inc["captures"]] for inc in incidents}
    campaigns = [{"id": cid, "label": label, "relation": rel,
                  "capture_ids": [c for i in members for c in caps_of[i]], "link_ids": lids}
                 for cid, label, rel, members, lids in CAMPAIGNS]

    assignments = []
    for inc_id in ASSIGN:
        mine = [c for c in caps_of[inc_id] if c in by_cap]
        cands = []
        for cid, _, _, members, _ in CAMPAIGNS:
            others = [c for m in members if m != inc_id for c in caps_of[m] if c in by_cap]
            best = max((ledger(x, y, False)[1] for x in mine for y in others), default=0.0)
            cands.append({"campaign_id": cid, "score_bits": best})
        cands.sort(key=lambda c: -c["score_bits"])
        top = cands[0]
        decision = "assign" if top["score_bits"] >= 10 else ("review" if top["score_bits"] >= 4 else "new")
        kinds = {i["kind"] for c in mine for i in by_cap[c]}
        capacity = sum(w for k, w in WEIGHTS.items() if k in kinds)
        assignments.append({"incident_id": inc_id, "candidates": cands, "decision": decision,
                            "campaign_id": top["campaign_id"] if decision == "assign" else None,
                            "evidence_capacity_bits": capacity,
                            "method": f"{EXTRACTOR} best-member ledger, thresholds 10/4 bits (illustrative)"})

    doc = {
        "name": "fixture-v0",
        "description": "Four real cases (do_udostepnienia 1, 48, 61; BACKUP p15) mapped onto contract v0. "
                       "Indicators come from raw files and prototype signal outputs; links, campaigns and "
                       "assignments use illustrative prior weights.",
        "incidents": incidents, "links": links, "campaigns": campaigns, "assignments": assignments,
    }
    return Dataset.model_validate(doc)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", type=Path, default=Path("/data/raw"))
    ap.add_argument("--derived", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    ds = build(args.raw, args.derived)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "dataset.json").write_text(ds.to_json())
    (args.out / "contract-schema.json").write_text(json.dumps(json_schema(), indent=2))
    n_ind = len(ds.indicators())
    n_col = sum(1 for i in ds.indicators() if i.layer is Layer.collector)
    print(f"incidents={len(ds.incidents)} captures={sum(len(i.captures) for i in ds.incidents)} "
          f"indicators={n_ind} collector={n_col} links={len(ds.links)} campaigns={len(ds.campaigns)}")
    for l in ds.links:
        print(f"  {l.id} {l.a} <-> {l.b} {l.relation.value}/{l.level.value} {l.score_bits} bits, {len(l.evidence)} rows")
    for a in ds.assignments:
        print(f"  assign {a.incident_id}: {a.decision.value} {a.campaign_id} cands={[(c.campaign_id, c.score_bits) for c in a.candidates]} cap={a.evidence_capacity_bits}")


if __name__ == "__main__":
    main()
