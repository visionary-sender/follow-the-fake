import json
from datetime import datetime, timedelta, timezone

from ftf.enrich.cache import SCHEMA, Cache

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def test_put_get_roundtrip_and_ttl(tmp_path):
    cache = Cache(tmp_path / "c.json")
    cache.put("rdap", "example.com", status="ok", data={"registrar": "X"}, fetched_at=T0, ttl=timedelta(days=1), url="https://u")
    entry = cache.get("rdap", "example.com")
    assert entry.status == "ok" and entry.data == {"registrar": "X"} and entry.url == "https://u"
    assert entry.fresh(T0 + timedelta(hours=23))
    assert not entry.fresh(T0 + timedelta(days=2))
    assert cache.get("rdap", "other.com") is None


def test_save_and_reload_keeps_timestamps(tmp_path):
    path = tmp_path / "c.json"
    cache = Cache(path)
    cache.put("crt_sh", "example.com", status="not_found", data={"certificates": 0}, fetched_at=T0, ttl=timedelta(days=7))
    cache.save()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == SCHEMA
    reloaded = Cache(path)
    assert len(reloaded) == 1
    entry = reloaded.get("crt_sh", "example.com")
    assert entry.fetched_at == T0 and entry.expires_at == T0 + timedelta(days=7)


def test_corrupt_or_foreign_file_is_ignored_with_warning(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    cache = Cache(path)
    assert len(cache) == 0 and cache.warnings
    path.write_text(json.dumps({"schema": 99, "entries": {}}), encoding="utf-8")
    assert Cache(path).warnings


def test_save_leaves_no_temp_files(tmp_path):
    cache = Cache(tmp_path / "sub" / "c.json")
    cache.put("dns_doh", "example.com", status="ok", data={}, fetched_at=T0, ttl=timedelta(days=1))
    cache.save()
    assert sorted(p.name for p in (tmp_path / "sub").iterdir()) == ["c.json"]


def test_in_memory_cache_saves_nothing():
    cache = Cache(None)
    cache.put("dns_doh", "example.com", status="ok", data={}, fetched_at=T0, ttl=timedelta(days=1))
    cache.save()
    assert cache.get("dns_doh", "example.com") is not None


def test_malformed_source_is_dropped_with_warning(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"schema": 1, "entries": {"rdap": "oops", "dns_doh": {"example.com": {"status": "ok"}}}}), encoding="utf-8")
    cache = Cache(path)
    assert cache.get("rdap", "example.com") is None
    assert cache.warnings and "rdap" in cache.warnings[0]
    assert cache.get("dns_doh", "example.com") is None  # record without timestamps is ignored, not fatal
