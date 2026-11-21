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
        "https://www.news-a.example/a1\tnews-a.example\tarticle\treal article\n"
        "https://www.news-a.example/a1\tnews-a.example\tarticle\n"
        "https://www.news-b.example/\tnews-b.example\thome\n"
    )
    _capture(root / "picked-fetch", "cap-gz", "https://www.news-a.example/a1", "live", "live", "2026-11-21T22:00:00Z")
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
    assert "certpl:c-7" not in inc

    # Ids follow the cross-project scheme.
    assert set(inc) == {
        "adlib:meta-1", "adlib:meta-2", "adlib:g-3", "fetch:cap-live", "certpl:c-5", "certpl:c-6", "fetch:cap-gz",
    }

    # Two ads share one landing URL: the later (live) capture wins; the larger capture id is the duplicate.
    m1, m2 = inc["adlib:meta-1"], inc["adlib:meta-2"]
    assert [c.id for c in m1.captures] == ["adlib:meta-1:ad", "adlib:meta-1:lander"]
    assert [c.stage for c in m1.captures] == [m.Stage.ad, m.Stage.lander]
    assert m1.captures[1].duplicate_of is None
    assert m2.captures[1].duplicate_of == "adlib:meta-1:lander"
    assert {a.path for a in m1.captures[1].artefacts} == {"fetch-2/cap-lp/url.txt", "fetch-2/cap-lp/page.html"}
    assert m1.captures[0].artefacts[1].path == "scanner/creatives/a.jpg"
    assert rows["adlib:meta-1"]["has_landing_page"] and rows["adlib:meta-1"]["creatives"] == 1
    assert rows["adlib:meta-1"]["fetch_capture_id"] == "cap-lp"
    assert rows["adlib:meta-2"]["lander_duplicate_of"] == "adlib:meta-1:lander"
    # Duplicate lander bytes are counted once.
    assert rows["adlib:meta-2"]["bytes"] == 0 and rows["adlib:meta-1"]["bytes"] == 10 + 125
    # Every artefact referenced by an indicator exists in its incident.
    for i in ds.incidents:
        arts = {a.id for c in i.captures for a in c.artefacts}
        assert all(x.provenance.artefact_id in arts for x in i.indicators if x.provenance.artefact_id)

    def tags(i, cap):
        return {x.kind: x.value for x in i.indicators if x.capture_id == cap.id and x.kind.startswith("corpus_")}

    assert tags(m1, m1.captures[0]) == {"corpus_kind": "ad_record", "corpus_source": "meta:Brzoska"}
    assert tags(m1, m1.captures[1])["corpus_kind"] == "fetched_landing"
    g = inc["adlib:g-3"]
    assert rows["adlib:g-3"]["fetch_category"] == "cloaked" and not rows["adlib:g-3"]["has_landing_page"]
    assert tags(g, g.captures[1])["corpus_kind"] == "bare_domain"

    live = inc["fetch:cap-live"]
    assert live.source == "scanner_certpl" and live.source_ref == "c-4"
    assert [c.id for c in live.captures] == ["fetch:cap-live:c1"]
    assert rows["fetch:cap-live"]["corpus_kind"] == "fetched_landing"
    assert rows["certpl:c-5"]["corpus_kind"] == "bare_domain" and rows["certpl:c-5"]["fetch_outcome"] == "dns_failure"
    assert rows["certpl:c-6"]["corpus_kind"] == "bare_domain" and rows["certpl:c-6"]["fetch_outcome"] is None
    assert [c.id for c in inc["certpl:c-6"].captures] == ["certpl:c-6:c1"]

    mnl = [i for i in ds.incidents if i.source == "hand_picked_must_not_link"]
    assert len(mnl) == 1 and mnl[0].source_ref == "https://www.news-a.example/a1" and mnl[0].id == "fetch:cap-gz"
    assert rows["fetch:cap-gz"]["capture_ids"] == ["fetch:cap-gz:c1"]
    assert tags(mnl[0], mnl[0].captures[0]) == {
        "corpus_kind": "must_not_link",
        "corpus_source": "picked:news-a.example:article",
        "corpus_brand": "news-a.example",
        "corpus_page_kind": "article",
    }

    # Corpus tags and the fetch verdict never link.
    for i in ds.incidents:
        for x in i.indicators:
            if x.kind.startswith("corpus_") or x.kind == "fetch_outcome":
                assert not m.linkable(x)
    assert summary["bytes_referenced"] == sum(r["bytes"] for r in rows.values())


def test_sanitise():
    from ftf.corpus.build import sanitise

    assert sanitise("Glapiński łódź") == "Glapinski-lodz"
    assert sanitise("CR123") == "CR123"
    assert sanitise("a/b::c") == "a-b-c"


def test_read_picked_rejects_bad_line(tmp_path):
    p = tmp_path / "p.tsv"
    p.write_text("news-a.example\tarticle\n")
    with pytest.raises(ValueError):
        read_picked(p)


def test_cli_targets_and_build(corpus, tmp_path, capsys):
    root, picked = corpus
    out = tmp_path / "t"
    assert main(["corpus", "targets", "--queue", str(root / "scanner/queue.jsonl"), "--run", RUN,
                 "--picked", str(picked), "--out", str(out), "--parts", "2"]) == 0
    lines = (out / "urls-1.txt").read_text().split() + (out / "urls-2.txt").read_text().split()
    assert len(lines) == 7 and "https://www.news-b.example/" in lines
    assert len((out / "targets.jsonl").read_text().splitlines()) == 7
    assert main(["corpus", "build", "--root", str(root), "--scanner", "scanner", "--run", RUN,
                 "--fetch", "fetch-1", "--fetch", "fetch-2"]) == 0
    assert '"incidents": 6' in capsys.readouterr().out


def test_article_links_same_site_only():
    from ftf.corpus.articles import article_links

    html = """
    <a href="/wiadomosci/7,114884,31234567,tytul-artykulu.html#x">a</a>
    <a href="https://next.news-a.example/next/7,151003,31234568,inny.html?utm=1">b</a>
    <a href="https://evil.example/7,1,2,x.html">c</a>
    <a href="http://wiadomosci.news-a.example/wiadomosci/7,1,3,plain-http.html">d</a>
    <a href="/wiadomosci/0,0.html">e</a>
    <a href="/wiadomosci/7,114884,31234567,tytul-artykulu.html">dup</a>
    """
    assert article_links(html, "https://wiadomosci.news-a.example/", 10) == [
        "https://wiadomosci.news-a.example/wiadomosci/7,114884,31234567,tytul-artykulu.html",
        "https://next.news-a.example/next/7,151003,31234568,inny.html",
    ]
    tvn = '<a href="/polska/jakis-tytul-ra1234567">x</a><a href="/polska">y</a>'
    assert article_links(tvn, "https://news-b.example/", 5) == ["https://news-b.example/polska/jakis-tytul-ra1234567"]


def test_cli_articles(tmp_path, capsys):
    root = tmp_path / "b"
    _capture(root / "pf", "cap-home", "https://news-b.example/", "live", "live", "2026-11-21T22:00:00Z")
    (root / "pf" / "cap-home" / "page.html").write_text(
        '<a href="/polska/a-ra100001">1</a><a href="/swiat/b-ra100002">2</a><a href="/c-ra100003">3</a>'
    )
    _capture(root / "pf", "cap-dead", "https://www.news-a.example/", "dead", "timeout", "2026-11-21T22:00:01Z")
    picked = tmp_path / "home.tsv"
    picked.write_text("https://news-b.example/\tnews-b.example\thome\nhttps://www.news-a.example/\tnews-a.example\thome\n")
    out = tmp_path / "articles.tsv"
    assert main(["corpus", "articles", "--picked", str(picked), "--fetch", str(root / "pf"),
                 "--per-page", "2", "--out", str(out)]) == 0
    rows = [r.split("\t") for r in out.read_text().splitlines()]
    assert [r[0] for r in rows] == ["https://news-b.example/polska/a-ra100001", "https://news-b.example/swiat/b-ra100002"]
    assert all(r[1:3] == ["news-b.example", "article"] for r in rows)
    assert len(read_picked(out)) == 2


def test_wayback_picked(tmp_path):
    from ftf.corpus.build import wayback_original

    orig = "https://news-b.example/polska/x-st1"
    snap = f"https://web.archive.org/web/20261118061622if_/{orig}"
    assert wayback_original(snap) == ("20261118061622", orig)
    assert wayback_original(orig) is None
    root = tmp_path / "b"
    _capture(root / "wb", "cap-wb", snap, "live", "live", "2026-11-21T23:00:00Z")
    lst = tmp_path / "wb.tsv"
    lst.write_text(f"{snap}\tnews-b.example\tarticle\n")
    build_corpus(root, None, None, [], [lst], ["wb"])
    ds = m.Dataset.from_json((root / "dataset.json").read_bytes())
    (inc,) = ds.incidents
    assert inc.source == "wayback" and inc.source_ref == orig and inc.id == "fetch:cap-wb"
    tags = {x.kind: x.value for x in inc.indicators if x.kind.startswith("corpus_")}
    assert tags["corpus_kind"] == "must_not_link" and tags["corpus_wayback_snapshot"] == "20261118061622"
    row = json.loads((root / "manifest.jsonl").read_text())
    assert row["source"] == "wayback" and row["source_ref"] == orig


def test_review_fixes(tmp_path):
    from ftf.corpus.articles import article_links, site_of
    from ftf.corpus.build import read_fetch_dirs, wayback_original

    assert site_of("www.bank.com.pl") == "bank.com.pl"
    assert article_links('<a href="https://evil.com.pl/a-b-c-1">x</a>', "https://www.bank.com.pl/", 5) == []
    assert article_links('<a href="/a-ra1">x</a>', "https://news-b.example/", 0) == []
    assert wayback_original("https://web.archive.org/web/2026if_/news-b.example/x-st1") == ("2026", "https://news-b.example/x-st1")
    q = read_queue_from([_row("k", "meta", "i", url="  https://x.example/path ", domain="x.example")])
    assert target_url(q[0]) == "https://x.example/path"

    # A loaded capture beats a later failed one, across subdirs and in any argument order.
    root = tmp_path / "r"
    _capture(root / "a", "ok", "https://u.example/", "live", "live", "2026-11-21T20:00:00Z")
    _capture(root / "b", "late-fail", "https://u.example/", "cloaked", "bot_wall", "2026-11-21T22:00:00Z")
    _capture(root / "b", "old", "https://v.example/", "live", "live", "2026-11-21T20:00:00Z")
    _capture(root / "a", "new", "https://v.example/", "live", "live", "2026-11-21T21:00:00Z")
    for order in (["a", "b"], ["b", "a"]):
        got = read_fetch_dirs(root, order)
        assert got["https://u.example/"].manifest["capture_id"] == "ok"
        assert got["https://v.example/"].manifest["capture_id"] == "new"


def test_victim_and_blocked(corpus):
    root, picked = corpus
    # Victim-profile re-fetch: the cloaked Google target loads, the shared Meta lander stays cloaked.
    _capture(root / "fetch-victim", "v-g", "https://invest.example/", "live", "live", "2026-11-21T23:00:00Z")
    _capture(root / "fetch-victim", "v-lp", "https://shared.example/lp", "cloaked", "decoy", "2026-11-21T23:00:01Z")
    _capture(root / "fetch-victim", "v-dead", "https://dead.example/", "live", "live", "2026-11-21T23:00:02Z")
    _capture(root / "picked-fetch", "cap-403", "https://www.news-b.example/", "dead", "http_error", "2026-11-21T22:00:01Z")
    summary = build_corpus(root, "scanner", [RUN], ["fetch-1", "fetch-2"], [picked], ["picked-fetch"], "t",
                           victim_dirs=["fetch-victim"])
    ds = m.Dataset.from_json((root / "dataset.json").read_bytes())
    inc = {i.id: i for i in ds.incidents}
    rows = {r["incident_id"]: r for r in map(json.loads, (root / "manifest.jsonl").read_text().splitlines())}

    g = inc["adlib:g-3"]
    assert [c.id for c in g.captures] == ["adlib:g-3:ad", "adlib:g-3:lander", "adlib:g-3:lander-victim"]
    vtags = {x.kind: x.value for x in g.indicators if x.capture_id == "adlib:g-3:lander-victim" and x.kind.startswith("corpus_")}
    assert vtags == {"corpus_kind": "fetched_landing", "corpus_source": "google_atc:trading", "corpus_profile": "victim-pl-mobile"}
    assert rows["adlib:g-3"]["victim_flipped_to_live"] and rows["adlib:g-3"]["victim_outcome"] == "live"

    # Shared victim lander: deduplicated separately from the default lander.
    assert inc["adlib:meta-2"].captures[2].duplicate_of == "adlib:meta-1:lander-victim"
    assert inc["adlib:meta-2"].captures[1].duplicate_of == "adlib:meta-1:lander"
    assert not rows["adlib:meta-1"]["victim_flipped_to_live"]

    # A dead CERT domain that loads under the victim profile becomes a fetched landing page.
    assert rows["certpl:c-5"]["corpus_kind"] == "fetched_landing"
    assert [c.id for c in inc["certpl:c-5"].captures] == ["certpl:c-5:c1", "certpl:c-5:victim"]

    assert summary["victim_profile"]["refetched"] == 4 and summary["victim_profile"]["flipped_to_live"] == 2
    # The 403 home page is kept, but not as a must-not-link distractor.
    assert rows["fetch:cap-403"]["corpus_kind"] == "must_not_link_blocked"
    assert summary["by_corpus_kind"]["must_not_link"] == 1


def test_tag_ingested(corpus, tmp_path):
    from ftf.corpus.ingested import tag_ingested

    root, picked = corpus
    _capture(root / "fetch-victim", "v-g", "https://invest.example/", "live", "live", "2026-11-21T23:00:00Z")
    build_corpus(root, "scanner", [RUN], ["fetch-1", "fetch-2"], [picked], ["picked-fetch"], "t",
                 victim_dirs=["fetch-victim"])
    # Shaped like `ftf ingest` output: the default fetch and its victim re-fetch grouped in one incident.
    ingested = {"incidents": [
        {"id": "fetch:cap-g", "captures": [{"id": "fetch:cap-g:c1"}, {"id": "fetch:cap-g:c2"}]},
        {"id": "fetch:v-g", "captures": [{"id": "fetch:v-g:c1"}]},
        {"id": "fetch:cap-lp", "captures": [{"id": "fetch:cap-lp:c1"}]},
        {"id": "fetch:cap-dead", "captures": [{"id": "fetch:cap-dead:c1"}]},
        {"id": "fetch:cap-gz", "captures": [{"id": "fetch:cap-gz:c1"}]},
        {"id": "fetch:unknown", "captures": [{"id": "fetch:unknown:c1"}]},
    ]}
    (tmp_path / "ing.json").write_text(json.dumps(ingested))
    out = tmp_path / "kinds.jsonl"
    s = tag_ingested(tmp_path / "ing.json", root / "manifest.jsonl", out)
    rows = {r["incident_id"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert s["untagged"] == 1 and s["tagged"] == 5
    assert rows["fetch:cap-g"]["corpus_kind"] == "bare_domain"  # cloaked default fetch only
    assert rows["fetch:v-g"]["corpus_kind"] == "fetched_landing" and rows["fetch:v-g"]["profiles"] == ["victim-pl-mobile"]
    assert {i["corpus_incident_id"] for i in rows["fetch:cap-lp"]["corpus_items"]} == {"adlib:meta-1", "adlib:meta-2"}
    assert rows["fetch:cap-dead"]["corpus_kind"] == "bare_domain"
    assert rows["fetch:cap-gz"]["corpus_kind"] == "must_not_link"


def test_convert_scanner_v1(tmp_path):
    from ftf.corpus.convert_scanner import convert

    sc = tmp_path / "old"
    (sc / "creatives").mkdir(parents=True)
    (sc / "creatives" / "abc.jpg").write_bytes(b"\xff\xd8 not really a jpeg")

    def ind(iid, kind, layer, level, value, loc):
        return {"id": f"{iid}/{kind}", "capture_id": f"{iid}/ad", "layer": layer, "kind": kind, "level": level,
                "value": value, "observed": "static", "provenance": {"artefact_id": f"{iid}/ad/library", "locator": loc},
                "extractor": "old"}

    meta = {"id": "meta-111", "source": "scanner_meta", "source_ref": "111",
            "captures": [{"id": "meta-111/ad", "stage": "ad", "captured_on": "2026-11-21", "artefacts": [
                {"id": "meta-111/ad/library", "type": "ad_library"},
                {"id": "meta-111/ad/creative/0", "type": "ad_screenshot", "path": "creatives/abc.jpg", "size": 20},
                {"id": "meta-111/ad/creative/1", "type": "asset", "path": "creatives/missing.jpg"}]}],
            "indicators": [ind("meta-111", "meta_ad_id", "creative", "capture", "111", "ad_id"),
                           ind("meta-111", "advertiser_page_id", "distribution_account", "operator", "999", "page_id"),
                           ind("meta-111", "ad_text", "creative", "campaign", "Invented text", "body"),
                           ind("meta-111", "landing_url", "landing", "landing", "https://lp.example/x", "link_url"),
                           ind("meta-111", "landing_domain", "landing", "landing", "lp.example", "link_url"),
                           ind("meta-111", "ad_start_date", "creative", "capture", "2026-11-14", "start_date")]}
    cert = {"id": "certpl-7", "source": "scanner_certpl", "source_ref": "7",
            "captures": [{"id": "certpl-7/ad", "stage": "unknown", "artefacts": [{"id": "certpl-7/ad/library", "type": "url"}]}],
            "indicators": [ind("certpl-7", "cert_register_id", "creative", "capture", "7", "ad_id"),
                           ind("certpl-7", "landing_domain", "landing", "landing", "Shop.Bad-Domain.example", "DomainAddress")]}
    _write_dataset(sc / f"runs/{RUN}/incidents-meta.json", [meta])
    _write_dataset(sc / f"runs/{RUN}/incidents-certpl.json", [cert])
    (sc / "queue.jsonl").write_text(json.dumps(_row("meta:111", "meta", "meta-111", keyword="kw")) + "\n")

    out = tmp_path / "v1"
    s = convert(sc, out)
    assert s["captures"] == 2 and s["creatives"] == 1 and s["by_source"] == {"meta": 1, "certpl": 1}
    folders = {p.parent.name: p.parent for p in out.glob("*/capture.json")}
    mfolder = folders[f"{RUN}-meta-111"]
    man = json.loads((mfolder / "capture.json").read_text())
    ad = json.loads((mfolder / "ad.json").read_text())
    inc = m.Incident.model_validate(man["incident"])
    assert inc.id == "adlib:111" and inc.captures[0].id == "adlib:111:ad"
    assert ad["page_id"] == "999" and ad["body"] == "Invented text" and ad["keyword"] == "kw"
    assert ad["landing_url"] == "https://lp.example/x" and ad["title"] is None
    (cre,) = [a for a in inc.captures[0].artefacts if a.type is m.ArtefactType.asset]
    assert cre.path == f"{RUN}-meta-111/creative-0.jpg" and (out / cre.path).read_bytes().startswith(b"\xff\xd8")
    assert len(cre.sha256) == 64
    (cfolder,) = [p for n, p in folders.items() if n.startswith(f"{RUN}-certpl-")]
    cinc = m.Incident.model_validate(json.loads((cfolder / "capture.json").read_text())["incident"])
    cad = json.loads((cfolder / "ad.json").read_text())
    assert cinc.id.startswith("certpl:") and cinc.captures[0].id == f"{cinc.id}:listing"
    assert cad["register_id"] == "7" and cad["landing_domain"] == "shop.bad-domain.example"


def test_v1_queue_and_shared_rows(tmp_path):
    root = tmp_path / "b"
    sc = root / "scanner"
    inc = _scanner_incident("adlib:222", "google_bq", None)
    inc["captures"][0]["id"] = "adlib:222:ad"
    inc["captures"][0]["artefacts"] = [{"id": "adlib:222:ad:library", "type": "ad_library"},
                                       {"id": "adlib:222:ad:creative-0", "type": "asset", "path": "f1/creative-0.jpg", "size": 5}]
    inc["indicators"][0].update(capture_id="adlib:222:ad", provenance={"artefact_id": "adlib:222:ad:library"})
    (sc / "f1").mkdir(parents=True)
    (sc / "f1" / "capture.json").write_text(json.dumps({"incident": inc}))
    rows = [
        {**_row("google_bq:222", "google_bq", "adlib:222", domain="ad.example"), "incidents_file": None, "capture_dir": "f1"},
        _row("certpl:1", "certpl", "c-1", domain="same.example"),
        _row("certpl:2", "certpl", "c-2", domain="same.example"),
    ]
    (sc / "queue.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    _write_dataset(sc / f"runs/{RUN}/incidents-certpl.json", [_scanner_incident(f"c-{i}", "certpl") for i in (1, 2)])
    _capture(root / "fetch-1", "cap-same", "https://same.example/", "live", "live", "2026-11-21T21:00:00Z")
    summary = build_corpus(root, "scanner", None, ["fetch-1"], None, [], "t")
    man = [json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines()]
    ad = [r for r in man if r["source"] == "google_bq"][0]
    assert ad["incident_id"] == "adlib:222" and ad["corpus_kind"] == "ad_record" and ad["creatives"] == 1
    same = [r for r in man if r["source"] == "certpl"]
    assert [r["source_ref"] for r in same] == ["c-1", "c-2"] and same[1]["shares_incident"] and same[1]["bytes"] == 0
    assert summary["incidents"] == 3


def test_article_links_keep_non_tracking_query():
    from ftf.corpus.articles import article_links

    html = '<a href="/news/a-b-c-1234?id=7&utm_source=x">a</a>'
    assert article_links(html, "https://site.example/", 5) == ["https://site.example/news/a-b-c-1234?id=7"]


def test_tag_ingested_scanner_items_and_check(corpus, tmp_path):
    from ftf.corpus.ingested import check_indicators, tag_ingested

    root, picked = corpus
    build_corpus(root, "scanner", [RUN], ["fetch-1", "fetch-2"], None, [], "t")
    ing = {"incidents": [
        {"id": "adlib:meta-1", "captures": [{"id": "adlib:meta-1:ad"}], "indicators": [{"layer": "creative"}]},
        {"id": "certpl:dead.example", "captures": [{"id": "certpl:dead.example:listing"}], "indicators": []},
        {"id": "fetch:cap-live", "captures": [{"id": "fetch:cap-live:c1", "observed": "live"}],
         "indicators": [{"layer": "kit"}, {"layer": "kit"}]},
    ]}
    (tmp_path / "ing.json").write_text(json.dumps(ing))
    out = tmp_path / "k.jsonl"
    s = tag_ingested(tmp_path / "ing.json", root / "manifest.jsonl", out)
    rows = {r["incident_id"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert rows["adlib:meta-1"]["corpus_kind"] == "ad_record"
    assert rows["certpl:dead.example"]["corpus_kind"] == "bare_domain"
    assert rows["fetch:cap-live"]["corpus_kind"] == "fetched_landing"
    assert s["untagged"] == 0 and s["indicators_by_layer"] == {"creative": 1, "kit": 2}
    check_indicators(s, ing)
    empty = {"incidents": [{"id": "fetch:x", "captures": [{"id": "fetch:x:c1", "observed": "live"}], "indicators": []}]}
    with pytest.raises(SystemExit):
        check_indicators({"indicators_by_layer": {}}, empty)


def test_tag_ingested_merged_refetch(corpus, tmp_path):
    from ftf.corpus.ingested import tag_ingested

    root, picked = corpus
    build_corpus(root, "scanner", [RUN], ["fetch-1", "fetch-2"], None, [], "t")
    # The reader merged the failed first fetch of a URL with the later one: the incident id names
    # the first folder only; the second folder appears in the artefact paths.
    ing = {"incidents": [{"id": "fetch:cap-lp-old", "captures": [
        {"id": "fetch:cap-lp-old:c1", "artefacts": [{"path": "fetch-1/cap-lp-old/url.txt"}]},
        {"id": "fetch:cap-lp-old:c2", "artefacts": [{"path": "fetch-2/cap-lp/page.html"}]}], "indicators": []}]}
    (tmp_path / "ing.json").write_text(json.dumps(ing))
    out = tmp_path / "k.jsonl"
    s = tag_ingested(tmp_path / "ing.json", root / "manifest.jsonl", out)
    (row,) = map(json.loads, out.read_text().splitlines())
    assert s["untagged"] == 0 and row["corpus_kind"] == "fetched_landing" and "cap-lp" in row["fetch_folders"]
