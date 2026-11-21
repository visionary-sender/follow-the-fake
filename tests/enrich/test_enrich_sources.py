import json

import pytest

from enrich_support import fixture
from ftf.enrich.normalise import registrable_domain
from ftf.enrich.sources import (
    _doh_answers,
    _iso_date,
    issuer_org,
    parse_cert_pl,
    parse_crt,
    parse_rdap,
    rdap_base,
    rdap_services,
)


def registrable(value):
    return registrable_domain(value, tld_cache=None)


def test_iana_bootstrap_maps_pl_and_com_to_https_registry_servers():
    services = rdap_services(json.loads(fixture("iana.json")))
    assert services["pl"] == ["https://rdap.dns.pl/"]
    assert rdap_base("nask.pl", services) == "https://rdap.dns.pl/"
    assert rdap_base("example.com", services).startswith("https://rdap.")
    assert rdap_base("example.example", services) is None


def test_rdap_recorded_example_com():
    parsed = parse_rdap(json.loads(fixture("rdap-example.json")))
    assert parsed == {
        "registrar": "RESERVED-Internet Assigned Numbers Authority",
        "registrar_iana_id": "376",
        "created_date": "1995-08-14",
    }


def test_rdap_rejects_impossible_dates():
    body = {"events": [{"eventAction": "registration", "eventDate": "2020-13-40T00:00:00Z"}]}
    assert parse_rdap(body)["created_date"] is None
    assert _iso_date("2020-02-29T10:00:00Z") == "2020-02-29"
    assert _iso_date("2021-02-29") is None


@pytest.mark.parametrize(
    ("fixture_name", "rtype", "expected"),
    [
        ("doh-example-A.json", "A", sorted(["104.20.23.154", "172.66.147.243"])),
        ("doh-example-NS.json", "NS", ["elliott.ns.cloudflare.com", "hera.ns.cloudflare.com"]),
        ("doh-example-MX.json", "MX", []),  # null MX is "0 ." and carries no host
    ],
)
def test_doh_recorded_answers(fixture_name, rtype, expected):
    assert _doh_answers(json.loads(fixture(fixture_name)), rtype) == expected


def test_doh_ignores_cname_chain_for_a_records():
    body = {"Answer": [{"type": 5, "data": "edge.example.net."}, {"type": 1, "data": "192.0.2.7"}, {"type": 1, "data": "not-an-ip"}]}
    assert _doh_answers(body, "A") == ["192.0.2.7"]


def test_crt_recorded_example_com_has_no_siblings_and_earliest_not_before():
    rows = json.loads(fixture("crt-example.json"))
    parsed = parse_crt(rows, "example.com", registrable)
    assert parsed["certificates"] == len(rows)
    assert parsed["first_seen"] == "2010-09-02"
    # m.testexample.com on a thawte certificate is a real sibling; e-mail and free-text entries are not hosts
    assert parsed["san_siblings"] == ["testexample.com"]
    assert "SSL Corporation" in parsed["issuers"]


def test_crt_synthetic_rows_report_other_registrable_names_only():
    # Synthetic rows in the crt.sh JSON shape, to test sibling logic.
    rows = [
        {"issuer_name": "C=US, O=Let's Encrypt, CN=E7", "name_value": "shop.victim-one.top\nvictim-one.top\nother-site.xyz", "not_before": "2026-11-02T10:00:00"},
        {"issuer_name": "C=US, O=Let's Encrypt, CN=E7", "name_value": "*.victim-one.top\nother-site.xyz", "not_before": "2026-11-05T10:00:00"},
    ]
    parsed = parse_crt(rows, "victim-one.top", registrable)
    assert parsed["san_siblings"] == ["other-site.xyz"]
    assert parsed["issuers"] == ["Let's Encrypt"]
    assert parsed["first_seen"] == "2026-11-02"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("C=US, O=SSL Corporation, CN=Cloudflare", "SSL Corporation"), ("CN=Only A Name", "CN=Only A Name"), (None, None), ("", None)],
)
def test_issuer_org(raw, expected):
    assert issuer_org(raw) == expected


def test_cert_pl_recorded_head_lists_rifutnuota_and_not_example():
    index = parse_cert_pl(json.loads(fixture("certpl-head40.json")))
    assert index["rifutnuota.store"] == {"listed": True, "listed_date": "2026-11-21", "rows": 1}
    assert "example.com" not in index


def test_cert_pl_relisting_uses_active_rows_and_earliest_insert():
    rows = [
        {"DomainAddress": "Relisted.example.top", "InsertDate": "2026-01-10T00:00:00+00:00", "DeleteDate": "2026-02-01T00:00:00+00:00"},
        {"DomainAddress": "relisted.example.top", "InsertDate": "2026-03-05T00:00:00+00:00", "DeleteDate": None},
        {"DomainAddress": "relisted.example.top", "InsertDate": "2026-04-01T00:00:00+00:00", "DeleteDate": None},
        {"DomainAddress": "gone.example.top", "InsertDate": "2026-01-01T00:00:00+00:00", "DeleteDate": "2026-01-09T00:00:00+00:00"},
    ]
    index = parse_cert_pl(rows)
    assert index["relisted.example.top"] == {"listed": True, "listed_date": "2026-03-05", "rows": 3}
    assert index["gone.example.top"]["listed"] is False


def test_bad_bootstrap_shape_raises_so_the_caller_can_treat_it_as_unavailable():
    with pytest.raises((KeyError, TypeError, IndexError)):
        rdap_services({"nope": []})
