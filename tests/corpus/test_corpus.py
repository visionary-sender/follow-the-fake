import json
from pathlib import Path

import pytest

from ftf.cli import main
from ftf.contract import models as m
from ftf.corpus.build import build_corpus
from ftf.corpus.targets import build_targets, normalise_host, read_picked, read_queue, split, target_url

RUN = "20261121T000000Z"


def _row(key, source, incident_id, url=None, domain=None, keyword=None, run=RUN):
    return {
        "key": key,
        "source": source,
        "incident_id": incident_id,
        "incidents_file": f"runs/{run}/incidents-{source}.json",
        "run_id": run,
        "keyword": keyword,
        "landing_url": url,
        "landing_domain": domain,
        "status": "pending",
    }


def _scanner_incident(iid, source, creative=None):
    arts = [{"id": f"{iid}/ad/library", "type": "ad_library"}]
    if creative:
        arts.append({"id": f"{iid}/ad/creative/0", "type": "ad_screenshot", "path": creative, "size": 10})
    return {
        "id": iid,
        "source": f"scanner_{source}",
        "source_ref": iid,
        "captures": [{"id": f"{iid}/ad", "stage": "ad", "artefacts": arts}],
        "indicators": [
            {
                "id": f"{iid}/ad/advertiser_id/0",
                "capture_id": f"{iid}/ad",
                "layer": "distribution_account",
                "kind": "advertiser_id",
                "level": "operator",
                "value": "A1",
                "observed": "static",
                "provenance": {"artefact_id": f"{iid}/ad/library"},
                "extractor": "scanner",
            }
        ],
    }


def _write_dataset(path: Path, incidents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"contract_version": m.CONTRACT_VERSION, "name": "x", "incidents": incidents}))


def _capture(out: Path, cid: str, url: str, category: str, outcome: str, started: str):
    d = out / cid
    d.mkdir(parents=True)
    (d / "url.txt").write_text(url)
    inc = {
        "id": f"fetch:{cid}",
        "source": "live_fetch",
        "source_ref": cid,
        "captures": [
            {
                "id": f"fetch:{cid}:c1",
                "stage": "unknown",
                "observed": "live",
                "captured_on": "2026-11-21",
                "artefacts": [
                    {"id": f"fetch:{cid}:url", "type": "url", "path": f"{cid}/url.txt", "size": len(url)},
                    {"id": f"fetch:{cid}:html", "type": "html", "path": f"{cid}/page.html", "size": 100},
                ],
            }
        ],
        "indicators": [
            {
                "id": f"fetch:{cid}:fetch_outcome",
                "capture_id": f"fetch:{cid}:c1",
                "layer": "collector",
                "kind": "fetch_outcome",
                "level": "capture",
                "value": f"{outcome}: x",
                "observed": "live",
                "provenance": {"artefact_id": f"fetch:{cid}:url"},
                "extractor": "ftf-fetch@0.1.0",
            }
        ],
    }
    (d / "capture.json").write_text(
        json.dumps(
            {
                "capture_id": cid,
                "requested_url": url,
                "final_url": url,
                "outcome": outcome,
                "category": category,
                "started_at": started,
                "incident": inc,
            }
        )
    )


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "batch"
    sc = root / "scanner"
    rows = [
        _row("meta:1", "meta", "meta-1", url="https://shared.example/lp", keyword="Brzoska"),
        _row("meta:2", "meta", "meta-2", url="https://shared.example/lp", keyword="Brzoska"),
        _row("google_atc:3", "google_atc", "g-3", domain="invest.example", keyword="trading"),
        _row("certpl:4", "certpl", "c-4", domain="Live.Example."),
        _row("certpl:5", "certpl", "c-5", domain="dead.example"),
        _row("certpl:6", "certpl", "c-6", domain="never.example"),
        _row("certpl:7", "certpl", "c-7", domain="other-run.example", run="20261122T000000Z"),
    ]
    sc.mkdir(parents=True)
    (sc / "queue.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows + rows[:1]))
    _write_dataset(
        sc / f"runs/{RUN}/incidents-meta.json",
        [_scanner_incident("meta-1", "meta", "creatives/a.jpg"), _scanner_incident("meta-2", "meta")],
    )
    _write_dataset(sc / f"runs/{RUN}/incidents-google_atc.json", [_scanner_incident("g-3", "google_atc")])
    _write_dataset(
        sc / f"runs/{RUN}/incidents-certpl.json",
        [_scanner_incident(f"c-{i}", "certpl") for i in (4, 5, 6)],
    )
    _capture(root / "fetch-1", "cap-lp-old", "https://shared.example/lp", "dead", "timeout", "2026-11-21T20:00:00Z")
    _capture(root / "fetch-2", "cap-lp", "https://shared.example/lp", "live", "live", "2026-11-21T21:00:00Z")
    _capture(root / "fetch-1", "cap-live", "https://live.example/", "live", "live", "2026-11-21T21:00:01Z")
    _capture(root / "fetch-1", "cap-dead", "https://dead.example/", "dead", "dns_failure", "2026-11-21T21:00:02Z")
    _capture(root / "fetch-2", "cap-g", "https://invest.example/", "cloaked", "bot_wall", "2026-11-21T21:00:03Z")
    (root / "fetch-2" / "junk").mkdir()
    (root / "fetch-2" / "junk" / "capture.json").write_text("not json")
    picked = tmp_path / "picked.tsv"
    picked.write_text(
        "# url\tbrand\tkind\n"
        "https://www.gazeta.pl/a1\tgazeta.pl\tarticle\treal article\n"
        "https://www.gazeta.pl/a1\tgazeta.pl\tarticle\n"
        "https://www.tvn24.pl/\ttvn24.pl\thome\n"
    )
    _capture(root / "picked-fetch", "cap-gz", "https://www.gazeta.pl/a1", "live", "live", "2026-11-21T22:00:00Z")
    return root, picked


def test_targets_dedupe_and_interleave(corpus):
    root, _ = corpus
    rows = read_queue(root / "scanner" / "queue.jsonl", [RUN])
    assert [r.key for r in rows].count("meta:1") == 1
    assert all(r.run_id == RUN for r in rows)
    targets = build_targets(rows)
    urls = [t.url for t in targets]
    assert urls[:3] == ["https://shared.example/lp", "https://invest.example/", "https://live.example/"]
    assert targets[0].keys == ["meta:1", "meta:2"]
    assert len(urls) == len(set(urls)) == 5
    parts = split(targets, 2)
    assert sorted(sum(([t.url for t in p] for p in parts), [])) == sorted(urls)


def test_normalise_host():
    assert normalise_host(" Live.Example. ") == "live.example"
    assert normalise_host("żółw.pl").startswith("xn--")
    assert normalise_host("not a host") is None
    assert normalise_host("localhost") is None


def test_target_url_ignores_non_http():
    rows = read_queue_from([_row("k", "meta", "i", url="javascript:alert(1)", domain="x.example")])
    assert target_url(rows[0]) == "https://x.example/"


def read_queue_from(rows):
    from ftf.corpus.targets import QueueRow

    return [QueueRow.from_json(r) for r in rows]


def test_build(corpus):
    root, picked = corpus
    summary = build_corpus(root, "scanner", [RUN], ["fetch-1", "fetch-2"], picked, ["picked-fetch"], "t")
    ds = m.Dataset.from_json((root / "dataset.json").read_bytes())
    inc = {i.id: i for i in ds.incidents}
    rows = {r["incident_id"]: r for r in map(json.loads, (root / "manifest.jsonl").read_text().splitlines())}

    assert summary["by_corpus_kind"] == {"ad_record": 3, "bare_domain": 2, "fetched_landing": 1, "must_not_link": 1}
    assert "c-7" not in inc

    # Two ads share one landing URL: the later (live) capture wins, the second ad's copy is a duplicate.
    m1, m2 = inc["meta-1"], inc["meta-2"]
    assert [c.stage for c in m1.captures] == [m.Stage.ad, m.Stage.lander]
    assert m1.captures[1].duplicate_of is None
    assert m2.captures[1].duplicate_of == m1.captures[1].id
    assert {a.path for a in m1.captures[1].artefacts} == {"fetch-2/cap-lp/url.txt", "fetch-2/cap-lp/page.html"}
    assert m1.captures[0].artefacts[1].path == "scanner/creatives/a.jpg"
    assert rows["meta-1"]["has_landing_page"] and rows["meta-1"]["creatives"] == 1

    def tags(i, cap):
        return {x.kind: x.value for x in i.indicators if x.capture_id == cap.id and x.kind.startswith("corpus_")}

    assert tags(m1, m1.captures[0]) == {"corpus_kind": "ad_record", "corpus_source": "meta:Brzoska"}
    assert tags(m1, m1.captures[1])["corpus_kind"] == "fetched_landing"
    g = inc["g-3"]
    assert rows["g-3"]["fetch_category"] == "cloaked" and not rows["g-3"]["has_landing_page"]
    assert tags(g, g.captures[1])["corpus_kind"] == "bare_domain"

    assert rows["c-4"]["corpus_kind"] == "fetched_landing"
    assert rows["c-5"]["corpus_kind"] == "bare_domain" and rows["c-5"]["fetch_outcome"] == "dns_failure"
    assert rows["c-6"]["corpus_kind"] == "bare_domain" and rows["c-6"]["fetch_outcome"] is None
    assert len(inc["c-6"].captures) == 1

    mnl = [i for i in ds.incidents if i.source == "hand_picked_must_not_link"]
    assert len(mnl) == 1 and mnl[0].source_ref == "https://www.gazeta.pl/a1"
    assert tags(mnl[0], mnl[0].captures[0]) == {
        "corpus_kind": "must_not_link",
        "corpus_source": "picked:gazeta.pl:article",
        "corpus_brand": "gazeta.pl",
        "corpus_page_kind": "article",
    }

    # Corpus tags and the fetch verdict never link.
    for i in ds.incidents:
        for x in i.indicators:
            if x.kind.startswith("corpus_") or x.kind == "fetch_outcome":
                assert not m.linkable(x)
    assert summary["bytes_referenced"] == sum(r["bytes"] for r in rows.values())


def test_read_picked_rejects_bad_line(tmp_path):
    p = tmp_path / "p.tsv"
    p.write_text("gazeta.pl\tarticle\n")
    with pytest.raises(ValueError):
        read_picked(p)


def test_cli_targets_and_build(corpus, tmp_path, capsys):
    root, picked = corpus
    out = tmp_path / "t"
    assert main(["corpus", "targets", "--queue", str(root / "scanner/queue.jsonl"), "--run", RUN,
                 "--picked", str(picked), "--out", str(out), "--parts", "2"]) == 0
    lines = (out / "urls-1.txt").read_text().split() + (out / "urls-2.txt").read_text().split()
    assert len(lines) == 7 and "https://www.tvn24.pl/" in lines
    assert len((out / "targets.jsonl").read_text().splitlines()) == 7
    assert main(["corpus", "build", "--root", str(root), "--scanner", "scanner", "--run", RUN,
                 "--fetch", "fetch-1", "--fetch", "fetch-2"]) == 0
    assert '"incidents": 6' in capsys.readouterr().out
