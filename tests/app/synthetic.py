"""A small synthetic campaign base for the app tests (no organiser data)."""

from ftf.contract import CONTRACT_VERSION, Dataset

HOSTILE = "<a href='http://evil.example/x'>click</a>"


def _ind(id, capture_id, layer, kind, value, level="operator"):
    return {"id": id, "capture_id": capture_id, "layer": layer, "kind": kind, "level": level,
            "value": value, "observed": "static", "extractor": "test@0"}


def _incident(n, day):
    return {
        "id": f"i{n}",
        "source": "test",
        "captures": [
            {"id": f"i{n}/ad", "stage": "ad", "captured_on": day},
            {"id": f"i{n}/lander", "stage": "lander", "captured_on": day},
        ],
        "indicators": [
            _ind(f"i{n}/fp", f"i{n}/lander", "collector", "aio_fingerprint", "fp-1", "capture"),
            _ind(f"i{n}/kit", f"i{n}/lander", "kit", "kit_hash", "k-1" if n > 1 else "k-0", "platform"),
            _ind(f"i{n}/adtext", f"i{n}/ad", "creative", "ad_text_hash", HOSTILE, "campaign"),
        ] + ([_ind(f"i{n}/pixel", f"i{n}/lander", "tracking_account", "meta_pixel", "px-1")] if n <= 2 else []),
    }


def _row(layer, kind, status, value, bits, ids):
    return {"layer": layer, "kind": kind, "status": status, "value": value, "bits": bits, "indicator_ids": ids}


def _fp(a, b):
    return _row("collector", "aio_fingerprint", "collector_excluded", "fp-1", 0.0, [f"i{a}/fp", f"i{b}/fp"])


def _link(id, a, b, relation, level, bits, evidence):
    return {"id": id, "a": a, "b": b, "relation": relation, "level": level, "score_bits": bits,
            "method": "test", "evidence": evidence}


def dataset() -> Dataset:
    links = [
        _link("L-px", "i1/lander", "i2/lander", "tracking_account", "strict", 9.0, [
            _row("tracking_account", "meta_pixel", "shared", "px-1", 9.0, ["i1/pixel", "i2/pixel"]),
            _fp(1, 2),
        ]),
        _link("L-k23", "i2/lander", "i3/lander", "template", "medium", 5.0, [
            _row("kit", "kit_hash", "shared", "k-1", 5.0, ["i2/kit", "i3/kit"]),
        ]),
        _link("L-k34", "i3/lander", "i4/lander", "template", "loose", 1.0, [
            _row("kit", "kit_hash", "shared", "k-1", 1.0, ["i3/kit", "i4/kit"]),
            _row("tracking_account", "meta_pixel", "missing", None, 0.0, []),
        ]),
        _link("L-ad", "i1/ad", "i3/ad", "template", "loose", 4.0, [
            _row("creative", "ad_text_hash", "shared", HOSTILE, 4.0, ["i1/adtext", "i3/adtext"]),
        ]),
        # Only collector evidence: must never become an edge.
        _link("L-fp", "i1/lander", "i4/lander", "operator", "loose", 0.0, [_fp(1, 4)]),
    ]
    campaigns = [
        {"id": "C-px", "label": "Pixel campaign", "relation": "tracking_account",
         "capture_ids": ["i1/lander", "i2/lander"], "link_ids": ["L-px"]},
        {"id": "C-kit", "label": "Kit family", "relation": "template",
         "capture_ids": ["i2/lander", "i3/lander", "i4/lander"], "link_ids": []},
    ]
    days = ["2026-10-01", "2026-10-05", "2026-10-09", "2026-11-02"]
    return Dataset.model_validate({
        "contract_version": CONTRACT_VERSION,
        "name": "synthetic",
        "incidents": [_incident(n, days[n - 1]) for n in range(1, 5)],
        "links": links,
        "campaigns": campaigns,
    })
