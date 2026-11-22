"""URL lists: a text file of URLs, one incident per URL, read back by the URL-aware extractors.

Fixtures are invented: example.com/.net/.org hosts with gclid, utm and ad params. (`.test` has no
registrable domain in the Public Suffix List, so it cannot exercise landing_domain.)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ftf.contract import Artefact, ArtefactType, Observed, Stage
from ftf.indicators import _pages
from ftf.ingest import adapters, ids, load_inputs, run, urllist

from .conftest import write

HEADER = "Link list (example)"
URL_A = "https://alpha.example.com/offer?gclid=Cj0KCQ-test-alpha&utm_campaign=spring&utm_source=google"
URL_B = "https://beta.example.net/p/one?ad_id=120200000000000001&utm_medium=cpc"
URL_C = "https://gamma.example.org/?gclid=Cj0KCQ-test-gamma&utm_term=wallet"
TOKENS = ("Cj0KCQ-test-alpha", "Cj0KCQ-test-gamma")


def list_file(folder: Path, name: str = "scam_links_example.txt") -> Path:
    lines = [HEADER, "", URL_A, "", URL_B, "", URL_C, "", URL_A, "not a url", ""]
    return write(folder / name, "\n".join(lines))


# -- detection and ids -----------------------------------------------------------------------------


def test_detection_by_content_and_suffix(tmp_path):
    assert adapters.is_url_list(list_file(tmp_path))
    notes = write(tmp_path / "notes.txt", "Case notes\nnothing to see here\nhttps://alpha.example.com/\n")
    assert not adapters.is_url_list(notes)  # one URL line of three: not mostly URLs
    page = write(tmp_path / "page.html", "<a href='https://alpha.example.com/'>x</a>\nhttps://beta.example.net/\n")
    assert not adapters.is_url_list(page)  # URLs in a page are not a list


def test_one_incident_per_distinct_url_with_bare_url_ids(tmp_path):
    ds = load_inputs([list_file(tmp_path)])
    assert len(ds.incidents) == 3  # URL_A is listed twice
    assert {i.id for i in ds.incidents} == {ids.input_incident_id(u) for u in (URL_A, URL_B, URL_C)}
    for inc in ds.incidents:
        assert inc.source == "url_list" and inc.source_ref == "scam_links_example.txt"
        (cap,) = inc.captures
        assert cap.stage is Stage.unknown
        assert {m.modality for m in cap.missing} == {ArtefactType.html, ArtefactType.har, ArtefactType.screenshot}
        (art,) = cap.artefacts
        assert art.type is ArtefactType.url
        assert art.path == "scam_links_example.txt"


def test_listed_url_matches_the_bare_url_incident(tmp_path):
    listed = next(i for i in load_inputs([list_file(tmp_path)]).incidents if i.id == ids.input_incident_id(URL_B))
    bare = load_inputs([URL_B]).incidents[0]
    assert listed.id == bare.id
    assert listed.captures[0].artefacts[0].sha256 == bare.captures[0].artefacts[0].sha256


def test_each_url_is_read_back_from_its_own_line(tmp_path):
    data = list_file(tmp_path).read_bytes()
    assert urllist.find_line(data, urllist.digest(URL_B)) == URL_B
    assert urllist.find_line(data, urllist.digest("https://nowhere.example.com/")) is None
    assert urllist.urls(data) == [URL_A, URL_B, URL_C, URL_A]


def test_bom_and_crlf_lines_are_read(tmp_path):
    path = write(tmp_path / "scam_links_bom.txt", "﻿" + URL_A + "\r\n" + URL_B + "\r\n")
    assert adapters.is_url_list(path)
    assert [i.id for i in load_inputs([path]).incidents] == [ids.input_incident_id(URL_A), ids.input_incident_id(URL_B)]


def test_organiser_root_picks_up_top_level_scam_links(tmp_path):
    raw = tmp_path / "raw"
    write(raw / "BACKUP" / "p1" / "page.html", "<html></html>")
    (raw / "do_udostepnienia").mkdir()
    list_file(raw, name="scam_links_odszyfrowane.txt")
    write(raw / "notes.txt", "Case notes, not a list")
    ds = load_inputs([raw], root=raw)
    url_incidents = [i for i in ds.incidents if i.source == "url_list"]
    assert len(url_incidents) == 3
    assert {i.source_ref for i in url_incidents} == {"scam_links_odszyfrowane.txt"}
    assert {a.path for i in url_incidents for c in i.captures for a in c.artefacts} == {"scam_links_odszyfrowane.txt"}


# -- extractors read each URL's own line ----------------------------------------------------------


def test_landing_and_url_shape_stay_per_incident(tmp_path):
    raw = tmp_path / "raw"
    result = run([list_file(raw)], root=raw)
    by_id = {i.id: i for i in result.dataset.incidents}
    alpha = by_id[ids.input_incident_id(URL_A)]
    beta = by_id[ids.input_incident_id(URL_B)]

    assert {(i.kind, i.value) for i in alpha.indicators if i.kind.startswith("landing")} == {
        ("landing_host", "alpha.example.com"),
        ("landing_domain", "example.com"),
    }
    assert {i.value for i in beta.indicators if i.kind == "landing_host"} == {"beta.example.net"}
    shapes = " ".join(i.value for i in alpha.indicators if i.kind == "page_url_shape")
    assert "gclid" in shapes and "utm_campaign" in shapes  # parameter names only, no values
    assert all(i.observed is Observed.static for i in alpha.indicators)


def test_platform_reads_the_url_parameters(tmp_path):
    raw = tmp_path / "raw"
    result = run([list_file(raw)], root=raw)
    beta = next(i for i in result.dataset.incidents if i.id == ids.input_incident_id(URL_B))
    meta_ads = [i for i in beta.indicators if i.kind == "meta_ad_id"]
    assert [i.value for i in meta_ads] == ["120200000000000001"]
    assert meta_ads[0].observed is Observed.static
    assert meta_ads[0].provenance.artefact_id == beta.captures[0].artefacts[0].id


def test_no_token_or_click_value_in_the_dataset(tmp_path):
    raw = tmp_path / "raw"
    dump = run([list_file(raw)], root=raw).dataset.to_json()
    for token in TOKENS:
        assert token not in dump


def test_url_artefact_reads_only_its_line(tmp_path):
    raw = tmp_path / "raw"
    result = run([list_file(raw)], root=raw)
    alpha = next(i for i in result.dataset.incidents if i.id == ids.input_incident_id(URL_A))
    (art,) = alpha.captures[0].artefacts
    assert _pages.url_text(raw, art) == URL_A.encode()


def test_plain_url_file_is_still_read_whole(tmp_path):
    text = "https://trk.example.net/click?x=1\nhttps://cloak.example.org/p\n"
    write(tmp_path / "x" / "url.txt", text)
    digest = hashlib.sha256(text.encode()).hexdigest()  # a plain file carries the digest of its whole content
    art = Artefact(id="x:url", type=ArtefactType.url, path="x/url.txt", sha256=digest)
    assert _pages.url_text(tmp_path, art) == text.encode()


def test_list_member_whose_line_is_gone_reads_nothing(tmp_path):
    raw = tmp_path / "raw"
    result = run([list_file(raw)], root=raw)
    alpha = next(i for i in result.dataset.incidents if i.id == ids.input_incident_id(URL_A))
    (art,) = alpha.captures[0].artefacts
    # the organiser re-saves the list without URL_A: no neighbour's URL may stand in for it
    list_file_text = "\n".join([HEADER, URL_B, URL_C])
    write(raw / "scam_links_example.txt", list_file_text)
    assert _pages.url_text(raw, art) is None


def test_scheme_is_case_insensitive_and_url_kept_as_written():
    assert urllist.url_line(b"HTTPS://Alpha.Example.com/X?y=1") == "HTTPS://Alpha.Example.com/X?y=1"
