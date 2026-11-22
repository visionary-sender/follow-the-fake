"""scripts/check_no_private_data.py on synthetic trees and a synthetic dataset (invented values only)."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_no_private_data.py"
# Invented private contacts, assembled so this file itself stays clean.
CONTACT_EMAIL = "jane.doe" + "@" + "mail.example.pl"
CONTACT_PHONE = "+48 512 " + "345 678"
PERSON_EMAIL = "someone.else" + "@" + "mail.example.pl"
SCAM_HOST = "login.scam-demo.example.test"  # invented scam infrastructure: allowed in git
SECRET_KEY = "AKIA" + "EXAMPLE0KEY0DEMO"  # invented AWS-shaped key (assembled so this file stays clean)


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check_no_private_data", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dataset(tmp_path: Path, extra: list | None = None) -> Path:
    indicators = [
        {"kind": "aio_landing", "layer": "landing", "value": f"https://{SCAM_HOST}/offer/demo-42"},
        {"kind": "ad_caption", "layer": "creative", "value": "plain caption"},
    ] + (extra or [])
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({"incidents": [{"id": "demo-1", "indicators": indicators}]}), encoding="utf-8")
    return path


def _tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "tree"
    for rel, data in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            f.write_bytes(data)
        else:
            f.write_text(data, encoding="utf-8")
    return root


def test_clean_tree_passes(check, tmp_path, capsys):
    root = _tree(tmp_path, {"src/a.py": "print('hello')\n"})
    assert check.check(_dataset(tmp_path), root, None) == 0
    assert "PASS" in capsys.readouterr().out


def test_scam_domains_urls_and_ids_are_not_hits(check, tmp_path, capsys):
    root = _tree(tmp_path, {
        "src/a.py": f"HOST = 'https://{SCAM_HOST}/offer/demo-42?id=918273645'\n",
        "docs/b.md": f"Seen at {SCAM_HOST} with pixel 123456789012345.\n",
    })
    assert check.check(_dataset(tmp_path), root, None) == 0
    assert "PASS" in capsys.readouterr().out


def test_public_figure_and_brand_names_are_not_hits(check, tmp_path):
    root = _tree(tmp_path, {"src/brands.py": "NAMES = ['Donald Tusk', 'TVN24', 'Bank Example']\n"})
    assert check.check(_dataset(tmp_path), root, None) == 0


def test_email_in_tree_is_a_personal_hit_and_value_is_not_printed(check, tmp_path, capsys):
    root = _tree(tmp_path, {"src/a.py": f"contact = '{PERSON_EMAIL}'\n"})
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    assert "src/a.py:1: personal" in out
    assert "someone.else" not in out


def test_placeholder_email_is_not_a_hit(check, tmp_path):
    root = _tree(tmp_path, {"src/a.py": "sender = 'noreply@example.com'\n"})
    assert check.check(_dataset(tmp_path), root, None) == 0


def test_phone_number_in_tree_is_a_personal_hit(check, tmp_path, capsys):
    root = _tree(tmp_path, {"src/a.py": f"call = '{CONTACT_PHONE}'\n"})
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    assert "src/a.py:1: personal" in out
    assert "512345" not in out


def test_dataset_contact_is_searched_for_in_any_format(check, tmp_path, capsys):
    dataset = _dataset(tmp_path, extra=[
        {"kind": "ad_caption", "layer": "creative", "value": f"contact {CONTACT_EMAIL} {CONTACT_PHONE}"},
    ])
    root = _tree(tmp_path, {
        "src/a.py": f"x = '{CONTACT_EMAIL.upper()}'\n",
        "src/b.txt": "512345678\n",
    })
    assert check.check(dataset, root, None) == 1
    out = capsys.readouterr().out
    assert "src/a.py:1: personal" in out
    assert "src/b.txt:1: personal" in out
    assert "jane" not in out.lower()


def test_private_key_is_a_secret_hit(check, tmp_path, capsys):
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    root = _tree(tmp_path, {"deploy/key.txt": f"{header}\nMIIB\n"})
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    assert "deploy/key.txt:1: secret" in out
    assert "MIIB" not in out


def test_cloud_key_and_password_assignment_are_secret_hits(check, tmp_path, capsys):
    root = _tree(tmp_path, {
        "src/a.py": f"KEY = '{SECRET_KEY}'\n",
        "src/b.py": "db_pass" + "word = 'Sup3rSecretValue'\n",
    })
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    assert "src/a.py:1: secret" in out
    assert "src/b.py:1: secret" in out
    assert SECRET_KEY not in out


def test_raw_capture_by_extension_is_a_hit(check, tmp_path, capsys):
    root = _tree(tmp_path, {
        "src/trace.har": '{"log": {"version": "1.2"}}',
        "src/page.mhtml": "MIME-Version: 1.0\n",
        "src/shot.png": b"\x89PNG\r\n\x1a\n\x00\x00",
        "src/report.pdf": "%PDF-1.4\n",
    })
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    for rel in ("src/trace.har", "src/page.mhtml", "src/shot.png", "src/report.pdf"):
        assert f"{rel}:1: raw" in out


def test_raw_capture_by_content_is_a_hit(check, tmp_path, capsys):
    har = json.dumps({"log": {"version": "1.2", "entries": []}})
    root = _tree(tmp_path, {"src/export.json": har, "src/capture.txt": "<!DOCTYPE html><html></html>\n"})
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    assert "src/export.json:1: raw" in out
    assert "src/capture.txt:1: raw" in out


def test_allowed_invented_fixture_passes(check, tmp_path):
    root = _tree(tmp_path, {"tests/scanner/fixtures/meta_ssr.html": "<!DOCTYPE html><html></html>\n"})
    assert check.check(_dataset(tmp_path), root, None) == 0


def test_source_mentioning_a_mime_header_is_not_a_capture(check, tmp_path):
    root = _tree(tmp_path, {"src/a.py": "HEADER = b'MIME-Version: 1.0'\n"})
    assert check.check(_dataset(tmp_path), root, None) == 0


def test_invented_fixture_path_allows_personal_but_not_secrets(check, tmp_path, capsys):
    root = _tree(tmp_path, {
        "tests/enrich/test_enrich_sources.py": f"x = '{PERSON_EMAIL}'\n",
    })
    assert check.check(_dataset(tmp_path), root, None) == 0
    root = _tree(tmp_path, {
        "tests/enrich/test_enrich_sources.py": f"x = '{PERSON_EMAIL}'\nKEY = '{SECRET_KEY}'\n",
    })
    assert check.check(_dataset(tmp_path), root, None) == 1
    assert "tests/enrich/test_enrich_sources.py:2: secret" in capsys.readouterr().out


def test_root_data_directory_is_not_scanned(check, tmp_path):
    root = _tree(tmp_path, {"data/raw/capture.har": '{"log": {"entries": []}}', "src/a.py": "x = 1\n"})
    assert check.check(_dataset(tmp_path), root, None) == 0


def test_commit_messages_are_scanned(check, tmp_path, capsys):
    messages = tmp_path / "messages.txt"
    messages.write_text(f"Fix contact {CONTACT_PHONE}\n", encoding="utf-8")
    root = _tree(tmp_path, {"src/a.py": "x = 1\n"})
    assert check.check(_dataset(tmp_path), root, messages) == 1
    out = capsys.readouterr().out
    assert "commit-messages:1: personal" in out
    assert "512345" not in out


def test_missing_dataset_is_an_error(check, tmp_path):
    root = _tree(tmp_path, {"src/a.py": "x = 1\n"})
    assert check.check(tmp_path / "absent.json", root, None) == 2


def test_dataset_phone_with_zero_prefix_is_searched_for(check, tmp_path, capsys):
    dataset = _dataset(tmp_path, extra=[{"kind": "ad_caption", "layer": "creative", "value": "call 0512 345 678"}])
    root = _tree(tmp_path, {"src/c.txt": "512345678\n"})
    assert check.check(dataset, root, None) == 1
    assert "src/c.txt:1: personal" in capsys.readouterr().out


def test_dataset_contact_in_allowlisted_fixture_is_still_a_hit(check, tmp_path, capsys):
    dataset = _dataset(tmp_path, extra=[{"kind": "ad_caption", "layer": "creative", "value": CONTACT_EMAIL}])
    root = _tree(tmp_path, {"tests/enrich/test_enrich_sources.py": f"x = '{CONTACT_EMAIL}'\n"})
    assert check.check(dataset, root, None) == 1
    assert "tests/enrich/test_enrich_sources.py:1: personal" in capsys.readouterr().out


def test_dataset_without_indicator_values_is_an_error(check, tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"incidents": [{"id": "x", "indicators": []}]}), encoding="utf-8")
    root = _tree(tmp_path, {"src/a.py": "x = 1\n"})
    assert check.check(path, root, None) == 2


def test_archive_is_a_raw_hit(check, tmp_path, capsys):
    root = _tree(tmp_path, {"src/cases.zip": b"PK\x03\x04\x00"})
    assert check.check(_dataset(tmp_path), root, None) == 1
    assert "src/cases.zip:1: raw" in capsys.readouterr().out


def test_url_with_credentials_is_a_secret_hit(check, tmp_path, capsys):
    url = "https://svc:" + "Passw0rdValue" + "@host.example.test/x"  # assembled: invented credential
    root = _tree(tmp_path, {"src/a.py": f"URL = '{url}'\n"})
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    assert "src/a.py:1: secret" in out
    assert "Passw0rd" not in out


def test_placeholders_are_not_secrets(check, tmp_path):
    root = _tree(tmp_path, {
        "docs/a.md": "PROXY=http://<user>:<password>@<host>:<port>\n",
        "scripts/b.sh": 'GH_TOKEN="$(gh_token)"\n',
    })
    assert check.check(_dataset(tmp_path), root, None) == 0


def test_invented_secret_fixture_keeps_private_key_check(check, tmp_path, capsys):
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    token = "TOKEN = 'abcdef" + "1234567890'"  # assembled: invented token literal
    root = _tree(tmp_path, {
        "tests/indicators/test_kit.py": f"{token}\n{header}\n",
    })
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    assert "tests/indicators/test_kit.py:2: secret" in out
    assert "tests/indicators/test_kit.py:1" not in out
