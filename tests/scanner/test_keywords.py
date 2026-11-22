"""Search terms come from the caller, never from the code: keyword files, `--keywords`, required terms."""

import re
from pathlib import Path

import pytest

from ftf import scanner
from ftf.cli import main
from ftf.scanner import run as scan_run

REPO = Path(__file__).resolve().parents[2]


def test_no_built_in_terms_in_the_module():
    assert not hasattr(scan_run, "DEFAULT_KEYWORDS")
    assert scan_run.KEYWORD_SOURCES == ("meta", "google_atc")


def test_example_file_loads_and_is_invented():
    loaded = scan_run.load_keywords(REPO / "config" / "scanner-keywords.example.txt")
    assert set(loaded) == {"meta", "google_atc"}
    assert all(term.startswith("placeholder") for terms in loaded.values() for term in terms)


def test_public_keyword_file_loads_and_holds_no_organiser_shapes():
    loaded = scan_run.load_keywords(REPO / "config" / "scanner-keywords.txt")
    assert set(loaded) == {"meta", "google_atc"}
    assert all(loaded[s] for s in loaded)
    assert all(not re.search(r"https?:|/|\.(pl|com|net|org|eu)\b|\d{5,}", term) for terms in loaded.values() for term in terms)


def test_keyword_file_sections_comments_and_duplicates(tmp_path):
    f = tmp_path / "kw.txt"
    f.write_text("# note\n\n[meta]\nalpha beta\n  gamma  \nalpha beta\n[google_atc]\ndelta\n", encoding="utf-8")
    assert scan_run.load_keywords(f) == {"meta": ["alpha beta", "gamma"], "google_atc": ["delta"]}


@pytest.mark.parametrize(
    "text,message",
    [
        ("alpha\n[meta]\n", "must come after a [source] heading"),
        ("[meta]\n[other]\nx\n", "unknown heading"),
    ],
)
def test_keyword_file_errors_name_the_line_not_the_term(tmp_path, text, message):
    f = tmp_path / "kw.txt"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(message)) as exc:
        scan_run.load_keywords(f)
    assert "alpha" not in str(exc.value)  # the term itself is never echoed


def test_keywords_for_list_applies_to_keyword_sources_only():
    assert scan_run.keywords_for(["a"], "meta") == ["a"]
    assert scan_run.keywords_for(["a"], "google_atc") == ["a"]
    assert scan_run.keywords_for(["a"], "certpl") == []
    assert scan_run.keywords_for({"meta": ["m"]}, "google_atc") == []
    assert scan_run.keywords_for(None, "meta") == []


def test_scan_refuses_keyword_sources_without_terms(tmp_path):
    with pytest.raises(ValueError, match="no search terms for meta"):
        scanner.scan(["certpl", "meta"], out=tmp_path / "scan")
    assert not (tmp_path / "scan").exists()  # checked before the store is touched


def test_cli_requires_terms_for_keyword_sources(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(scan_run, "scan", lambda *a, **k: called.append(a) or {})
    code = main(["scan", "--source", "meta", "--out", str(tmp_path)])
    assert code == 2 and not called
    assert "need search terms" in capsys.readouterr().err


def test_cli_certpl_needs_no_terms(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(scan_run, "scan", lambda out, sources, **kw: seen.update(kw) or {"sources": {}, "run_id": "r", "new": 0})
    assert main(["scan", "--source", "certpl", "--out", str(tmp_path)]) == 0
    assert seen["keywords"] is None


def test_cli_keywords_file_passes_per_source_terms(tmp_path, monkeypatch):
    f = tmp_path / "kw.txt"
    f.write_text("[meta]\nalpha\n[google_atc]\nbeta\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(scan_run, "scan", lambda out, sources, **kw: seen.update(kw, sources=sources) or {"sources": {}, "run_id": "r", "new": 0})
    assert main(["scan", "--keywords-file", str(f), "--out", str(tmp_path / "o")]) == 0
    assert seen["keywords"] == {"meta": ["alpha"], "google_atc": ["beta"]}


def test_cli_keywords_file_missing_section_is_refused(tmp_path, monkeypatch, capsys):
    f = tmp_path / "kw.txt"
    f.write_text("[meta]\nalpha\n", encoding="utf-8")
    monkeypatch.setattr(scan_run, "scan", lambda *a, **k: pytest.fail("scan must not run"))
    assert main(["scan", "--source", "google_atc", "--keywords-file", str(f), "--out", str(tmp_path / "o")]) == 2
    assert "google_atc need search terms" in capsys.readouterr().err


def test_cli_keywords_and_file_are_exclusive(tmp_path, capsys):
    f = tmp_path / "kw.txt"
    f.write_text("[meta]\nalpha\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["scan", "--keywords", "a", "--keywords-file", str(f), "--out", str(tmp_path)])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_cli_unreadable_keyword_file_exits_2(tmp_path, capsys):
    assert main(["scan", "--keywords-file", str(tmp_path / "missing.txt"), "--out", str(tmp_path)]) == 2
    assert "cannot read keyword file" in capsys.readouterr().err
