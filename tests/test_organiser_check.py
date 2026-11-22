"""scripts/check_no_organiser_values.py on a synthetic dataset (no organiser data)."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_no_organiser_values.py"
VALUE = "landing-demo-42"  # invented id-like value standing in for an organiser value


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check_no_organiser_values", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dataset(tmp_path: Path) -> Path:
    ds = {
        "incidents": [{
            "id": "demo-1",
            "indicators": [
                {"kind": "aio_landing", "layer": "landing", "value": VALUE},
                {"kind": "ad_caption", "layer": "creative", "value": "plain caption"},
            ],
        }],
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(ds), encoding="utf-8")
    return path


def _tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "tree"
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def test_clean_tree_passes(check, tmp_path, capsys):
    root = _tree(tmp_path, {"src/a.py": "print('hello')\n"})
    assert check.check(_dataset(tmp_path), root, None) == 0
    assert "PASS" in capsys.readouterr().out


def test_hit_fails_and_never_prints_the_value(check, tmp_path, capsys):
    root = _tree(tmp_path, {"src/a.py": f"x = '{VALUE}'\n"})
    assert check.check(_dataset(tmp_path), root, None) == 1
    out = capsys.readouterr().out
    assert "src/a.py:1: organiser id" in out
    assert VALUE not in out


def test_nested_data_directory_is_scanned(check, tmp_path):
    root = _tree(tmp_path, {"src/pkg/data/planted.txt": f"{VALUE}\n"})
    assert check.check(_dataset(tmp_path), root, None) == 1


def test_root_data_directory_is_not_scanned(check, tmp_path):
    root = _tree(tmp_path, {"data/local.txt": f"{VALUE}\n", "src/a.py": "ok\n"})
    assert check.check(_dataset(tmp_path), root, None) == 0


def test_commit_messages_are_scanned(check, tmp_path, capsys):
    root = _tree(tmp_path, {"src/a.py": "ok\n"})
    messages = tmp_path / "messages.txt"
    messages.write_text(f"Fix {VALUE}\n", encoding="utf-8")
    assert check.check(_dataset(tmp_path), root, messages) == 1
    assert "commit-messages:1: organiser id" in capsys.readouterr().out


def test_empty_candidate_set_is_an_error(check, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"incidents": []}), encoding="utf-8")
    root = _tree(tmp_path, {"src/a.py": "ok\n"})
    assert check.check(empty, root, None) == 2


def test_public_hosts_match_exactly_only(check):
    assert check._public("www.google.com")
    assert check._public("cert.pl")
    assert not check._public("cert.pl.attacker.example")
    assert not check._public("sub.facebook.com.invented")


@pytest.mark.parametrize("domain", ["ads-twitter.com", "bing.com", "clarity.ms", "quora.com", "taboola.com"])
def test_public_service_in_dataset_is_not_a_hit(check, tmp_path, domain):
    ds = {"incidents": [{"id": "demo-1", "indicators": [
        {"kind": "script_url", "layer": "landing", "value": f"https://{domain}/tag.js"},
        {"kind": "aio_landing", "layer": "landing", "value": VALUE},
    ]}]}
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(ds), encoding="utf-8")
    root = _tree(tmp_path, {"src/a.py": f"HOST = '{domain}'\n"})
    assert check.check(path, root, None) == 0


@pytest.mark.parametrize("lookalike", ["bing.com.fixture-lookalike.net", "clarity.ms.fixture-lookalike.net", "taboola.com-mirror.fixture-lookalike.net"])
def test_lookalike_of_public_service_is_still_a_hit(check, tmp_path, lookalike):
    ds = {"incidents": [{"id": "demo-1", "indicators": [
        {"kind": "script_url", "layer": "landing", "value": f"https://{lookalike}/tag.js"},
        {"kind": "aio_landing", "layer": "landing", "value": VALUE},
    ]}]}
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(ds), encoding="utf-8")
    root = _tree(tmp_path, {"src/a.py": f"HOST = '{lookalike}'\n"})
    assert check.check(path, root, None) == 1
