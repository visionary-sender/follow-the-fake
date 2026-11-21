"""Pure arithmetic of scripts/spend.py on canned gcloud JSON (no network)."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "spend.py"
_spec = importlib.util.spec_from_file_location("spend", SCRIPT)
spend = importlib.util.module_from_spec(_spec)
sys.modules["spend"] = spend
_spec.loader.exec_module(spend)

FTF, ATTIC = spend.PROJECTS
UTC = timezone.utc
NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
ZONE = "https://www.googleapis.com/compute/v1/projects/p/zones/europe-central2-a"


def t(s):
    return spend.parse_time(s)


def entry(ts, method, name, op=None, first=False, request=None, error=False, kind="instances"):
    e = {"timestamp": ts, "insertId": f"{ts}{method}",
         "protoPayload": {"methodName": f"v1.compute.{kind}.{method}",
                          "resourceName": f"projects/p/zones/europe-central2-a/{kind}/{name}"}}
    if op:
        e["operation"] = {"id": op, "first": first}
    if request:
        e["protoPayload"]["request"] = request
    if error:
        e["protoPayload"]["status"] = {"code": 3, "message": "bad"}
    return e


def instance(name, status, start, stop=None, max_run=None, labels=None, mt="e2-standard-4"):
    sched = {"instanceTerminationAction": "STOP"}
    if max_run:
        sched["maxRunDuration"] = {"seconds": str(max_run)}
    return {"name": name, "zone": ZONE, "status": status, "machineType": f"{ZONE}/machineTypes/{mt}",
            "lastStartTimestamp": start, "lastStopTimestamp": stop, "scheduling": sched,
            "labels": labels or {}, "networkInterfaces": [{"accessConfigs": [{}]}]}


def test_parse_time_variants():
    a = t("2026-11-21T11:43:27.131-08:00")
    assert a == datetime(2026, 11, 21, 19, 43, 27, 131000, tzinfo=UTC)
    assert t("2026-11-21T19:43:55.027365123Z").microsecond == 27365
    assert t("2026-11-21T19:43:55Z").second == 55
    assert t(None) is None
    with pytest.raises(ValueError):
        t("yesterday")


def test_parse_events_merges_operations_and_drops_failures():
    entries = [
        entry("2026-11-21T19:43:09Z", "insert", "vm", op="o1", first=True,
              request={"machineType": "x/e2-standard-4"}),
        entry("2026-11-21T19:43:27Z", "insert", "vm", op="o1"),
        entry("2026-11-21T19:42:56Z", "insert", "vm", error=True),  # failed attempt, no op
        entry("2026-11-21T19:50:00Z", "insert", "other", op="o2", first=True),
        entry("2026-11-21T19:51:00Z", "insert", "other", op="o2", error=True),  # failed async
    ]
    evs = spend.parse_events(entries)
    assert len(evs) == 1
    assert evs[0].time == t("2026-11-21T19:43:09Z")
    assert evs[0].request["machineType"].endswith("e2-standard-4")


def test_intervals_running_instance_counts_to_now():
    evs = spend.parse_events([entry("2026-11-22T08:00:00Z", "insert", "vm", op="o")])
    cur = instance("vm", "RUNNING", "2026-11-22T08:00:00Z", max_run=57600)
    ivs = spend.run_intervals(evs, cur, NOW)
    assert ivs == [(t("2026-11-22T08:00:00Z"), NOW)]
    assert spend.hours(ivs) == 2


def test_intervals_stop_start_and_auto_stop_without_logged_stop():
    evs = spend.parse_events([
        entry("2026-11-21T10:00:00Z", "insert", "vm", op="a"),
        entry("2026-11-21T12:00:00Z", "stop", "vm", op="b"),
        entry("2026-11-21T20:00:00Z", "start", "vm", op="c"),
    ])
    # stopped by maxRunDuration (system event not seen), no lastStopTimestamp
    cur = instance("vm", "TERMINATED", "2026-11-21T20:00:00Z", max_run=4 * 3600)
    ivs = spend.run_intervals(evs, cur, NOW)
    assert spend.hours(ivs) == 2 + 4
    cur["lastStopTimestamp"] = "2026-11-21T21:30:00Z"
    assert spend.hours(spend.run_intervals(evs, cur, NOW)) == 2 + 1.5


def test_intervals_deleted_instance_and_clip_to_since():
    evs = spend.parse_events([
        entry("2026-11-20T22:00:00Z", "start", "vm", op="a"),  # before SINCE (23:00Z)
        entry("2026-11-21T01:00:00Z", "delete", "vm", op="b"),
    ])
    assert spend.hours(spend.run_intervals(evs, None, NOW)) == 2


def test_running_instance_without_events_starts_at_last_start():
    cur = instance("vm", "RUNNING", "2026-11-22T09:30:00Z")
    assert spend.hours(spend.run_intervals([], cur, NOW)) == 0.5


def test_auto_stop_time():
    cur = instance("vm", "RUNNING", "2026-11-21T11:43:27.131-08:00", max_run=57600)
    when, action = spend.auto_stop(cur)
    assert when == t("2026-11-21T19:43:27.131Z") + timedelta(hours=16)
    assert action == "STOP"
    assert spend.auto_stop(instance("vm", "RUNNING", "2026-11-21T10:00:00Z")) == (None, "none")


def test_build_lines_prices_vm_and_disks_and_attributes_issue():
    insert = {"labels": [{"key": "ftf-issue", "value": "hack-9"}],
              "machineType": f"{ZONE}/machineTypes/e2-standard-4",
              "networkInterfaces": [{"accessConfigs": [{}]}],
              "disks": [{"boot": True, "autoDelete": True, "initializeParams": {
                  "diskSizeGb": "100", "diskType": f"{ZONE}/diskTypes/pd-balanced"}}]}
    evs = spend.parse_events([
        entry("2026-11-21T10:00:00Z", "insert", "gone", op="a", request=insert),
        entry("2026-11-21T20:00:00Z", "delete", "gone", op="b"),
    ])
    cur = instance("wb", "RUNNING", "2026-11-22T08:00:00Z")
    disk = {"name": "wb", "zone": ZONE, "type": f"{ZONE}/diskTypes/pd-balanced", "sizeGb": "50",
            "creationTimestamp": "2026-11-22T08:00:00Z", "users": [f"{ZONE}/instances/wb"]}
    lines = {(ln.kind, ln.name): ln for ln in spend.build_lines(FTF, evs, [cur], [disk], NOW)}
    gone = lines[("VM", "gone")]
    assert (gone.issue, gone.hours, gone.exists) == ("hack-9", 10, False)
    assert gone.usd == pytest.approx(10 * (0.157 + 0.005))
    assert lines[("disk", "gone")].usd == pytest.approx(0.12 * 100 * 10 / 730)
    assert lines[("VM", "wb")].issue is None  # unlabelled -> warning
    assert lines[("disk", "wb")].usd == pytest.approx(0.12 * 50 * 2 / 730)


def test_attic_only_our_numbered_resources_and_numeric_label():
    ours = instance("vm-80000034-1", "RUNNING", "2026-11-22T09:00:00Z", mt="g2-standard-8",
                    labels={"k": "34"})
    untagged = instance("vm-80000035-1", "RUNNING", "2026-11-22T09:00:00Z", mt="a2-highgpu-1g")
    theirs = instance("ig-70000005-abcd", "RUNNING", "2026-11-22T09:00:00Z")
    lines = spend.build_lines(ATTIC, [], [ours, untagged, theirs], [], NOW)
    assert [(ln.name, ln.issue) for ln in lines] == [("vm-80000034-1", "hack-34"),
                                                     ("vm-80000035-1", None)]
    assert lines[0].usd == pytest.approx(0.95 + 0.005)


def test_unknown_machine_type_is_flagged_not_priced():
    cur = instance("vm", "RUNNING", "2026-11-22T09:00:00Z", mt="n2-weird-7")
    (line,) = spend.build_lines(FTF, [], [cur], [], NOW)
    assert line.usd is None
    text = spend.render_project(FTF, [line], [cur], NOW, [])
    assert "price unknown" in text and "no pinned price for: n2-weird-7" in text
    assert "NO AUTOMATIC STOP" in text


def test_render_totals_against_cap():
    cur = instance("vm", "RUNNING", "2026-11-22T00:00:00Z", max_run=57600,
                   labels={"ftf-issue": "hack-1"})
    lines = spend.build_lines(FTF, [], [cur], [], NOW)
    text = spend.render_project(FTF, lines, [cur], NOW, ["cannot read disks"])
    pln = 10 * (0.157 + 0.005) * spend.USD_TO_PLN
    assert f"= {pln:.2f} PLN = {pln:.1f}% of the 100 PLN cap" in text
    assert "hack-1:" in text and "auto-STOP at 2026-11-22 16:00 UTC" in text
    assert "! cannot read disks" in text


def test_vm_running_at_since_with_only_a_stop_event():
    evs = spend.parse_events([entry("2026-11-21T05:00:00Z", "stop", "vm", op="a")])
    cur = instance("vm", "TERMINATED", "2026-11-20T22:00:00Z", stop="2026-11-21T05:00:00Z")
    assert spend.hours(spend.run_intervals(evs, cur, NOW)) == 6  # from SINCE 23:00Z
    assert spend.hours(spend.run_intervals(evs, None, NOW)) == 6


def test_vm_stopped_by_gcp_without_any_logged_event():
    cur = instance("vm", "TERMINATED", "2026-11-20T22:00:00Z", stop="2026-11-21T02:00:00Z")
    assert spend.hours(spend.run_intervals([], cur, NOW)) == 3


def test_deleted_vm_without_logged_stop_is_capped_by_its_max_run_duration():
    evs = spend.parse_events([entry("2026-11-21T10:00:00Z", "insert", "vm", op="a", request={
        "scheduling": {"maxRunDuration": {"seconds": "7200"}}})])
    assert spend.hours(spend.run_intervals(evs, None, NOW)) == 2


def test_disk_kept_after_instance_delete_and_unknown_size_flagged():
    insert = {"machineType": "x/e2-standard-4", "disks": [
        {"boot": True, "autoDelete": True, "initializeParams": {"diskType": "x/pd-ssd"}},  # size from image: unknown
        {"autoDelete": False, "initializeParams": {"diskName": "keep", "diskSizeGb": "10",
                                                   "diskType": "x/pd-standard"}}]}
    evs = spend.parse_events([
        entry("2026-11-22T00:00:00Z", "insert", "vm", op="a", request=insert),
        entry("2026-11-22T01:00:00Z", "delete", "vm", op="b"),
        entry("2026-11-22T05:00:00Z", "delete", "keep", op="c", kind="disks"),
    ])
    lines = {(ln.kind, ln.name): ln for ln in spend.build_lines(FTF, evs, [], [], NOW)}
    assert lines[("disk", "keep")].hours == 5
    boot = lines[("disk", "vm")]
    assert boot.usd is None and boot.spec == "pd-ssd ?GB" and boot.hours == 1


def test_regional_disk_and_unreadable_instances():
    disk = {"name": "r", "region": "https://x/regions/europe-west4", "type": "x/pd-balanced",
            "sizeGb": "10", "creationTimestamp": "2026-11-22T09:00:00Z"}
    (line,) = spend.build_lines(FTF, [], [], [disk], NOW)
    assert line.zone == "europe-west4" and line.hours == 1
    assert "running now: UNKNOWN" in spend.render_project(FTF, [line], None, NOW, [])
