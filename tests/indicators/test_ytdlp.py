"""yt-dlp info records of video ads: uploader and media ids only (synthetic records, example.test)."""

from __future__ import annotations

import json
from pathlib import Path

from ftf.contract import Artefact, ArtefactType, Capture, Incident, Stage
from ftf.indicators import ytdlp


def write(root: Path, rel: str, data: str | bytes) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return rel


def record(**fields) -> str:
    base = {
        "id": "1234567890123",
        "title": "Synthetic headline about a celebrity",
        "description": "Synthetic body text with https://shop.example.test/offer?access_token=EAAsecret",
        "extractor": "facebook",
        "webpage_url": "https://www.example.test/ads/video/1234567890123",
        "webpage_url_domain": "example.test",
        "playlist_uploader_id": "900100200",
        "uploader_id": "uploader-7",
        "channel_id": "channel-9",
        "thumbnail": "https://cdn.example.test/t.jpg",
        "formats": [{"url": "https://cdn.example.test/v.mp4"}],
    }
    base.update(fields)
    return json.dumps(base)


def capture(*arts: Artefact) -> Capture:
    return Capture(id="v:c1", stage=Stage.lander, artefacts=list(arts))


def run(cap: Capture, root: Path) -> list:
    return list(ytdlp.extract(Incident(id="v", source="synthetic", captures=[cap]), cap, root))


def info_artefact(root: Path, data: str, rel: str = "v/Clip [1234567890123].info.json") -> Artefact:
    return Artefact(id="v:info", type=ArtefactType.ad_library, path=write(root, rel, data))


def test_uploader_and_media_ids(tmp_path):
    cap = capture(info_artefact(tmp_path, record()))
    inds = {i.kind: i for i in run(cap, tmp_path)}
    assert set(inds) == {"video_uploader_id", "video_media_id"}
    assert inds["video_uploader_id"].value == "900100200"
    assert inds["video_uploader_id"].layer.value == "distribution_account"
    assert inds["video_media_id"].value == "1234567890123"
    assert inds["video_media_id"].layer.value == "creative"
    assert all(i.extractor == ytdlp.EXTRACTOR and i.capture_id == "v:c1" for i in inds.values())


def test_text_and_links_never_become_values(tmp_path):
    cap = capture(info_artefact(tmp_path, record()))
    values = " ".join(i.value for i in run(cap, tmp_path))
    for leaked in ("Synthetic", "headline", "http", "access_token", "example.test", "uploader-7"):
        assert leaked not in values


def test_falls_back_to_uploader_then_channel(tmp_path):
    no_page = json.loads(record())
    del no_page["playlist_uploader_id"]
    cap = capture(info_artefact(tmp_path, json.dumps(no_page)))
    assert {i.value for i in run(cap, tmp_path) if i.kind == "video_uploader_id"} == {"uploader-7"}
    no_uploader = {k: v for k, v in no_page.items() if k != "uploader_id"}
    cap = capture(info_artefact(tmp_path, json.dumps(no_uploader), rel="w/Clip.info.json"))
    assert {i.value for i in run(cap, tmp_path) if i.kind == "video_uploader_id"} == {"channel-9"}


def test_numeric_ids_are_kept_and_booleans_are_not(tmp_path):
    cap = capture(info_artefact(tmp_path, record(id=55512345, playlist_uploader_id=True, uploader_id=None)))
    inds = {i.kind: i.value for i in run(cap, tmp_path)}
    assert inds["video_media_id"] == "55512345"
    assert inds["video_uploader_id"] == "channel-9"  # a boolean is not an id; the next field is used


def test_only_yt_dlp_records_of_ad_library_artefacts(tmp_path):
    scanner_record = Artefact(id="v:ad", type=ArtefactType.ad_library,
                              path=write(tmp_path, "v/ad.json", record()))
    video = Artefact(id="v:mp4", type=ArtefactType.video, path=write(tmp_path, "v/clip.mp4", b"\x00\x00"))
    assert run(capture(scanner_record, video), tmp_path) == []


def test_bad_or_oversized_records_give_nothing(tmp_path):
    assert run(capture(info_artefact(tmp_path, "[1, 2]")), tmp_path) == []
    assert run(capture(info_artefact(tmp_path, "{not json")), tmp_path) == []
    big = write(tmp_path, "v/huge.info.json", "{" + " " * (ytdlp.MAX_BYTES + 1) + "}")
    assert run(capture(Artefact(id="v:big", type=ArtefactType.ad_library, path=big)), tmp_path) == []
    missing = Artefact(id="v:none", type=ArtefactType.ad_library, path="v/absent.info.json")
    assert run(capture(missing), tmp_path) == []


def test_handles_are_ids_and_a_bad_record_does_not_hide_a_good_one(tmp_path):
    bad = Artefact(id="v:bad", type=ArtefactType.ad_library, path=write(tmp_path, "v/a.info.json", "[1]"))
    good = info_artefact(tmp_path, record(playlist_uploader_id="@synthetic-channel"), rel="v/b.info.json")
    inds = {i.kind: i.value for i in run(capture(bad, good), tmp_path)}
    assert inds["video_uploader_id"] == "@synthetic-channel"
    assert inds["video_media_id"] == "1234567890123"
