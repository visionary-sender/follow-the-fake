"""Synthetic fixtures for ingestion tests: small fake captures on example.test domains, built at test time."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest


def har(url: str, started: str = "2025-01-02T03:10:00.000Z", extra: list[str] = ()) -> str:
    entries = [
        {
            "startedDateTime": started,
            "request": {"method": "GET", "url": u},
            "response": {"status": 200, "content": {"mimeType": "text/html" if i == 0 else "text/javascript"}},
        }
        for i, u in enumerate([url, *extra])
    ]
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "WebInspector"}, "pages": [{"startedDateTime": started, "id": "page_1", "title": url}], "entries": entries}})


def html(saved_from: str | None = None, body: str = "<p>hello</p>", trackers: bool = True) -> str:
    head = f"<!-- saved from url=({len(saved_from):04d}){saved_from} -->\n" if saved_from else ""
    script = "<script>window.aio = JSON.parse('{}'); fbq('init', '1');</script>" if trackers else ""
    return f"{head}<!DOCTYPE html><html><head><title>t</title>{script}</head><body>{body}</body></html>"


def write(path: Path, data: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def saved_page(folder: Path, stem: str, saved_from: str | None, trackers: bool = True, body: str = "<p>x</p>") -> None:
    write(folder / f"{stem}.html", html(saved_from, body=body, trackers=trackers))
    write(folder / f"{stem}_files" / "main.js.pobrane", f"// kit {stem} {body}")
    write(folder / f"{stem}_files" / "hero.webp", f"img {stem} {body}".encode())


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    """A miniature organiser tree: ``raw/do_udostępnienia`` (NFD name, as on disk) and ``raw/BACKUP``."""
    root = tmp_path / "raw"
    do = root / unicodedata.normalize("NFD", "do_udostępnienia")
    # case 1: ad screenshot + HAR + saved page of one site -> one capture
    c1 = do / "1"
    write(c1 / "Zrzut ekranu 2025-01-02 031405.png", b"\x89PNG ad one")
    write(c1 / "alpha.example.test.har", har("https://alpha.example.test/?utm_id=a"))
    saved_page(c1, "«Wiem» — Gazeta", "https://alpha.example.test/?utm_id=a")
    # case 2: two unrelated sites in one folder, one of them only a HAR
    c2 = do / "2"
    write(c2 / "beta.example.test.har", har("https://beta.example.test/?utm_id=b"))
    saved_page(c2, "Beta page", "https://beta.example.test/?utm_id=b")
    write(c2 / "gamma.example.test.har", har("https://gamma.example.test/L1/?clickid=x", "2025-01-02T03:40:00.000Z"))
    # case 5: ad-library video only
    c5 = do / "5"
    write(c5 / "Ad [123_3].mp4", b"fake video")
    write(c5 / "Ad [123_3].info.json", json.dumps({"id": "123"}))
    # case 61: ad + article (local-file HAR) + chat lander + white page on revisit
    c61 = do / "61"
    write(c61 / "reklama_1.png", b"\x89PNG ad 61")
    write(c61 / "strona_1" / ".har", har("file:///C:/Users/analyst/Downloads/page.html", "2025-01-03T10:00:00.000Z"))
    saved_page(c61 / "strona_1", "37 minut", None)
    write(c61 / "strona_2" / "delta.har", har("https://delta.example.test/?utm_visit=1", "2025-01-03T10:05:00.000Z"))
    saved_page(c61 / "strona_2", "Chat", "https://delta.example.test/?utm_visit=1", body="chat")
    write(c61 / "strona_3" / "delta.example.test.har", har("https://delta.example.test/?utm_visit=1", "2025-01-03T10:10:00.000Z"))
    saved_page(c61 / "strona_3", "Portal", "https://delta.example.test/?utm_visit=1", trackers=False, body="white")
    # BACKUP p1 = p2 byte for byte; p1 also has an automated archive; p10 a deleted-video marker
    bk = root / "BACKUP"
    for p in ("p1", "p2"):
        write(bk / p / "Zrzut ekranu 2025-01-04 061500.png", b"\x89PNG ad p1")
        write(bk / p / "epsilon.example.test.har", har("https://epsilon.example.test/", "2025-01-04T06:30:00.000Z"))
        saved_page(bk / p, "Epsilon", "https://epsilon.example.test/")
    arch = bk / "p1" / "archive_epsilon.example.test_20261008"
    write(arch / "network.har", har("https://epsilon.example.test/", "2025-01-04T06:20:00.000Z"))
    write(arch / "screenshots" / "full.png", b"\x89PNG 502")
    write(bk / "p1" / "archive_epsilon.example.test_20261008.zip", b"PK fake")
    write(bk / "p1" / "archive_epsilon.example.test_20261008.zip.sha256", "abc  archive.zip\n")
    write(bk / "p10" / "Zrzut ekranu 2025-01-04 091500.png", b"\x89PNG p10")
    write(bk / "p10" / "zeta.example.test.har", har("https://zeta.example.test/", "2025-01-04T08:00:00.000Z"))
    saved_page(bk / "p10", "Zeta", "https://zeta.example.test/")
    write(bk / "p10" / "video_deleted.txt", b"")
    return root


MHTML = (
    "From: <Saved by Blink>\r\n"
    "Snapshot-Content-Location: https://eta.example.test/offer\r\n"
    "Subject: Offer\r\n"
    "Date: Mon, 06 Jan 2025 09:00:00 -0000\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/related; type="text/html"; boundary="----B"\r\n'
    "\r\n"
    "------B\r\n"
    "Content-Type: text/html\r\n"
    "Content-Location: https://eta.example.test/offer\r\n"
    "\r\n"
    "<html><body>offer</body></html>\r\n"
    "------B--\r\n"
)
