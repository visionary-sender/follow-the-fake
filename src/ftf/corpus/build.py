"""Package scanner items, fetcher captures and hand-picked real pages into one contract dataset.

Layout of a corpus directory (`root`); all artefact paths in the output are relative to it:

    scanner/            snapshot of the scanner's data dir: queue.jsonl, runs/, creatives/
    fetch-*/            `ftf fetch` output for queue targets: <capture_id>/capture.json + files
    picked-*/           `ftf fetch` output for the hand-picked must-not-link lists
    dataset.json        written here: contract Dataset, incidents only
    manifest.jsonl      written here: one flat row per incident, keyed by incident id
    summary.json        written here: counts per corpus_kind and source, sizes, fetch outcomes

Ids follow the cross-project scheme (HACK-9 `ftf.ingest.ids`, HACK-41):

- ad-library ads: incident `adlib:<ad archive id>`, captures `adlib:<id>:ad` and, when the
  landing URL was fetched, `adlib:<id>:lander` (the fetcher's capture, re-keyed);
- fetched pages without an ad (CERT Polska domains that loaded, must-not-link pages): the
  fetcher's own incident, ids unchanged (`fetch:<capture_id>`, capture `fetch:<capture_id>:c1`);
- CERT Polska entries with no live page: incident `certpl:<register id>`, capture `...:c1`.

Every capture carries `corpus_kind` and `corpus_source` indicators in the collector layer: they
describe how the corpus was gathered, not the scam, so they can never contribute to a link.
The same fields sit in manifest.jsonl. See docs/corpus.md.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from ftf.contract import models as m
from ftf.corpus.targets import QueueRow, read_picked, read_queue, target_url

EXTRACTOR = "ftf-corpus@0.1.0"

# corpus_kind values.
AD_RECORD = "ad_record"
FETCHED_LANDING = "fetched_landing"
BARE_DOMAIN = "bare_domain"
MUST_NOT_LINK = "must_not_link"
KINDS = (AD_RECORD, FETCHED_LANDING, BARE_DOMAIN, MUST_NOT_LINK, "must_not_link_blocked")  # see MUST_NOT_LINK_BLOCKED

# Sources with an ad behind the queue row; everything else (CERT Polska) is a bare domain list.
AD_SOURCES = {"meta", "google_atc", "google_bq"}
PICKED_SOURCE = "hand_picked_must_not_link"
# A hand-picked real page the fetcher could not load (e.g. HTTP 403): kept as honest data, but
# not a must-not-link distractor, since its capture shows an error page, not the outlet.
MUST_NOT_LINK_BLOCKED = "must_not_link_blocked"
VICTIM_PROFILE = "victim-pl-mobile"
VICTIM_TAG = {"profile": VICTIM_PROFILE}
WAYBACK_SOURCE = "wayback"
_WAYBACK = re.compile(r"^https?://web\.archive\.org/web/(\d{4,14})[a-z_]*/((?:https?://)?.+)$")


def wayback_original(url: str) -> tuple[str, str] | None:
    """(snapshot timestamp, original URL) for an Internet Archive snapshot URL, else None."""
    mt = _WAYBACK.match(url)
    if not mt:
        return None
    orig = mt.group(2)
    return mt.group(1), orig if orig.startswith(("http://", "https://")) else f"https://{orig}"


def sanitise(part: str) -> str:
    """Id part, exactly as `ftf ingest` builds it (HACK-9 `ftf.ingest.ids.sanitise`)."""
    from ftf.ingest.ids import sanitise as ingest_sanitise

    return ingest_sanitise(part) or "x"


@dataclass
class FetchedCapture:
    """One `ftf fetch` capture directory, read from its capture.json."""

    rel_dir: str  # relative to the corpus root
    manifest: dict

    @property
    def requested_url(self) -> str:
        return self.manifest["requested_url"]

    @property
    def category(self) -> str:
        return self.manifest.get("category") or "unknown"

    @property
    def outcome(self) -> str:
        return self.manifest.get("outcome") or "unknown"

    @property
    def is_page(self) -> bool:
        """The fetcher rendered a real page (its own `category`, never an outcome list here)."""
        return self.category == "live"

    def incident(self) -> m.Incident:
        """The fetcher's incident with artefact paths made relative to the corpus root."""
        inc = m.Incident.model_validate(self.manifest["incident"])
        base = self.rel_dir.rsplit("/", 1)[0]  # fetcher paths are "<capture_id>/<file>" under --out
        caps = []
        for c in inc.captures:
            arts = [a.model_copy(update={"path": f"{base}/{a.path}"}) if a.path else a for a in c.artefacts]
            caps.append(c.model_copy(update={"artefacts": arts}))
        return inc.model_copy(update={"captures": caps})


def read_fetch_dirs(root: Path, subdirs: list[str]) -> dict[str, FetchedCapture]:
    """Map requested URL -> capture, across all `subdirs`.

    A URL fetched more than once keeps a capture that loaded a page over one that did not, and
    among equals the later one (by `started_at`), whatever the order of `subdirs`.
    """
    found: list[FetchedCapture] = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for cj in base.glob("*/capture.json"):
            try:
                man = json.loads(cj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(man, dict) or "requested_url" not in man or "incident" not in man:
                continue
            found.append(FetchedCapture(f"{sub}/{cj.parent.name}", man))
    out: dict[str, FetchedCapture] = {}
    for fc in sorted(found, key=lambda f: (f.is_page, f.manifest.get("started_at") or "")):
        out[fc.requested_url] = fc
    return out


def _tag(capture_id: str, kind: str, value: str) -> m.Indicator:
    return m.Indicator(
        id=f"{capture_id}:{kind}",
        capture_id=capture_id,
        layer=m.Layer.collector,
        kind=kind,
        level=m.Level.capture,
        value=value,
        observed=m.Observed.static,
        provenance=m.Provenance(note="corpus builder tag"),
        extractor=EXTRACTOR,
    )


def _tags(capture_id: str, corpus_kind: str, source: str, **extra: str) -> list[m.Indicator]:
    out = [_tag(capture_id, "corpus_kind", corpus_kind), _tag(capture_id, "corpus_source", source)]
    out += [_tag(capture_id, f"corpus_{k}", v) for k, v in extra.items()]
    return out


def _rekey(inc: m.Incident, new_inc: str, cap_ids: dict[str, str]) -> tuple[list[m.Capture], list[m.Indicator]]:
    """Move captures (old id -> new id per `cap_ids`) and their indicators to new ids.

    Artefact ids become `<capture>:<n>`, indicator ids `<capture>:<kind>:<n>`; provenance follows.
    """
    art_ids: dict[str, str] = {}
    caps = []
    for c in inc.captures:
        if c.id not in cap_ids:
            continue
        cid = cap_ids[c.id]
        arts = []
        for n, a in enumerate(c.artefacts):
            art_ids[a.id] = f"{cid}:{a.type.value}:{n}"
            arts.append(a.model_copy(update={"id": art_ids[a.id]}))
        caps.append(c.model_copy(update={"id": cid, "artefacts": arts}))
    inds = []
    per_kind: Counter = Counter()
    for ind in inc.indicators:
        cid = cap_ids.get(ind.capture_id)
        if cid is None:
            continue
        prov = ind.provenance
        if prov.artefact_id is not None:
            prov = prov.model_copy(update={"artefact_id": art_ids.get(prov.artefact_id)})
        n = per_kind[(cid, ind.kind)]
        per_kind[(cid, ind.kind)] += 1
        inds.append(ind.model_copy(update={"id": f"{cid}:{ind.kind}:{n}", "capture_id": cid, "provenance": prov}))
    return caps, inds


def _rehome_scanner(inc: m.Incident, scanner: str) -> m.Incident:
    """Scanner artefact paths are relative to its data dir; make them relative to the root."""
    caps = []
    for c in inc.captures:
        arts = [a.model_copy(update={"path": f"{scanner}/{a.path}"}) if a.path else a for a in c.artefacts]
        caps.append(c.model_copy(update={"artefacts": arts}))
    return inc.model_copy(update={"captures": caps})


def _attempt_label(man: dict) -> str:
    """`<profile>@<egress>` for a fetch attempt, e.g. `victim-pl-mobile@ftf-egress-1` or `...@proxy`.

    The fetcher writes `profile` and `egress` as objects (name, user agent, region, ...)."""
    prof = man.get("profile")
    prof = (prof.get("name") if isinstance(prof, dict) else prof) or "default"
    eg = man.get("egress") if isinstance(man.get("egress"), dict) else {}
    where = "proxy" if eg.get("proxy") else (eg.get("name") or "egress")
    return f"{prof}@{where}"


def _is_creative(a: m.Artefact) -> bool:
    name = (a.path or "").rsplit("/", 1)[-1]
    return a.type is m.ArtefactType.ad_screenshot or (a.type is m.ArtefactType.asset and name.startswith("creative-"))


def _source_label(row: QueueRow) -> str:
    return f"{row.source}:{row.keyword}" if row.keyword else row.source


def _bytes(root: Path, inc: m.Incident) -> int:
    total = 0
    for c in inc.captures:
        if c.duplicate_of is not None:
            continue  # the same files are counted on the canonical capture
        for a in c.artefacts:
            if a.size is not None:
                total += a.size
            elif a.path and (root / a.path).is_file():
                total += (root / a.path).stat().st_size
    return total


def _ad_incident(
    base: m.Incident, row: QueueRow, fc: FetchedCapture | None, victim: FetchedCapture | None, label: str
) -> m.Incident:
    # v1 scanner incidents already carry the cross-project id; pre-v1 ones get it from the ad id.
    iid = base.id if base.id.startswith("adlib:") else f"adlib:{sanitise(base.source_ref or row.incident_id)}"
    ad_caps = [c for c in base.captures if c.stage is m.Stage.ad] or base.captures[:1]
    names = {c.id: f"{iid}:ad" if i == 0 else f"{iid}:ad{i}" for i, c in enumerate(ad_caps)}
    caps, inds = _rekey(base, iid, names)
    for c in caps:
        inds += _tags(c.id, AD_RECORD, label)
    for suffix, f, extra in ((":lander", fc, {}), (":lander-victim", victim, VICTIM_TAG)):
        if f is not None:
            lcaps, linds = _fetched_captures(f, f"{iid}{suffix}", label, extra)
            caps += [c.model_copy(update={"stage": m.Stage.lander}) for c in lcaps]
            inds += linds
    return m.Incident(id=iid, source=base.source, source_ref=base.source_ref, captures=caps, indicators=inds)


def _fetched_captures(
    fc: FetchedCapture, cid: str, label: str, extra: dict[str, str]
) -> tuple[list[m.Capture], list[m.Indicator]]:
    """All captures of a fetch, re-keyed as `cid`, `cid`2, ... and tagged by their own verdict."""
    finc = fc.incident()
    names = {c.id: cid if i == 0 else f"{cid}{i + 1}" for i, c in enumerate(finc.captures)}
    caps, inds = _rekey(finc, cid, names)
    kind = FETCHED_LANDING if fc.is_page else BARE_DOMAIN
    for c in caps:
        inds += _tags(c.id, kind, label, **extra)
    return caps, inds


def _fetch_incident(
    fc: FetchedCapture,
    source: str,
    source_ref: str,
    kind: str,
    label: str,
    victim: FetchedCapture | None = None,
    **extra: str,
) -> m.Incident:
    inc = fc.incident()
    inds = list(inc.indicators)
    for c in inc.captures:
        inds += _tags(c.id, kind, label, **extra)
    caps = list(inc.captures)
    if victim is not None:
        vcaps, vinds = _fetched_captures(victim, f"{inc.id}:victim", label, VICTIM_TAG)
        caps += vcaps
        inds += vinds
    return inc.model_copy(update={"source": source, "source_ref": source_ref, "captures": caps, "indicators": inds})


def _bare_incident(base: m.Incident, label: str, victim: FetchedCapture | None = None) -> m.Incident:
    ref = sanitise(base.source_ref or base.id)
    iid = f"certpl:{ref}"
    names = {c.id: f"{iid}:c{i + 1}" for i, c in enumerate(base.captures)}
    caps, inds = _rekey(base, iid, names)
    for c in caps:
        inds += _tags(c.id, BARE_DOMAIN, label)
    if victim is not None:
        vcaps, vinds = _fetched_captures(victim, f"{iid}:victim", label, VICTIM_TAG)
        caps += vcaps
        inds += vinds
    return m.Incident(id=iid, source=base.source, source_ref=base.source_ref, captures=caps, indicators=inds)


def _mark_duplicates(incidents: list[m.Incident], url_of: dict[str, tuple[str, str]]) -> list[m.Incident]:
    """Captures of the same requested URL in several incidents (ads sharing one landing page):
    the lexicographically smallest capture id is canonical, the others point at it."""
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for cid, url in url_of.items():
        groups[url].append(cid)
    dup = {cid: min(ids) for ids in groups.values() if len(ids) > 1 for cid in ids if cid != min(ids)}
    if not dup:
        return incidents
    out = []
    for inc in incidents:
        caps = [c.model_copy(update={"duplicate_of": dup[c.id]}) if c.id in dup else c for c in inc.captures]
        out.append(inc.model_copy(update={"captures": caps}))
    return out


def build_corpus(
    root: Path,
    scanner: str | None,
    runs: list[str] | None,
    fetch_dirs: list[str],
    picked: list[Path] | Path | None,
    picked_fetch_dirs: list[str],
    name: str = "corpus",
    victim_dirs: list[str] | None = None,
    egress_dirs: list[str] | None = None,
) -> dict:
    root = Path(root)
    incidents: list[m.Incident] = []
    rows_out: list[dict] = []
    # capture id -> (requested URL, profile), for duplicate marking: same URL and profile = same files
    lander_url: dict[str, tuple[str, str]] = {}
    seen_ids: set[str] = set()

    def add(inc: m.Incident, row: dict) -> bool:
        if inc.id in seen_ids:
            # One incident for several corpus items (a CERT domain listed twice, two entries whose
            # landing URL is the same fetch): keep the item in the manifest, not a second incident.
            rows_out.append({"incident_id": inc.id, **row, "shares_incident": True})
            return False
        seen_ids.add(inc.id)
        incidents.append(inc)
        rows_out.append({"incident_id": inc.id, **row})
        return True

    if scanner:
        fetched = read_fetch_dirs(root, fetch_dirs)
        victims = read_fetch_dirs(root, victim_dirs or [])
        # Re-fetches of the same URLs from other egress IPs (HACK-45): extra attempts, recorded in
        # the manifest (the ingested dataset merges them with the default fetch by URL).
        egress: dict[str, list[FetchedCapture]] = defaultdict(list)
        for sub in egress_dirs or []:
            for fc in read_fetch_dirs(root, [sub]).values():
                egress[fc.requested_url].append(fc)
        files: dict[str, dict[str, m.Incident]] = {}
        for row in read_queue(root / scanner / "queue.jsonl", runs):
            if row.capture_dir:  # v1 scanner layout: one folder per item
                cj = root / scanner / row.capture_dir / "capture.json"
                if not cj.is_file():
                    continue
                base = m.Incident.model_validate(json.loads(cj.read_text(encoding="utf-8"))["incident"])
            elif row.incidents_file:
                if row.incidents_file not in files:
                    ds = m.Dataset.from_json((root / scanner / row.incidents_file).read_bytes())
                    files[row.incidents_file] = {i.id: i for i in ds.incidents}
                base = files[row.incidents_file].get(row.incident_id)
            else:
                base = None
            if base is None:
                continue
            base = _rehome_scanner(base, scanner)
            label = _source_label(row)
            url = target_url(row)
            fc = fetched.get(url) if url else None
            vc = victims.get(url) if url else None
            if row.source in AD_SOURCES:
                inc = _ad_incident(base, row, fc, vc, label)
                kind = AD_RECORD
            elif fc is not None and fc.is_page:
                inc = _fetch_incident(fc, base.source, base.source_ref or row.incident_id, FETCHED_LANDING, label, vc)
                kind = FETCHED_LANDING
            else:
                inc = _bare_incident(base, label, vc)
                kind = FETCHED_LANDING if vc is not None and vc.is_page else BARE_DOMAIN
            if not add(
                inc,
                {
                    "corpus_kind": kind,
                    "source": row.source,
                    "query": row.keyword,
                    "source_label": label,
                    "source_ref": base.source_ref,
                    "scanner_key": row.key,
                    "scanner_run": row.run_id,
                    "target_url": url,
                    "fetch_capture_id": fc.manifest.get("capture_id") if fc else None,
                    "fetch_outcome": fc.outcome if fc else None,
                    "fetch_category": fc.category if fc else None,
                    "final_url": fc.manifest.get("final_url") if fc else None,
                    "has_landing_page": bool(fc and fc.is_page),
                    "victim_capture_id": vc.manifest.get("capture_id") if vc else None,
                    "victim_outcome": vc.outcome if vc else None,
                    "victim_category": vc.category if vc else None,
                    "victim_final_url": vc.manifest.get("final_url") if vc else None,
                    "victim_flipped_to_live": bool(vc and vc.is_page and not (fc and fc.is_page)),
                    "egress": [
                        {"dir": e.rel_dir.split("/", 1)[0], "capture_id": e.manifest.get("capture_id"),
                         "profile": _attempt_label(e.manifest), "outcome": e.outcome,
                         "category": e.category, "final_url": e.manifest.get("final_url")}
                        for e in (egress.get(url, []) if url else [])
                    ],
                    "creatives": sum(1 for c in inc.captures for a in c.artefacts if _is_creative(a)),
                },
            ):
                continue
            # Fetched captures, keyed by the files they share (same URL, same profile).
            if kind == AD_RECORD:
                default_cid, victim_cid = f"{inc.id}:lander", f"{inc.id}:lander-victim"
            elif inc.id.startswith("fetch:"):
                default_cid, victim_cid = inc.captures[0].id, f"{inc.id}:victim"
            else:
                default_cid, victim_cid = None, f"{inc.id}:victim"
            if fc is not None and default_cid:
                lander_url[default_cid] = (url, "default")
            if vc is not None:
                lander_url[victim_cid] = (url, VICTIM_PROFILE)

    lists = [picked] if isinstance(picked, (str, Path)) else list(picked or [])
    if lists:
        pfetched = read_fetch_dirs(root, picked_fetch_dirs)
        for lst in lists:
            lp = Path(lst)
            if not lp.is_absolute() and not lp.exists():
                lp = root / lp  # lists kept inside the batch directory
            for p in read_picked(lp):
                fc = pfetched.get(p.url)
                if fc is None:
                    continue
                kind = MUST_NOT_LINK if fc.is_page else MUST_NOT_LINK_BLOCKED
                label = f"picked:{p.brand}:{p.page_kind}"
                wb = wayback_original(p.url)
                source, ref, extra = PICKED_SOURCE, p.url, {}
                if wb:  # a real page served by the Internet Archive because the outlet blocked us
                    source, ref, extra = WAYBACK_SOURCE, wb[1], {"wayback_snapshot": wb[0]}
                    label = f"wayback:{p.brand}:{p.page_kind}"
                inc = _fetch_incident(fc, source, ref, kind, label, brand=p.brand, page_kind=p.page_kind, **extra)
                add(
                    inc,
                    {
                        "corpus_kind": kind,
                        "source": source,
                        "source_ref": ref,
                        "wayback_snapshot": wb[0] if wb else None,
                        "query": None,
                        "source_label": label,
                        "brand": p.brand,
                        "page_kind": p.page_kind,
                        "target_url": p.url,
                        "fetch_capture_id": fc.manifest.get("capture_id"),
                        "fetch_outcome": fc.outcome,
                        "fetch_category": fc.category,
                        "final_url": fc.manifest.get("final_url"),
                        "has_landing_page": fc.is_page,
                        "capture_ids": [c.id for c in inc.captures],
                        "note": p.note,
                    },
                )

    incidents = _mark_duplicates(incidents, lander_url)
    ds = m.Dataset(
        name=name,
        description="Background corpus (HACK-18): scanner ads, fetched landing pages, bare domains, must-not-link real pages.",
        incidents=incidents,
    )
    by_id = {i.id: i for i in ds.incidents}
    for r in rows_out:
        inc = by_id[r["incident_id"]]
        r["bytes"] = 0 if r.get("shares_incident") else _bytes(root, inc)
        r["lander_duplicate_of"] = next((c.duplicate_of for c in inc.captures if c.duplicate_of), None)
    (root / "dataset.json").write_text(ds.to_json(), encoding="utf-8")
    with open(root / "manifest.jsonl", "w", encoding="utf-8") as fh:
        for r in rows_out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = summarise(rows_out)
    (root / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def summarise(rows: list[dict]) -> dict:
    by_kind = Counter(r["corpus_kind"] for r in rows)
    by_kind_source = Counter(f"{r['corpus_kind']} | {r['source_label']}" for r in rows)
    outcomes = Counter(f"{r['source']} | {r['fetch_category']} | {r['fetch_outcome']}" for r in rows if r["fetch_outcome"])
    return {
        "incidents": len(rows),
        "by_corpus_kind": dict(sorted(by_kind.items())),
        "by_corpus_kind_and_source": dict(sorted(by_kind_source.items())),
        "ads_with_landing_page": sum(1 for r in rows if r["corpus_kind"] == AD_RECORD and r["has_landing_page"]),
        "egress": {
            "refetched": sum(1 for r in rows if r.get("egress")),
            "live_only_via_egress": sum(
                1 for r in rows
                if any(e["category"] == "live" for e in r.get("egress") or [])
                and r.get("fetch_category") != "live" and r.get("victim_category") != "live"
            ),
        },
        "victim_profile": {
            "refetched": sum(1 for r in rows if r.get("victim_outcome")),
            "flipped_to_live": sum(1 for r in rows if r.get("victim_flipped_to_live")),
            "default_vs_victim": dict(
                sorted(
                    Counter(
                        f"{r['source']} | {r['fetch_outcome']} -> {r['victim_outcome']}"
                        for r in rows
                        if r.get("victim_outcome")
                    ).items()
                )
            ),
        },
        "fetch_outcomes": dict(sorted(outcomes.items())),
        "not_fetched": sum(1 for r in rows if not r["fetch_outcome"]),
        "bytes_referenced": sum(r["bytes"] for r in rows),
    }
