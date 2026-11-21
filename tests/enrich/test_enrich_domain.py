import socket
from datetime import date, timedelta

import pytest

from enrich_support import LOOKUP_HOSTS, NOW, FakeNet, client_for, example_routes
from ftf.contract import Dataset, Layer, Observed
from ftf.enrich.cache import Cache
from ftf.enrich.domain import Enricher, build_dataset
from ftf.enrich.sources import POLICY

UNKNOWN_KINDS = {
    "registrar", "registrar_iana_id", "created_date", "domain_age_days_at_capture",
    "a_record_ip", "nameserver", "ns_provider", "mx",
    "cert_issuer", "cert_first_seen", "cert_san_sibling", "cert_pl_listed",
}


def by_kind(result):
    out = {}
    for ind in result.incident.indicators:
        out.setdefault(ind.kind, []).append(ind)
    return out


def test_live_enrichment_returns_valid_contract_indicators(tmp_path):
    net = FakeNet()
    enricher = Enricher(Cache(tmp_path / "cache.json"), client=client_for(net), clock=lambda: NOW)
    result = enricher.enrich("https://www.example.com/landing?x=1", captured_on=date(2026, 11, 21))

    assert result.domain == "example.com"
    dataset = build_dataset([result])
    assert Dataset.from_json(dataset.to_json()) == dataset  # validates against the contract

    kinds = by_kind(result)
    assert kinds["registrar"][0].value == "RESERVED-Internet Assigned Numbers Authority"
    assert kinds["registrar_iana_id"][0].value == "376"
    assert kinds["created_date"][0].value == "1995-08-14"
    assert kinds["domain_age_days_at_capture"][0].value == str((date(2026, 11, 21) - date(1995, 8, 14)).days)
    assert sorted(i.value for i in kinds["a_record_ip"]) == ["104.20.23.154", "172.66.147.243"]
    assert sorted(i.value for i in kinds["nameserver"]) == ["elliott.ns.cloudflare.com", "hera.ns.cloudflare.com"]
    assert [i.value for i in kinds["ns_provider"]] == ["cloudflare.com"]
    assert "mx" not in kinds  # null MX
    assert kinds["cert_first_seen"][0].value == "2010-09-02"
    assert kinds["cert_pl_listed"][0].value == "false"
    assert [i.value for i in kinds["cert_san_sibling"]] == ["testexample.com"]


def test_every_indicator_is_infrastructure_live_with_source_and_fetch_time(tmp_path):
    result = Enricher(Cache(None), client=client_for(FakeNet()), clock=lambda: NOW).enrich("example.com")
    for ind in result.incident.indicators:
        assert ind.layer is Layer.infrastructure
        assert ind.observed is Observed.live
        assert "source=" in ind.provenance.note and "fetched_at=2026-11-21T12:00:00Z" in ind.provenance.note
    levels = {ind.kind: ind.level.value for ind in result.incident.indicators}
    assert levels["registrar"] == "tenant"
    assert levels["nameserver"] == "operator"
    assert levels["ns_provider"] == "platform"
    assert levels["cert_san_sibling"] == "operator"


def test_domain_age_is_assumed_to_be_lookup_date_when_capture_date_unknown():
    result = Enricher(Cache(None), client=client_for(FakeNet()), clock=lambda: NOW).enrich("example.com")
    age = by_kind(result)["domain_age_days_at_capture"][0]
    assert "captured_on=2026-11-21 (assumed: lookup date)" in age.provenance.note


def test_only_lookup_services_are_ever_contacted():
    net = FakeNet()
    Enricher(Cache(None), client=client_for(net), clock=lambda: NOW).enrich("example.com")
    assert net.hosts() <= LOOKUP_HOSTS  # the scanned domain's own host is never contacted


def test_second_run_is_served_from_cache_without_network(tmp_path):
    path = tmp_path / "cache.json"
    first_net = FakeNet()
    Enricher(Cache(path), client=client_for(first_net), clock=lambda: NOW).enrich("example.com")
    assert first_net.calls

    second_net = FakeNet()
    result = Enricher(Cache(path), client=client_for(second_net), clock=lambda: NOW + timedelta(hours=1)).enrich("example.com")
    assert second_net.calls == []
    assert all(look.origin == "cache" for look in result.lookups.values())
    assert by_kind(result)["registrar"][0].provenance.note.startswith("source=rdap; origin=cache; fetched_at=2026-11-21T12:00:00Z")


def test_offline_with_no_cache_returns_unknown_and_never_touches_the_network(monkeypatch, tmp_path):
    """Acceptance: with network disabled, a domain gives `unknown` and no exception."""

    def refuse(*args, **kwargs):
        raise OSError("network disabled for this test")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    net = FakeNet()
    enricher = Enricher(Cache(tmp_path / "empty.json"), client=client_for(net), offline=True, clock=lambda: NOW)
    result = enricher.enrich("example.com")

    assert net.calls == []
    kinds = by_kind(result)
    assert UNKNOWN_KINDS <= set(kinds)
    assert all(ind.value == "unknown" for ind in result.incident.indicators if ind.kind in UNKNOWN_KINDS)
    assert all(look.origin == "none" for look in result.lookups.values())
    assert build_dataset([result]).incidents[0].indicators  # still a valid contract document


def test_network_down_without_cache_gives_unknown_not_an_exception():
    net = FakeNet(down=True)
    result = Enricher(Cache(None), client=client_for(net), clock=lambda: NOW).enrich("example.com")
    unknown = {ind.kind for ind in result.incident.indicators if ind.value == "unknown"}
    assert UNKNOWN_KINDS <= unknown
    assert all(look.status == "unavailable" for look in result.lookups.values())


def test_stale_cache_is_served_and_labelled_when_network_fails(tmp_path):
    path = tmp_path / "cache.json"
    Enricher(Cache(path), client=client_for(FakeNet()), clock=lambda: NOW).enrich("example.com")

    later = NOW + timedelta(days=60)  # every TTL has expired
    result = Enricher(Cache(path), client=client_for(FakeNet(down=True)), clock=lambda: later).enrich("example.com")
    kinds = by_kind(result)
    assert kinds["registrar"][0].value == "RESERVED-Internet Assigned Numbers Authority"
    assert "origin=stale-cache" in kinds["registrar"][0].provenance.note
    assert "fetched_at=2026-11-21T12:00:00Z" in kinds["registrar"][0].provenance.note


def test_failing_source_is_skipped_after_repeated_failures():
    names = ["a1.com", "a2.com", "a3.com", "a4.com", "a5.com"]
    net = FakeNet(routes={**example_routes()}, down=True)
    enricher = Enricher(Cache(None), client=client_for(net), clock=lambda: NOW)
    for name in names:
        enricher.enrich(name)
    crt_calls = [u for u in net.calls if u.startswith("https://crt.sh/")]
    attempts_per_domain = POLICY["crt"].retries + 1
    assert len(crt_calls) == 3 * attempts_per_domain  # three failing domains, then crt.sh is skipped


def test_not_found_domain_gives_no_registry_indicators_but_a_list_answer():
    name = "nothing-here-at-all.pl"
    routes = {
        "https://data.iana.org/rdap/dns.json": example_routes()["https://data.iana.org/rdap/dns.json"],
        f"https://rdap.dns.pl/domain/{name}": 404,
        f"https://dns.google/resolve?name={name}&type=A": b'{"Status":3}',
        f"https://dns.google/resolve?name={name}&type=NS": b'{"Status":3}',
        f"https://dns.google/resolve?name={name}&type=MX": b'{"Status":3}',
        f"https://crt.sh/?q={name}&output=json": b"[]",
        "https://hole.cert.pl/domains/v2/domains.json": example_routes()["https://hole.cert.pl/domains/v2/domains.json"],
    }
    net = FakeNet(routes=routes)
    result = Enricher(Cache(None), client=client_for(net), clock=lambda: NOW).enrich(name)
    kinds = by_kind(result)
    assert result.lookups["rdap"].status == "not_found"
    assert result.lookups["dns_doh"].status == "not_found"
    assert result.lookups["crt_sh"].status == "not_found"
    assert "registrar" not in kinds and "a_record_ip" not in kinds and "cert_issuer" not in kinds
    assert kinds["cert_pl_listed"][0].value == "false"


@pytest.mark.parametrize("value", ["192.0.2.1", "nothing.example"])  # an IP, or a suffix the snapshot does not know
def test_invalid_input_is_rejected_before_any_lookup(value):
    net = FakeNet()
    with pytest.raises(ValueError):
        Enricher(Cache(None), client=client_for(net), clock=lambda: NOW).enrich(value)
    assert net.calls == []


def test_cert_pl_listing_becomes_listed_true_with_date(tmp_path):
    result = Enricher(Cache(None), client=client_for(FakeNet()), clock=lambda: NOW).enrich("rifutnuota.store")
    kinds = by_kind(result)
    assert kinds["cert_pl_listed"][0].value == "true"
    assert kinds["cert_pl_listed_date"][0].value == "2026-11-21"
