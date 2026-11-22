"""Learned stability: per-layer persistence s from the scammers' own campaign objects.

The aio platform exposes campaign and owner ids (`aio_campaign`, `aio_owner`, Ingestion's kinds).
Pairs of captures inside one aio campaign (relation `campaign`) or one aio owner (relation
`operator`) are positives; pairs of captures of two different owners are negatives.

For every layer, and every kind with data, persistence is the share of positive pairs (both sides
carrying the layer / kind) that share a value. Missing on either side is skipped, never counted as
a disagreement (MAR). Pairs inside one group are dependent, so a group of m carriers
contributes m − 1 effective observations at its own sharing rate (macro-weighting: a big campaign
does not dominate). Beta shrinkage toward the prior:

    s_layer = (x_eff + κ·s_prior) / (n_eff + κ),   s_prior inverted from the prior table
    s_kind  = (x_eff_kind + κ·s_layer) / (n_eff_kind + κ)

The label's own layer is held out: it is never estimated from the pairs it defines (attacker-
labelled self-supervision). Its aliases (e.g. the campaign name next to the campaign id)
sit in the same layer and are held out with it.

Weight of a kind under a learned relation: bits = log2[(s + (1 − s)·u)/u], times the prior's
relation scale where that is below 1 (a copy prior for kits and forms that same-operator pairs cannot
measure). u is the frequency of a value carried by one operator, over the larger of the background
corpus and the sample, never below 1/N; unique-by-construction ids use the type prior 1/N. Relations
without labels keep the prior's absolute bits. Kinds without positive data, without a usable u,
or with prior weight 0 (dates, flags) keep the prior entry. Learned bits never exceed the prior by
more than `MAX_RAISE` bits.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from ftf.contract.models import Dataset, Layer, Relation
from ftf.linking.background import ALPHA, TYPE_PRIOR_SHARE, BackgroundCounts, unique_by_construction
from ftf.linking.hierarchy import ANCHOR_KINDS, CHAIN
from ftf.linking.weights import HAND_TABLE, PRIVATE_LAYERS, KindWeight, WeightTable

KAPPA = 4.0  # Beta prior strength (pseudo-observations) toward the prior / layer estimate
MAX_NEG_PAIRS = 20000  # negatives only feed report counts
# Learned bits may lower a prior freely but raise it by at most this much: ~20 positive pairs and a corpus of
# tens of operators cannot measure how often a value is copied with a kit (±2 bits).
MAX_RAISE = 2.0
S_CLIP = (0.02, 0.98)
LABELS: dict[Relation, str] = {Relation.campaign: "aio_campaign", Relation.operator: "aio_owner"}
NEGATIVE_LABEL = "aio_owner"
FORMAT = "ftf-weights"
VERSION = 1
METHOD = "learned@0.1"
# Chain ids that are single-valued per capture and campaign-stable: a different value is a conflict row
# (never a cannot-link, see `ledger.has_private_conflict`) under the relations they name. They say
# nothing about the relations listed: an owner runs several aio campaigns, so a different campaign id
# is no evidence against the same owner.
SINGLE_VALUED_CHAIN: dict[str, frozenset] = {
    "aio_campaign": frozenset({Relation.operator, Relation.tracking_account, Relation.template}),
}

Feats = dict[tuple[Layer, str], set[str]]


def _clip(s: float) -> float:
    return min(max(s, S_CLIP[0]), S_CLIP[1])


def s_from_bits(bits: float, u: float) -> float:
    """Invert bits = log2[(s + (1 − s)u)/u] for s (clipped to S_CLIP)."""
    if u >= 1:
        return S_CLIP[1]
    return _clip((2.0 ** bits * u - u) / (1.0 - u))


def bits_from_s(s: float, u: float) -> float:
    return max(math.log2((s + (1.0 - s) * u) / u), 0.0)


def _rate(groups: list[list[set[str] | None]]) -> tuple[float, float, int, int]:
    """(x_eff, n_eff, n_pairs, n_shared) over groups of per-member value sets (None = missing)."""
    x_eff = n_eff = 0.0
    n_pairs = n_shared = 0
    for members in groups:
        present = [m for m in members if m]
        if len(present) < 2:
            continue
        pairs = list(combinations(present, 2))
        shared = sum(1 for a, b in pairs if a & b)
        e = len(present) - 1
        x_eff += e * shared / len(pairs)
        n_eff += e
        n_pairs += len(pairs)
        n_shared += shared
    return x_eff, n_eff, n_pairs, n_shared


def _layer_values(f: Feats, layer: Layer, held_kinds: set[str]) -> set[str] | None:
    vals = {f"{k}={v}" for (ly, k), vs in f.items() if ly is layer and k not in held_kinds for v in vs}
    return vals or None


def learn_weights(
    ds: Dataset, background: BackgroundCounts | None = None, prior: WeightTable = HAND_TABLE, kappa: float = KAPPA
) -> tuple[WeightTable, dict]:
    """Learn a `WeightTable` (same shape as the prior) and a JSON-ready report (kinds and counts, no values)."""
    from ftf.linking.ledger import ScoringContext  # local: ledger imports background, which is fine

    ctx = ScoringContext.from_dataset(ds, weights=prior, chain_rule=False)
    feats: dict[str, Feats] = {
        cap: {key: set(v.ids) for key, v in vals.items() if key[0] is not Layer.collector}
        for cap, vals in ctx.values.items()
    }
    kind_layer: dict[str, Layer] = {}
    layer_count: dict[str, dict[Layer, int]] = defaultdict(lambda: defaultdict(int))
    for f in feats.values():
        for layer, kind in f:
            layer_count[kind][layer] += 1
    for kind, counts in layer_count.items():
        kind_layer[kind] = max(counts, key=lambda ly: (counts[ly], ly.value))

    def labels_of(kind: str) -> dict[str, set[str]]:
        return {cap: set().union(*(vs for (_, k), vs in f.items() if k == kind)) for cap, f in feats.items()
                if any(k == kind for _, k in f)}

    u_ref = _reference_u(ctx, background)
    report: dict = {
        "method": METHOD, "kappa": kappa, "alpha": background.alpha if background else ALPHA,
        "n_captures": ctx.n, "n_operators_sample": ctx.n_groups,
        "background": None if background is None else {
            "n_operators": background.n_operators, "n_incidents": background.n_incidents, "sources": background.sources,
        },
        "relations": {}, "kinds": {},
    }

    # negatives: capture pairs of two different owners
    owners = labels_of(NEGATIVE_LABEL)
    neg_pairs = _negative_pairs(owners)

    learned_s: dict[Relation, dict[str, float]] = {}
    learned_n: dict[Relation, dict[str, float]] = {}
    for relation, label in LABELS.items():
        lab = labels_of(label)
        if not lab:
            report["relations"][relation.value] = {"label_kind": label, "status": "no labels in dataset"}
            continue
        # the label's layer and the layers of its ancestor chain ids (an aio campaign sits under one owner and
        # one tenant, so every positive pair shares them by construction: they cannot be learned here)
        label_level = ANCHOR_KINDS[label]
        coarser = CHAIN[CHAIN.index(label_level):]
        held_layers = {kind_layer[label]} | {kind_layer[k] for k, lv in ANCHOR_KINDS.items() if lv in coarser and k in kind_layer}
        held_kinds = {k for k, ly in kind_layer.items() if ly in held_layers} | {label}
        members: dict[str, list[str]] = defaultdict(list)
        for cap, vals in lab.items():
            for v in vals:
                members[v].append(cap)
        groups = [sorted(m) for m in members.values() if len(m) >= 2]
        rel: dict = {
            "label_kind": label, "held_out_layers": sorted(ly.value for ly in held_layers),
            "n_groups": len(groups), "n_singletons": sum(1 for m in members.values() if len(m) < 2),
            "n_pos_pairs": sum(len(g) * (len(g) - 1) // 2 for g in groups), "n_neg_pairs": len(neg_pairs),
            "layers": {}, "kinds": {},
        }
        s_layer: dict[Layer, float] = {}
        for layer in Layer:
            if layer is Layer.collector:
                continue
            if layer in held_layers:
                rel["layers"][layer.value] = {"held_out": True}
                continue
            x, n, npairs, nshared = _rate([[_layer_values(feats[c], layer, held_kinds) for c in g] for g in groups])
            negs = [(a, b) for a, b in neg_pairs
                    if _layer_values(feats[a], layer, held_kinds) and _layer_values(feats[b], layer, held_kinds)]
            neg_shared = sum(1 for a, b in negs
                             if _layer_values(feats[a], layer, held_kinds) & _layer_values(feats[b], layer, held_kinds))
            s0 = s_from_bits(prior.layer_defaults.get(layer, 0.0) * prior.relation_scale.get(layer, {}).get(relation, 0.0),
                             _layer_u(u_ref, layer))
            s = (x + kappa * s0) / (n + kappa)
            s_layer[layer] = s
            rel["layers"][layer.value] = {
                "s": round(s, 3), "s_prior": round(s0, 3), "n_pos": npairs, "n_pos_shared": nshared,
                "n_eff": round(n, 2), "shrinkage_weight": round(n / (n + kappa), 3),
                "n_neg": len(negs), "n_neg_shared": neg_shared,
            }
        ks: dict[str, float] = {}
        kn: dict[str, float] = {}
        for kind, layer in sorted(kind_layer.items()):
            if kind in held_kinds or layer is Layer.collector:
                continue
            x, n, npairs, nshared = _rate([[feats[c].get((layer, kind)) for c in g] for g in groups])
            s = (x + kappa * s_layer[layer]) / (n + kappa)
            negs = [(a, b) for a, b in neg_pairs if feats[a].get((layer, kind)) and feats[b].get((layer, kind))]
            neg_shared = sum(1 for a, b in negs if feats[a][(layer, kind)] & feats[b][(layer, kind)])
            ks[kind], kn[kind] = s, n  # a kind is learned only from its own positive pairs
            rel["kinds"][kind] = {
                "layer": layer.value, "s": round(s, 3), "n_pos": npairs, "n_pos_shared": nshared,
                "n_eff": round(n, 2), "shrinkage_weight": round(n / (n + kappa), 3),
                "n_neg": len(negs), "n_neg_shared": neg_shared,
            }
        learned_s[relation], learned_n[relation] = ks, kn
        report["relations"][relation.value] = rel

    kinds: dict[str, KindWeight] = dict(prior.kinds)
    for kind, layer in sorted(kind_layer.items()):
        if layer is Layer.collector:
            continue
        base = prior.weight(kind, layer)
        u = u_ref.get(kind)
        info: dict = {"layer": layer.value, "in_prior": kind in prior.kinds}
        if u is not None:
            info.update({"u_ref": round(u["u"], 5), "u_source": u["source"], "N": u["N"], "V": u["V"],
                         "type_prior": u["type_prior"]})
        absolute = {r: base.bits * prior.scale(kind, layer, r) for r in Relation}
        learned_rel = []
        if base.bits <= 0:
            info["fallback"] = "prior weight 0 (not an actor id)"
        elif u is None:
            info["fallback"] = "no rarity estimate"
        else:
            for relation, ks in learned_s.items():
                if kind not in ks or learned_n[relation].get(kind, 0) <= 0 or absolute[relation] <= 0:
                    continue
                # a hand relation scale below 1 is a copy prior (kits and forms are sold and copied across
                # operators) that same-operator pairs cannot measure: it stays a multiplier
                learned = bits_from_s(ks[kind], u["u"]) * min(prior.scale(kind, layer, relation), 1.0)
                if learned > absolute[relation] + MAX_RAISE:
                    info.setdefault("raise_capped", []).append(relation.value)
                absolute[relation] = min(learned, absolute[relation] + MAX_RAISE)
                learned_rel.append(relation.value)
            if not learned_rel:
                info["fallback"] = "no positive pairs"
        info["learned_relations"] = learned_rel
        top = max(absolute.values())
        info["bits"] = {r.value: round(b, 3) for r, b in absolute.items()}
        report["kinds"][kind] = info
        if not learned_rel or kind not in prior.kinds:
            # a kind the prior does not know stays unknown (reported, and warned about by `ftf score`/`link`)
            if learned_rel:
                info["not_in_table"] = "kind has no prior entry; learned bits reported only"
            continue
        kinds[kind] = KindWeight(
            layer=base.layer if kind in prior.kinds else layer, bits=round(top, 3),
            single_valued=base.single_valued, private=base.private or (kind not in prior.kinds and layer in PRIVATE_LAYERS),
            relations={r: (round(b / top, 4) if top > 0 else 0.0) for r, b in absolute.items()},
        )
    for kind, silent in SINGLE_VALUED_CHAIN.items():
        if kind not in kind_layer:
            continue
        base = kinds.get(kind) or prior.weight(kind, kind_layer[kind])
        absolute = {r: base.bits * (base.relations.get(r, 0.0) if base.relations is not None
                                    else prior.relation_scale.get(kind_layer[kind], {}).get(r, 0.0)) for r in Relation}
        top = max(absolute.values()) or base.bits
        kinds[kind] = KindWeight(
            layer=kind_layer[kind], bits=round(top, 3), single_valued=True, private=True,
            relations={r: (0.0 if r in silent else round(absolute[r] / top, 4) if top > 0 else 0.0) for r in Relation},
        )
        report["kinds"].setdefault(kind, {})["single_valued_chain_id"] = sorted(r.value for r in silent)
    table = WeightTable(
        kinds=kinds, layer_defaults=dict(prior.layer_defaults), relation_scale=prior.relation_scale,
        conflict_relations=prior.conflict_relations, conflict_bits=prior.conflict_bits,
        hub_bits=prior.hub_bits, static_discount=prior.static_discount,
    )
    return table, report


def _reference_u(ctx, background: BackgroundCounts | None) -> dict[str, dict]:
    """Per kind: u of a value carried by exactly one operator, over the larger of background and sample."""
    groups: dict[str, set[str]] = defaultdict(set)
    values: dict[str, set[str]] = defaultdict(set)
    layer_of: dict[str, Layer] = {}
    for (layer, kind, norm), incidents in ctx.index.items():
        if layer is Layer.collector:
            continue
        groups[kind] |= {ctx.group_of[i] for i in incidents}
        values[kind].add(norm)
        layer_of[kind] = layer
    alpha = background.alpha if background else ALPHA
    out: dict[str, dict] = {}
    for kind, gs in groups.items():
        n, v = len(gs), len(values[kind])
        unique = sum(unique_by_construction(x) for x in values[kind]) / v
        source = "sample"
        st = background.stats(layer_of[kind], kind) if background else None
        if st is not None:
            # max, not sum: the corpus may contain the sample itself (see `ScoringContext.rarity`)
            n = max(n, st.n_operators)
            v = max(v, st.n_values)
            unique = max(unique, st.unique_share) if st.n_values else unique
            source = "sample+background"
        if n < 2:
            continue
        type_prior = unique >= TYPE_PRIOR_SHARE
        u = 1.0 / n if type_prior else (1.0 + alpha) / (n + alpha * v)
        # never rarer than one operator in N: a multi-valued kind (asset hashes) has V >> N, and the Dirichlet
        # denominator alone would claim log2(V) bits the N operators cannot support (cap log2 N)
        u = max(u, 1.0 / n)
        out[kind] = {"u": min(u, 1.0), "N": n, "V": v, "source": source, "type_prior": type_prior,
                     "layer": layer_of[kind]}
    return out


def _layer_u(u_ref: dict[str, dict], layer: Layer) -> float:
    us = sorted(x["u"] for x in u_ref.values() if x["layer"] is layer)
    return us[len(us) // 2] if us else 0.1


# ---- JSON (no pickle) ----


def table_to_json(table: WeightTable) -> dict:
    return {
        "kinds": {
            k: {"layer": e.layer.value, "bits": e.bits, "single_valued": e.single_valued, "private": e.private,
                "relations": None if e.relations is None else {r.value: s for r, s in e.relations.items()}}
            for k, e in sorted(table.kinds.items())
        },
        "layer_defaults": {ly.value: b for ly, b in table.layer_defaults.items()},
        "relation_scale": {ly.value: {r.value: s for r, s in row.items()} for ly, row in table.relation_scale.items()},
        "conflict_relations": sorted(r.value for r in table.conflict_relations),
        "conflict_bits": table.conflict_bits, "hub_bits": table.hub_bits, "static_discount": table.static_discount,
    }


def table_from_json(doc: dict) -> WeightTable:
    return WeightTable(
        kinds={
            k: KindWeight(
                layer=Layer(e["layer"]), bits=float(e["bits"]), single_valued=bool(e["single_valued"]),
                private=bool(e["private"]),
                relations=None if e["relations"] is None else {Relation(r): float(s) for r, s in e["relations"].items()},
            )
            for k, e in doc["kinds"].items()
        },
        layer_defaults={Layer(ly): float(b) for ly, b in doc["layer_defaults"].items()},
        relation_scale={Layer(ly): {Relation(r): float(s) for r, s in row.items()} for ly, row in doc["relation_scale"].items()},
        conflict_relations=frozenset(Relation(r) for r in doc["conflict_relations"]),
        conflict_bits=float(doc["conflict_bits"]), hub_bits=float(doc["hub_bits"]),
        static_discount=float(doc["static_discount"]),
    )


def dump_weights(path: Path, table: WeightTable, report: dict, background: BackgroundCounts | None = None) -> None:
    doc = {"format": FORMAT, "version": VERSION, "method": METHOD, "table": table_to_json(table),
           "background": None if background is None else background.to_json(), "report": report}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=False), encoding="utf-8")


def load_weights(path: Path) -> tuple[WeightTable, BackgroundCounts | None, dict]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("format") != FORMAT or doc.get("version") != VERSION:
        raise ValueError(f"{path}: not an {FORMAT} v{VERSION} document")
    bg = BackgroundCounts.from_json(doc["background"]) if doc.get("background") else None
    return table_from_json(doc["table"]), bg, doc.get("report", {})


def _negative_pairs(owners: dict[str, set[str]]) -> list[tuple[str, str]]:
    """Capture pairs of two different owners, at most `MAX_NEG_PAIRS` (an even stride over all pairs, built
    lazily: report-only counts must not cost O(n²) memory)."""
    caps = sorted(owners)
    n = len(caps)
    total = n * (n - 1) // 2
    stride = max(1, -(-total // MAX_NEG_PAIRS))
    out: list[tuple[str, str]] = []
    for idx, (a, b) in enumerate(combinations(caps, 2)):
        if idx % stride == 0 and not owners[a] & owners[b]:
            out.append((a, b))
    return out
