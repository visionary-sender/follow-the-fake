import pytest

from ftf.enrich.normalise import hostname, registrable_domain


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.Example.co.uk/path?q=1", "example.co.uk"),
        ("a.b.example.com.", "example.com"),
        ("*.example.com", "example.com"),
        ("example.com:8443", "example.com"),
        ("victim.github.io", "victim.github.io"),  # private suffixes keep each hosted site separate
        ("zażółć.pl", "zażółć.pl".encode("idna").decode("ascii")),
    ],
)
def test_registrable_domain_normalises(value, expected, tmp_path):
    assert registrable_domain(value, tld_cache=tmp_path) == expected


@pytest.mark.parametrize("value", ["", "   ", "192.0.2.1", "localhost", "com", "not a host!", "https://"])
def test_non_domains_give_none(value, tmp_path):
    assert registrable_domain(value, tld_cache=tmp_path) is None


def test_hostname_rejects_labels_that_cannot_be_hosts():
    assert hostname("https://exa_mple.com/") is None
    assert hostname("AS207960 Test Intermediate - example.com") is None
