"""Rules of scripts/image_leak_check.py on synthetic tar streams (no docker, no organiser data)."""

import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "image_leak_check.py"
_spec = importlib.util.spec_from_file_location("image_leak_check", SCRIPT)
leak = importlib.util.module_from_spec(_spec)
sys.modules["image_leak_check"] = leak
_spec.loader.exec_module(leak)

FAKE_KEY = b"-----BEGIN " + b"PRIVATE KEY-----\n" + b"QUJD" * 16 + b"\n"
KEY_HEADER_ONLY = b"\x00-----BEGIN " + b"PRIVATE KEY-----\x00%s\x00"


def _tar(files: dict[str, bytes], dirs: tuple[str, ...] = ()) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for d in dirs:
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def _rules(files, raw=None, dirs=()):
    return sorted({rule for _, rule in leak.scan_tar(_tar(files, dirs), raw)[1]})


def test_clean_filesystem():
    files = {
        "usr/local/lib/python3.11/site-packages/ftf/cli.py": b"print('hi')",
        "usr/local/lib/python3.11/site-packages/streamlit/static/logo.png": b"\x89PNG",
        "home/ftf/.bashrc": b"# bash",
        "etc/ssl/certs/ca-certificates.crt": b"-----BEGIN CERTIFICATE-----",
    }
    n, hits = leak.scan_tar(_tar(files, dirs=("data/", "data/raw/", "base/")))
    assert hits == [] and n == 7


@pytest.mark.parametrize("name,rule", [
    ("src/.git/config", "git metadata"),
    ("home/ftf/x.har", "capture file"),
    ("tmp/page.MHTML", "capture file"),
    ("base/dataset.json", "campaign base file"),
    ("app/data/enrichment/cache.json", "campaign base file"),
    ("opt/x/vectors/clip.npy", "campaign base file"),
    ("home/ftf/.env", "credential file"),
    (".env", "credential file"),
    ("root/.config/gcloud/credentials.db", "credential file"),
    ("data/raw/case/notes.txt", "file under a data volume"),
    ("home/ftf/shot.png", "media outside library trees"),
])
def test_path_rules(name, rule):
    assert rule in _rules({name: b"x"})


def test_git_directory_entry():
    assert _rules({}, dirs=("work/.git/",)) == ["git metadata"]


def test_secret_contents():
    assert _rules({"usr/lib/k.pem": FAKE_KEY}) == ["private key"]
    assert _rules({"usr/lib/libtls.so": KEY_HEADER_ONLY}) == []
    assert _rules({"usr/lib/libgnutls.so": b"\x7fELF" + FAKE_KEY}) == []  # sample key inside a library
    assert _rules({"usr/lib/sa.txt": b'{"type": "service_account"}'}) == ["service account"]
    assert _rules({"usr/lib/t.txt": b"token=ghp_" + b"a" * 36}) == ["github token"]


def test_raw_index_matches_copies_and_case_names(tmp_path):
    raw = tmp_path / "raw"
    (raw / "case-alpha").mkdir(parents=True)
    (raw / "case-alpha" / "ad.bin").write_bytes(b"organiser bytes" * 64)
    (raw / "case-alpha" / "empty.txt").write_bytes(b"")
    idx = leak.RawIndex.build(raw)
    assert _rules({"usr/share/x/renamed.dat": b"organiser bytes" * 64}, idx) == ["byte-identical to an organiser file"]
    assert _rules({"usr/share/case-alpha/readme": b"y"}, idx) == ["organiser case folder name"]
    assert _rules({"usr/share/other/readme": b"organiser bytes!"}, idx) == []
    assert _rules({"usr/lib/py.typed": b""}, idx) == []  # tiny files are not hash-matched


def test_large_file_is_scanned_in_chunks(monkeypatch):
    monkeypatch.setattr(leak, "MAX_CONTENT", 100)
    monkeypatch.setattr(leak, "CHUNK", 64)
    big = b"x" * 1000 + FAKE_KEY + b"y" * 1000  # the key straddles chunk boundaries
    assert _rules({"usr/share/doc/big.txt": big}) == ["private key"]
    assert _rules({"usr/lib/big.so": b"\x7fELF" + big}) == []
