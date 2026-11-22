"""Tag patterns that name no host (HACK-60): tested with invented hosts, never with real ones."""

from ftf.indicators import _ids


def _clarity(text: str) -> list[str]:
    return [h.value for h in _ids.scan(text, contacts=False) if h.kind == "clarity_id"]


def test_bare_tag_path_gives_no_id():
    # A URL shape alone would link unrelated sites that share the path (weight 3.0 in linking).
    assert _clarity('<script src="https://invented-tag-host.ms/tag/abc12345xy"></script>') == []


def test_script_call_form_still_gives_the_id():
    assert _clarity("window.x=1; f('clarity', 'script', 'abc12345xy');") == ["abc12345xy"]
