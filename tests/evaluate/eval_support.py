"""Small synthetic prototype files and datasets (no organiser data)."""

from __future__ import annotations

import json
from pathlib import Path

T1 = "aaaaaaaa-1111-4111-8111-111111111111"
T2 = "bbbbbbbb-2222-4222-8222-222222222222"
OWNER = "cccccccc-3333-4333-8333-333333333333"
PLATFORM = "dddddddd-4444-4444-8444-444444444444"
HANDLE = "0123456789abcdef0123456789abcdef"

SIGNALS = {
    "_meta": {"note": "synthetic"},
    "1": {
        "landing_cdn_account": [PLATFORM, T1],
        "action_files_id": [PLATFORM],
        "fb_pixel": ["111"],
        "param_shape": [f"{HANDLE}={{hex32}}"],
        "capture_day": ["2026-10-09"],
        "html_lang": ["pl"],
    },
    "p1": {"landing_cdn_account": [T1], "fb_pixel": ["111"], "html_lang": ["pl"], "capture_day": ["2026-10-08"]},
    "p2": {"landing_cdn_account": [T1], "fb_pixel": ["111"], "html_lang": ["pl"]},
    "p6": {
        "landing_cdn_account": [T2],
        "html_comment": [f"<img src=\"/_cdn/production/landing-cdn/{T2}/x.png\">"],
        "html_lang": ["pl"],
    },
    "61/s1": {"landing_cdn_account": ["666"], "css_class": ["x666y", "price-666"]},
    "61/ad": {"resource_basename": ["ad.png"]},
    "p14": {"html_lang": ["en"], "saved_from": ["https://x.test/?access_token=EAAsecretsecretsecret&a=1"]},
}
CARDS_SHARED = {
    "cases": [
        {"case_id": "1", "notable_ids": [f"aio_owner:{OWNER}", "aio_campaign_human_id:214711", f"aio_cdn_tenant:{T1}"]},
        {"case_id": "61", "notable_ids": ["aio_campaign:99999999-0000-4000-8000-000000000000", "aio_cdn_tenant:666"]},
    ]
}
CARDS_BACKUP = [
    {"case_id": "p1", "capture_date": "2026-10-08", "notable_ids": {
        "aio_owner_uuid": OWNER, "aio_tenant_path": T1, "aio_handle_key": HANDLE,
        "aio_campaign": "OPER / PL / Camp1 (0b9611d9-df5e-44e6-acea-ef85847dc3f1, human id 214711)"}},
    {"case_id": "p2", "notable_ids": {"duplicate_of": "p1"}},
    {"case_id": "p6", "notable_ids": {"aio_owner_uuid": "eeeeeeee-5555-4555-8555-555555555555",
                                      "aio_tenant_path": T2, "aio_campaign": "X (f65cbf2d-..., human id 213284)"}},
]


def write_prototype(tmp: Path) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "signals.json").write_text(json.dumps(SIGNALS))
    (tmp / "cases-shared.json").write_text(json.dumps(CARDS_SHARED))
    (tmp / "cases-backup.json").write_text(json.dumps(CARDS_BACKUP))
    return tmp
