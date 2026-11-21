import pytest

from ftf.enrich.http import USER_AGENT, PoliteClient, Politeness, Response, Unavailable

POLICY = Politeness(min_interval=5.0, timeout=1, retries=2, backoff=3)


class Script:
    """Scripted fetch: each call pops the next item (a Response or an exception)."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def __call__(self, url, headers, timeout, max_bytes):
        self.calls.append((url, dict(headers)))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_user_agent_is_generic_and_sent():
    assert "staszek" not in USER_AGENT.lower() and "plenti" not in USER_AGENT.lower()
    script = Script(Response(200, b"{}"))
    PoliteClient(script, clock=Clock(), sleep=lambda s: None).get("https://crt.sh/?q=x", POLICY)
    assert script.calls[0][1]["User-Agent"] == USER_AGENT


def test_retries_429_honouring_retry_after_then_succeeds():
    clock = Clock()
    script = Script(Response(429, b"", {"retry-after": "7"}), Response(200, b"[]"))
    resp = PoliteClient(script, clock=clock, sleep=clock.sleep).get("https://crt.sh/?q=x", POLICY)
    assert resp.status == 200
    assert len(script.calls) == 2
    assert 7.0 in clock.slept


def test_gives_up_after_retries_with_unavailable():
    clock = Clock()
    script = Script(Response(502, b""), Response(503, b""), Response(502, b""))
    unpaced = Politeness(min_interval=0.0, timeout=1, retries=2, backoff=3)
    with pytest.raises(Unavailable, match="after 3 attempts"):
        PoliteClient(script, clock=clock, sleep=clock.sleep).get("https://crt.sh/?q=x", unpaced)
    assert clock.slept == [3, 6]  # exponential backoff


def test_network_errors_are_retried_then_raised():
    clock = Clock()
    script = Script(Unavailable("timeout"), Unavailable("timeout"), Unavailable("timeout"))
    with pytest.raises(Unavailable):
        PoliteClient(script, clock=clock, sleep=clock.sleep).get("https://crt.sh/?q=x", POLICY)
    assert len(script.calls) == 3


def test_404_is_an_answer_not_retried():
    script = Script(Response(404, b""))
    assert PoliteClient(script, clock=Clock(), sleep=lambda s: None).get("https://rdap/x", POLICY).status == 404
    assert len(script.calls) == 1


def test_same_host_is_paced_by_min_interval():
    clock = Clock()
    script = Script(Response(200, b"{}"), Response(200, b"{}"))
    client = PoliteClient(script, clock=clock, sleep=clock.sleep)
    client.get("https://crt.sh/?q=a", POLICY)
    client.get("https://crt.sh/?q=b", POLICY)
    assert clock.slept == [5.0]
    assert client.requests["crt.sh"] == 2


def test_oversized_body_is_unavailable_and_not_retried():
    script = Script(Response(200, b"x" * 10, truncated=True))
    with pytest.raises(Unavailable, match="larger than"):
        PoliteClient(script, clock=Clock(), sleep=lambda s: None).get("https://hole.cert.pl/x", POLICY)
    assert len(script.calls) == 1
