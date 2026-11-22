"""Tag patterns that name no host (HACK-60): tested with invented hosts, never with real ones."""

from ftf.indicators import _ids


def _clarity(text: str) -> list[str]:
    return [h.value for h in _ids.scan(text, contacts=False) if h.kind == "clarity_id"]


def test_tag_path_on_any_ms_host_gives_the_id():
    assert _clarity('<script src="https://invented-tag-host.ms/tag/abc12345xy"></script>') == ["abc12345xy"]


def test_tag_path_on_other_hosts_gives_nothing():
    assert _clarity('<script src="https://invented-tag-host.example/tag/abc12345xy"></script>') == []


def test_script_call_form_still_gives_the_id():
    assert _clarity("window.x=1; f('clarity', 'script', 'abc12345xy');") == ["abc12345xy"]
