"""Degraded inputs (E3) and distractors (E4) built on contract documents.

Everything here is pure: it takes contract objects, returns new validated contract objects and
never reads captured pages. The expected behaviour of each distractor travels with it as an
`Expectation`, and `link_violations`, `assignment_violations` and `decision_diff` check an
engine's output against it (the runner lives in `ftf.evaluate.openset`).

Method: plans 07 §4 E3/E4 and 04 §(c)-(d).
"""

from __future__ import annotations

import json
import random
import re
import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ftf.contract import (
    ArtefactType,
    Assignment,
    Capture,
    Dataset,
    Decision,
    Incident,
    Indicator,
    Layer,
    Level,
    Relation,
)

# --------------------------------------------------------------------------------------------
# E3: degraded inputs
# --------------------------------------------------------------------------------------------

#: Artefact types each degradation mode keeps. Everything else is dropped and listed as missing.
MODES: dict[str, frozenset[ArtefactType]] = {
    "screenshot": frozenset({ArtefactType.screenshot, ArtefactType.ad_screenshot}),
    "url": frozenset({ArtefactType.url}),
    "html": frozenset({ArtefactType.html, ArtefactType.mhtml}),
    "har": frozenset({ArtefactType.har}),
    "ad_text": frozenset({ArtefactType.text}),
}


def degrade_incident(incident: Incident, mode: str) -> Incident:
    """Keep only the artefacts of `mode` and the indicators extracted from them.

    An indicator survives if its provenance names a kept artefact. Collector indicators survive
    every mode: the engine ignores them, so rows differ only by modality. Other indicators without
    an artefact provenance are dropped, since we cannot tell which modality they came from. Every
    dropped modality is added to the capture's `missing` with reason `degraded:<mode>`.
    """
    keep_types = MODES[mode]  # KeyError on an unknown mode is intended
    reason = f"degraded:{mode}"
    kept_artefacts: set[str] = set()
    captures = []
    for cap in incident.captures:
        arts = [a for a in cap.artefacts if a.type in keep_types]
        kept_artefacts.update(a.id for a in arts)
        dropped = {a.type for a in cap.artefacts} - keep_types
        already = {m.modality for m in cap.missing}
        missing = [m.model_dump() for m in cap.missing]
        missing += [{"modality": t, "reason": reason} for t in sorted(dropped - already, key=lambda t: t.value)]
        captures.append({**cap.model_dump(), "artefacts": [a.model_dump() for a in arts], "missing": missing})
    indicators = []
    for i in incident.indicators:
        aid = i.provenance.artefact_id
        if i.layer is Layer.collector:
            # Kept in every mode; a pointer at a dropped artefact is cleared so the incident validates.
            d = i.model_dump()
            if aid not in kept_artefacts:
                d["provenance"] = {**d["provenance"], "artefact_id": None}
            indicators.append(d)
        elif aid is not None and aid in kept_artefacts:
            indicators.append(i.model_dump())
    return Incident.model_validate({**incident.model_dump(), "captures": captures, "indicators": indicators})


def degrade(dataset: Dataset, mode: str) -> Dataset:
    """Degrade every incident of `dataset` to `mode`.

    Links, campaigns and assignments are engine output that may cite dropped indicators, so the
    result holds incidents only; re-run the engine on it.
    """
    return Dataset(
        name=f"{dataset.name}+degraded:{mode}",
        description=dataset.description,
        incidents=[degrade_incident(i, mode) for i in dataset.incidents],
    )


# --------------------------------------------------------------------------------------------
# E4: distractors
# --------------------------------------------------------------------------------------------

ALL_BUT_ECOSYSTEM = frozenset(Relation) - {Relation.ecosystem}
CAMPAIGN_LEVEL = frozenset({Relation.campaign, Relation.operator, Relation.tracking_account})


@dataclass(frozen=True)
class Expectation:
    """What an engine must (not) do with a distractor's subject captures.

    forbidden: link or campaign relations that must never join a subject to a capture of
        `against` (None means any capture outside the subjects).
    expected: relations a subject should share with `expected_with` (reported, not a failure).
    never_assign: the open-set decision must be `new` or `review`.
    decisions_unchanged: clustering and assignment output must be identical with and without
        the perturbation (collector invariance).
    """

    forbidden: frozenset[Relation] = frozenset()
    against: frozenset[str] | None = None
    expected: frozenset[Relation] = frozenset()
    expected_with: frozenset[str] = frozenset()
    never_assign: bool = False
    decisions_unchanged: bool = False


@dataclass(frozen=True)
class Distractor:
    """A perturbation to add to (or swap into) a reference dataset, with its expected answer.

    incidents: new incidents to add, or replacements when `replaces` is set.
    replaces: ids of reference incidents that `incidents` replace one for one (same ids).
    subjects: capture ids under test.
    """

    code: str  # E4 letter: a..f
    name: str
    subjects: frozenset[str]
    expect: Expectation
    incidents: tuple[Incident, ...] = ()
    replaces: tuple[str, ...] = ()
    note: str = ""


# ---- shared helpers ------------------------------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_HEX = frozenset("0123456789abcdef")


def _fresh_token(token: str, rng: random.Random) -> str:
    """A random token of the same length and character classes (hex stays hex)."""
    hexlike = set(token) <= _HEX
    for _ in range(100):
        out = []
        for ch in token:
            if ch.isdigit():
                out.append(rng.choice(string.digits))
            elif hexlike:
                out.append(rng.choice("abcdef"))
            elif ch.isupper():
                out.append(rng.choice(string.ascii_uppercase))
            else:
                out.append(rng.choice(string.ascii_lowercase))
        new = "".join(out)
        if new != token:
            return new
    raise ValueError("could not draw a fresh token")  # only for 1-char tokens of one class


def _id_tokens(value: str) -> list[str]:
    """Tokens of a value that carry identity: length >= 4 with a digit; else every token."""
    toks = _TOKEN.findall(value)
    ided = [t for t in toks if len(t) >= 4 and any(c.isdigit() for c in t)]
    return ided or toks


def _reid(prefix: str, x: str) -> str:
    return f"{prefix}{x}"


def _map_strings(obj, fn):
    """`fn` applied to every string in a nested dict/list; enum members and other values are kept."""
    if isinstance(obj, dict):
        return {k: _map_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    if isinstance(obj, str) and not isinstance(obj, Enum):
        return fn(obj)
    return obj


def _copy_captures(
    incident: Incident,
    capture_ids: Iterable[str],
    keep: callable,
    *,
    new_incident_id: str,
    source: str,
    prefix: str,
    string_map: callable | None = None,
    drop_artefacts: bool = False,
    missing_reason: str | None = None,
) -> Incident:
    """Copy selected captures and indicators of `incident` into a new synthetic incident.

    Ids get `prefix`. Artefacts lose `path` and `sha256`: the synthetic item has no file on disk,
    and a file hash would make it an exact duplicate of the original. With `drop_artefacts`,
    artefacts not cited by a kept indicator are removed and listed as missing. With `string_map`,
    it is applied to every string of the copied captures and indicators (not to the new incident's
    own id or source).
    """
    capture_ids = set(capture_ids)
    inds = [i for i in incident.indicators if i.capture_id in capture_ids and keep(i)]
    cited = {i.provenance.artefact_id for i in inds if i.provenance.artefact_id}
    captures = []
    for cap in incident.captures:
        if cap.id not in capture_ids:
            continue
        arts = [a for a in cap.artefacts if not drop_artefacts or a.id in cited]
        missing = [m.model_dump() for m in cap.missing]
        if drop_artefacts and missing_reason:
            gone = {a.type for a in cap.artefacts} - {a.type for a in arts} - {m.modality for m in cap.missing}
            missing += [{"modality": t, "reason": missing_reason} for t in sorted(gone, key=lambda t: t.value)]
        captures.append({
            **cap.model_dump(),
            "id": _reid(prefix, cap.id),
            "duplicate_of": None,
            "artefacts": [{**a.model_dump(), "id": _reid(prefix, a.id), "path": None, "sha256": None} for a in arts],
            "missing": missing,
        })
    indicators = []
    for ind in inds:
        d = ind.model_dump()
        d["id"] = _reid(prefix, ind.id)
        d["capture_id"] = _reid(prefix, ind.capture_id)
        aid = ind.provenance.artefact_id
        d["provenance"] = {**d["provenance"], "artefact_id": _reid(prefix, aid) if aid else None}
        indicators.append(d)
    if string_map is not None:
        captures = _map_strings(captures, string_map)
        indicators = _map_strings(indicators, string_map)
    return Incident.model_validate({
        "id": new_incident_id,
        "source": source,
        "source_ref": None,
        "captures": captures,
        "indicators": indicators,
    })


def _find_capture(dataset: Dataset, capture_id: str) -> tuple[Incident, Capture]:
    inc = dataset.incident_of(capture_id)
    return inc, next(c for c in inc.captures if c.id == capture_id)


# ---- (a) natural must-not-link pairs ---------------------------------------------------------

def natural_must_not_link(
    pairs: Iterable[tuple[Iterable[str], Iterable[str]]],
    relations: Iterable[Relation] = (Relation.campaign,),
) -> list[Distractor]:
    """(a) Natural must-not-link pairs from the ground-truth labels (the labelled set supplies them).

    Each pair is (capture ids, capture ids) that must not share a link or campaign under any of
    `relations`. Nothing is added to the dataset.
    """
    out = []
    forbidden = frozenset(Relation(r) for r in relations)
    for n, (a, b) in enumerate(pairs):
        out.append(Distractor(
            code="a",
            name=f"natural-{n}",
            subjects=frozenset(a),
            expect=Expectation(forbidden=forbidden, against=frozenset(b)),
            note="natural must-not-link pair",
        ))
    return out


# ---- (b) synthetic kit copy ------------------------------------------------------------------

KIT_COPY_LAYERS = frozenset({Layer.tenant, Layer.owner, Layer.landing, Layer.tracking_account, Layer.tracker_instance})
KIT_COPY_KINDS = re.compile(r"pixel|gateway|tenant|landing")


def kit_copy(dataset: Dataset, capture_id: str, *, seed: int = 0, tag: str = "0") -> Distractor:
    """(b) Same kit, fresh operator: a copy of one capture with tenant, landing, pixel and gateway
    values replaced by random values of the same shape.

    Replaced indicators are those in `KIT_COPY_LAYERS` or whose kind matches `KIT_COPY_KINDS`.
    Their identity tokens are scrubbed, as whole alphanumeric tokens, from every string of the
    copy (values, provenance, ids), so a landing path inside a URL or a locator keeps no original
    identity. Vector-only indicators are dropped, since a copied vector would keep the original
    store and key. Expected: template link to the original, no campaign/operator/tracking link,
    never assigned.
    """
    rng = random.Random(f"{seed}:{capture_id}:{tag}")
    inc, _ = _find_capture(dataset, capture_id)

    def replaced(i: Indicator) -> bool:
        return i.layer in KIT_COPY_LAYERS or bool(KIT_COPY_KINDS.search(i.kind))

    mapping: dict[str, str] = {}
    for ind in inc.indicators:
        if ind.capture_id == capture_id and replaced(ind) and ind.value is not None:
            for tok in _id_tokens(ind.value):
                if tok not in mapping:
                    mapping[tok] = _fresh_token(tok, rng)
    # Whole alphanumeric tokens only, longest first; the same map scrubs every string of the copy.
    alts = "|".join(re.escape(t) for t in sorted(mapping, key=len, reverse=True))
    scrub_re = re.compile(rf"(?<![A-Za-z0-9])({alts})(?![A-Za-z0-9])") if mapping else None

    def scrub(s: str) -> str:
        return scrub_re.sub(lambda m: mapping[m.group(1)], s) if scrub_re else s

    prefix = f"syn-kitcopy-{tag}:"
    new = _copy_captures(
        inc, [capture_id],
        lambda i: i.vector_ref is None,  # a copied vector would keep the original store and key
        new_incident_id=f"syn-kitcopy-{tag}", source="synthetic", prefix=prefix, string_map=scrub,
    )
    return Distractor(
        code="b",
        name=f"kit_copy:{capture_id}",
        subjects=frozenset(c.id for c in new.captures),
        expect=Expectation(
            forbidden=CAMPAIGN_LEVEL,
            expected=frozenset({Relation.template}),
            expected_with=frozenset({capture_id}),
            never_assign=True,
        ),
        incidents=(new,),
        note="same template, not same campaign",
    )


# ---- (c) platform-only page ------------------------------------------------------------------

def platform_only(dataset: Dataset, capture_id: str, *, tag: str = "0") -> Distractor:
    """(c) A page that carries only platform constants (aio main.js, hue-rotate, x-token
    headers): platform-layer indicators, plus indicators of any layer at platform or ecosystem
    level. Expected: no link above ecosystem, never assigned.
    """
    inc, _ = _find_capture(dataset, capture_id)
    keep = lambda i: i.layer is Layer.platform or i.level in (Level.platform, Level.ecosystem)
    new = _copy_captures(
        inc, [capture_id], keep, new_incident_id=f"syn-platform-{tag}", source="synthetic",
        prefix=f"syn-platform-{tag}:", drop_artefacts=True, missing_reason="distractor:platform_only",
    )
    return Distractor(
        code="c",
        name=f"platform_only:{capture_id}",
        subjects=frozenset(c.id for c in new.captures),
        expect=Expectation(forbidden=ALL_BUT_ECOSYSTEM, never_assign=True),
        incidents=(new,),
        note="must not link to any campaign",
    )


# ---- (d) collector injection -----------------------------------------------------------------

COLLECTOR_INJECTION_KINDS = ("browser_fingerprint", "analyst_ip", "fbclid_session", "capture_day")


def collector_injection(dataset: Dataset, capture_a: str, capture_b: str, *, seed: int = 0) -> Distractor:
    """(d) Inject one identical fingerprint, analyst IP, fbclid segment and capture day into two
    unrelated captures as collector-layer indicators. The two incidents keep their ids and replace
    the originals. Expected: zero decisions change.
    """
    rng = random.Random(f"{seed}:{capture_a}:{capture_b}")
    values = {
        "browser_fingerprint": "".join(rng.choice("0123456789abcdef") for _ in range(32)),
        "analyst_ip": f"10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}",
        "fbclid_session": "IwAR" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(24)),
        "capture_day": f"2026-10-{rng.randrange(1, 29):02d}",
    }
    out: list[Incident] = []
    for cap_id in (capture_a, capture_b):
        inc, _ = _find_capture(dataset, cap_id)
        if any(i.id == inc.id for i in out):
            raise ValueError("collector injection needs captures from two different incidents")
        extra = [
            {
                "id": f"{cap_id}:inj:{kind}",
                "capture_id": cap_id,
                "layer": Layer.collector,
                "kind": kind,
                "level": Level.capture,
                "value": value,
                "observed": "static",
                "provenance": {"note": "injected collector distractor (E4d)"},
                "extractor": "ftf.evaluate.perturb@0",
            }
            for kind, value in values.items()
        ]
        d = inc.model_dump()
        d["indicators"] = d["indicators"] + extra
        out.append(Incident.model_validate(d))
    return Distractor(
        code="d",
        name=f"collector_injection:{capture_a}+{capture_b}",
        subjects=frozenset({capture_a, capture_b}),
        expect=Expectation(decisions_unchanged=True),
        incidents=tuple(out),
        replaces=tuple(i.id for i in out),
        note="collector fields must never change a decision",
    )


# ---- (e) brand negatives ---------------------------------------------------------------------

CORPUS_TAGS_FILE = "corpus_kind.jsonl"


def read_corpus_tags(path: str | Path) -> dict[str, str]:
    """Incident id -> corpus kind, from the background corpus's tag file (one JSON row per
    incident with `incident_id` and `corpus_kind`). The tags are not indicators."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["incident_id"]] = row["corpus_kind"]
    return out


def brand_negatives(
    corpus: Dataset,
    tags: Mapping[str, str],
    *,
    kinds: Iterable[str] = ("must_not_link",),
    prefix: str = "brandneg:",
) -> list[Distractor]:
    """(e) Real pages of the impersonated outlets, as already-parsed contract incidents.

    The background corpus (batch 1) is a contract Dataset whose incidents are tagged by
    `read_corpus_tags` (`corpus_kind.jsonl` beside the dataset). Only incidents whose tag is in
    `kinds` are used. The default `must_not_link` leaves out `must_not_link_blocked`, the
    fetches the corpus marks as blocked, which are not the outlet's pages. Ids get `prefix` to
    avoid clashes with the reference. Expected: nothing above the brand (ecosystem) level, never
    assigned.
    """
    wanted = set(kinds)
    out = []
    for inc in corpus.incidents:
        if tags.get(inc.id) not in wanted:
            continue
        new = _copy_captures(
            inc, [c.id for c in inc.captures], lambda i: True,
            new_incident_id=_reid(prefix, inc.id), source=inc.source, prefix=prefix,
        )
        out.append(Distractor(
            code="e",
            name=f"brand_negative:{inc.id}",
            subjects=frozenset(c.id for c in new.captures),
            expect=Expectation(forbidden=ALL_BUT_ECOSYSTEM, never_assign=True),
            incidents=(new,),
            note="real outlet page: must not link above brand level",
        ))
    return out


# ---- (f) celebrity-only ----------------------------------------------------------------------

CELEBRITY_KINDS = re.compile(r"face|image|photo|phash|img")


def celebrity_only(dataset: Dataset, capture_id: str, *, kinds: re.Pattern | str = CELEBRITY_KINDS, tag: str = "0") -> Distractor:
    """(f) A new incident carrying only the creative-layer face/image indicators of a capture
    (a celebrity photo in another context). Expected: review or new, never assign.
    """
    pat = re.compile(kinds) if isinstance(kinds, str) else kinds
    inc, _ = _find_capture(dataset, capture_id)
    keep = lambda i: i.layer is Layer.creative and bool(pat.search(i.kind))
    if not any(keep(i) for i in inc.indicators if i.capture_id == capture_id):
        raise ValueError(f"capture {capture_id} has no creative face/image indicator")
    new = _copy_captures(
        inc, [capture_id], keep, new_incident_id=f"syn-celebrity-{tag}", source="synthetic",
        prefix=f"syn-celebrity-{tag}:", drop_artefacts=True, missing_reason="distractor:celebrity_only",
    )
    return Distractor(
        code="f",
        name=f"celebrity_only:{capture_id}",
        subjects=frozenset(c.id for c in new.captures),
        expect=Expectation(forbidden=ALL_BUT_ECOSYSTEM, never_assign=True),
        incidents=(new,),
        note="a face alone is never campaign evidence",
    )


# --------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------

def _outside(d: Distractor, capture_id: str) -> bool:
    if capture_id in d.subjects:
        return False
    return d.expect.against is None or capture_id in d.expect.against


def link_violations(d: Distractor, clustered: Dataset) -> list[str]:
    """Forbidden links and campaigns joining a subject to an outside capture."""
    bad = []
    forbidden = d.expect.forbidden
    for l in clustered.links:
        if l.relation not in forbidden:
            continue
        if (l.a in d.subjects and _outside(d, l.b)) or (l.b in d.subjects and _outside(d, l.a)):
            bad.append(f"link {l.id} ({l.relation.value}, {l.level.value}) {l.a} <-> {l.b}")
    for c in clustered.campaigns:
        if c.relation not in forbidden:
            continue
        ids = set(c.capture_ids)
        if ids & d.subjects and any(_outside(d, x) for x in ids):
            bad.append(f"campaign {c.id} ({c.relation.value}) joins subjects with {len(ids - d.subjects)} other captures")
    return bad


def expected_met(d: Distractor, clustered: Dataset) -> bool | None:
    """Whether every expected relation joins a subject to `expected_with`; None if nothing expected."""
    if not d.expect.expected:
        return None
    found = set()
    for l in clustered.links:
        if l.relation in d.expect.expected and (
            (l.a in d.subjects and l.b in d.expect.expected_with) or (l.b in d.subjects and l.a in d.expect.expected_with)
        ):
            found.add(l.relation)
    for c in clustered.campaigns:
        ids = set(c.capture_ids)
        if c.relation in d.expect.expected and ids & d.subjects and ids & d.expect.expected_with:
            found.add(c.relation)
    return found >= d.expect.expected


def assignment_violations(d: Distractor, a: Assignment, reference: Dataset) -> list[str]:
    """An `assign` that the expectation forbids."""
    if a.decision is not Decision.assign:
        return []
    if d.expect.never_assign:
        return [f"{a.incident_id} assigned to {a.campaign_id}"]
    camp = next((c for c in reference.campaigns if c.id == a.campaign_id), None)
    if camp is None:
        return [f"{a.incident_id} assigned to unknown campaign {a.campaign_id}"]
    if camp.relation in d.expect.forbidden and any(_outside(d, x) for x in camp.capture_ids):
        return [f"{a.incident_id} assigned to {camp.id} ({camp.relation.value})"]
    return []


def _decision_view(ds: Dataset) -> tuple[set, set, dict]:
    camps = {c.id: frozenset(c.capture_ids) for c in ds.campaigns}
    links = {(frozenset((l.a, l.b)), l.relation.value, l.level.value) for l in ds.links}
    clusters = {(c.relation.value, frozenset(c.capture_ids)) for c in ds.campaigns}
    assigns = {
        a.incident_id: (a.decision.value, camps.get(a.campaign_id) if a.campaign_id else None)
        for a in ds.assignments
    }
    return links, clusters, assigns


def decision_diff(before: Dataset, after: Dataset) -> list[str]:
    """Differences in links (pair, relation, level), campaigns (relation, members) and
    assignment decisions between two engine outputs. Campaign ids may differ; members may not.
    """
    lb, cb, ab = _decision_view(before)
    la, ca, aa = _decision_view(after)
    out = []
    for k in sorted(lb - la, key=str):
        out.append(f"link lost: {sorted(k[0])} {k[1]}/{k[2]}")
    for k in sorted(la - lb, key=str):
        out.append(f"link added: {sorted(k[0])} {k[1]}/{k[2]}")
    for k in sorted(cb - ca, key=str):
        out.append(f"campaign lost: {k[0]} {sorted(k[1])}")
    for k in sorted(ca - cb, key=str):
        out.append(f"campaign added: {k[0]} {sorted(k[1])}")
    for iid in sorted(set(ab) | set(aa)):
        if ab.get(iid) != aa.get(iid):
            out.append(f"assignment of {iid} changed")
    return out


def with_incidents(dataset: Dataset, add: Iterable[Incident] = (), drop: Iterable[str] = (), *, name: str | None = None) -> Dataset:
    """Incidents of `dataset` minus `drop` (incident ids) plus `add`, without engine output.

    `duplicate_of` pointers to removed captures are cleared so the result validates.
    """
    drop = set(drop)
    add = list(add)
    keep = [i for i in dataset.incidents if i.id not in drop]
    present = {c.id for i in keep + add for c in i.captures}
    incidents = []
    for inc in keep + add:
        if all(c.duplicate_of is None or c.duplicate_of in present for c in inc.captures):
            incidents.append(inc)
            continue
        d = inc.model_dump()
        for c in d["captures"]:
            if c["duplicate_of"] is not None and c["duplicate_of"] not in present:
                c["duplicate_of"] = None
        incidents.append(Incident.model_validate(d))
    return Dataset(name=name or dataset.name, description=dataset.description, incidents=incidents)


def hold_out(dataset: Dataset, incident_ids: Iterable[str], *, name: str | None = None) -> Dataset:
    """`dataset` without the given incidents and without their exact duplicates, at capture level.

    The held-out captures are closed under exact duplication in both directions: a capture is
    removed when its canonical capture (`duplicate_of`, or itself) is the canonical of a held-out
    capture, wherever it sits. An incident whose captures all go is dropped; a mixed incident
    keeps its other captures, with their indicators. So no kept capture points at a removed one.
    Engine output is not carried over.
    """
    drop = set(incident_ids)
    held = [c.id for i in dataset.incidents if i.id in drop for c in i.captures]
    canon = {c.id: (c.duplicate_of or c.id) for i in dataset.incidents for c in i.captures}
    roots = {canon[c] for c in held}
    gone = {cid for cid, root in canon.items() if root in roots}
    incidents = []
    for inc in dataset.incidents:
        if inc.id in drop:
            continue
        if not any(c.id in gone for c in inc.captures):
            incidents.append(inc)
            continue
        kept = {c.id for c in inc.captures if c.id not in gone}
        if not kept:
            continue
        d = inc.model_dump()
        d["captures"] = [c for c in d["captures"] if c["id"] in kept]
        d["indicators"] = [i for i in d["indicators"] if i["capture_id"] in kept]
        incidents.append(Incident.model_validate(d))
    return Dataset(name=name or dataset.name, description=dataset.description, incidents=incidents)


def replace_incidents(dataset: Dataset, replacements: Mapping[str, Incident] | Iterable[Incident]) -> Dataset:
    """`dataset` with incidents swapped for same-id replacements, order kept, engine output dropped."""
    rep = dict(replacements) if isinstance(replacements, Mapping) else {i.id: i for i in replacements}
    unknown = set(rep) - {i.id for i in dataset.incidents}
    if unknown:
        raise KeyError(f"no incident to replace: {sorted(unknown)}")
    return Dataset(
        name=dataset.name,
        description=dataset.description,
        incidents=[rep.get(i.id, i) for i in dataset.incidents],
    )


__all__ = [
    "ALL_BUT_ECOSYSTEM",
    "CAMPAIGN_LEVEL",
    "MODES",
    "Distractor",
    "Expectation",
    "assignment_violations",
    "brand_negatives",
    "celebrity_only",
    "collector_injection",
    "decision_diff",
    "degrade",
    "degrade_incident",
    "expected_met",
    "hold_out",
    "kit_copy",
    "link_violations",
    "natural_must_not_link",
    "platform_only",
    "read_corpus_tags",
    "replace_incidents",
    "with_incidents",
]
