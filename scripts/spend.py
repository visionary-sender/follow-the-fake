"""Cloud spend report, built from what GCP itself records.

    python3 -I scripts/spend.py

For each project (follow-the-fake and attic-212115) it lists every VM and disk of ours that
exists or existed since SINCE, with run intervals taken from the Compute Engine resource state
and the admin-activity / system-event audit logs, an ESTIMATED cost from the pinned price table
below, the total against the cap, and anything still running with the time its automatic stop
fires. It only reads (list/describe/logging read); it never creates, changes or deletes anything.

Runs wherever gcloud has read access to both projects (the laptop is fine: it is gcloud calls
plus arithmetic). Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SINCE = datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc)  # 2026-11-21 00:00 Warsaw time

# Fixed rate so the same inputs always give the same numbers.
USD_TO_PLN = 4.00

# Pinned on-demand list prices in USD, approximate, for the regions we use (europe-central2
# for follow-the-fake, europe-west4 for GPUs on attic); each is the regional price rounded up.
# GPU machine types (g2 = 1x L4, a2-highgpu-1g = 1x A100 40GB) include the GPU.
# Unknown machine or disk types get no price and are flagged in the report.
VM_USD_PER_HOUR = {
    "e2-medium": 0.040,
    "e2-standard-2": 0.079,
    "e2-standard-4": 0.157,
    "e2-standard-8": 0.314,
    "e2-highmem-4": 0.212,
    "g2-standard-4": 0.79,
    "g2-standard-8": 0.95,
    "a2-highgpu-1g": 3.75,
}
EXTERNAL_IP_USD_PER_HOUR = 0.005  # in-use external IPv4, charged while the VM runs
DISK_USD_PER_GB_MONTH = {"pd-standard": 0.048, "pd-balanced": 0.12, "pd-ssd": 0.22}
HOURS_PER_MONTH = 730

# attic-212115 convention (provisional, coordinator 2026-11-21; change it here only):
# nothing there may name this project, so our resources carry a numeric name in the 8xxxxxxx
# range (e.g. vm-80000034-1) and the label k=<HACK issue number> (e.g. k=34).
ATTIC_LABEL_KEY = "k"
ATTIC_NAME_PATTERN = re.compile(r"(?<!\d)8\d{7}(?!\d)")

START_METHODS = {"insert", "start", "resume"}
STOP_METHODS = {"stop", "suspend", "delete", "preempted", "guestTerminate"}
RUNNING_STATES = {"PROVISIONING", "STAGING", "RUNNING"}
LOG_LIMIT = 20000  # audit-log entries read per project (newest first); hitting it is reported


@dataclass(frozen=True)
class Project:
    id: str
    gcloud_config: str
    cap_pln: float
    label_key: str
    numeric_label: bool  # attic: label value is the bare issue number
    name_pattern: re.Pattern | None = None  # attic: only names in our numeric range are ours

    def is_ours(self, name: str, labels: dict) -> bool:
        if self.name_pattern is None:
            return True
        return bool(self.name_pattern.search(name)) or str(labels.get(self.label_key, "")).isdigit()

    def issue(self, labels: dict) -> str | None:
        value = labels.get(self.label_key)
        if not value:
            return None
        if self.numeric_label:
            return f"hack-{value}" if str(value).isdigit() else None
        return str(value)


PROJECTS = [
    Project("follow-the-fake", "follow-the-fake", 100.0, "ftf-issue", False),
    Project("attic-212115", os.environ.get("FTF_ATTIC_GCLOUD_CONFIG", "default"), 100.0,
            ATTIC_LABEL_KEY, True, ATTIC_NAME_PATTERN),
]


# ---------------------------------------------------------------- pure helpers (unit-tested)

def parse_time(value: str | None) -> datetime | None:
    """RFC 3339 from gcloud (Z or offset, any fraction digits) -> aware UTC datetime."""
    if not value:
        return None
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", value)
    if not m:
        raise ValueError(f"unparseable timestamp {value!r}")
    base, frac, tz = m.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    tz = "+00:00" if tz == "Z" else tz
    return datetime.fromisoformat(f"{base}.{frac}{tz}").astimezone(timezone.utc)


def basename(url: str | None) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def labels_dict(labels) -> dict:
    """Labels come as a dict from list calls and as [{key, value}] in audit-log requests."""
    if isinstance(labels, dict):
        return labels
    return {x.get("key"): x.get("value", "") for x in labels or []}


@dataclass
class Event:
    time: datetime
    kind: str  # "instances" or "disks"
    zone: str
    name: str
    method: str  # insert, delete, start, stop, ...
    request: dict = field(default_factory=dict)


def parse_events(entries: list[dict]) -> list[Event]:
    """Audit-log entries -> one Event per successful operation, sorted by time.

    A long-running operation logs a first and a last entry under one operation id; failed
    attempts carry a non-zero status. Events of failed operations are dropped; for the rest the
    earliest timestamp is kept together with the request (which only the first entry carries).
    """
    failed, by_op = set(), {}
    for e in entries:
        pp = e.get("protoPayload") or {}
        op = (e.get("operation") or {}).get("id") or e.get("insertId")
        if (pp.get("status") or {}).get("code"):
            failed.add(op)
            continue
        parts = (pp.get("methodName") or "").split(".")
        m = re.search(r"/(?:zones|regions)/([^/]+)/(instances|disks)/([^/]+)$",
                      pp.get("resourceName") or "")
        if len(parts) < 2 or not m:
            continue
        ev = Event(parse_time(e["timestamp"]), m.group(2), m.group(1), m.group(3), parts[-1],
                   pp.get("request") or {})
        prev = by_op.get(op)
        if prev is None:
            by_op[op] = ev
        else:
            prev.time = min(prev.time, ev.time)
            prev.request = prev.request or ev.request
    return sorted((ev for op, ev in by_op.items() if op not in failed),
                  key=lambda ev: (ev.time, ev.name))


def run_intervals(events: list[Event], current: dict | None, now: datetime,
                  since: datetime = SINCE) -> list[tuple[datetime, datetime]]:
    """Running intervals of one instance, clipped to [since, now].

    events: that instance's lifecycle events, sorted. current: its `instances list` record,
    or None if it no longer exists. An interval still open after the last event is closed by
    the current state: now if running, else lastStopTimestamp, else the automatic stop
    (start + maxRunDuration, from the current record or the insert request) if that is earlier
    than now, else now (over-estimate). A first stop/delete with no start before it means the
    instance was already running at `since` (the log is only read from `since`), so it counts
    from `since`; for a delete of an already stopped instance this over-estimates.
    """
    out, start = [], None
    insert = next((ev.request for ev in events if ev.method == "insert"), {})
    for i, ev in enumerate(events):
        if ev.method in START_METHODS and start is None:
            start = ev.time
        elif ev.method in STOP_METHODS:
            if start is not None:
                out.append((start, ev.time))
            elif i == 0:
                out.append((since, ev.time))
            start = None
    sched = (current if current is not None else insert).get("scheduling") or {}
    max_run = (sched.get("maxRunDuration") or {}).get("seconds")
    auto_end = (lambda s: min(now, s + timedelta(seconds=int(max_run)))) if max_run else \
        (lambda s: now)
    if current is not None:
        status = current.get("status")
        last_start = parse_time(current.get("lastStartTimestamp"))
        last_stop = parse_time(current.get("lastStopTimestamp"))
        if start is None and last_start and (not out or last_start > out[-1][1]):
            if status in RUNNING_STATES:
                # started outside the logged window, or by a path the log missed
                start = last_start
            elif not events and last_stop and last_stop > since >= last_start:
                out.append((since, last_stop))  # running at `since`, stopped by GCP itself
        if start is not None:
            if status in RUNNING_STATES:
                end = now
            elif last_stop and last_stop >= start:
                end = last_stop
            else:
                end = auto_end(start)
            out.append((start, end))
    elif start is not None:
        out.append((start, auto_end(start)))  # gone without a logged stop
    clipped = [(max(a, since), min(b, now)) for a, b in out]
    return [(a, b) for a, b in clipped if b > a]


def hours(intervals) -> float:
    return sum((b - a).total_seconds() for a, b in intervals) / 3600


def vm_usd_per_hour(machine_type: str, external_ip: bool) -> float | None:
    price = VM_USD_PER_HOUR.get(machine_type)
    if price is None:
        return None
    return price + (EXTERNAL_IP_USD_PER_HOUR if external_ip else 0.0)


def disk_usd(disk_type: str, size_gb: float | None, alive_hours: float) -> float | None:
    price = DISK_USD_PER_GB_MONTH.get(disk_type)
    if price is None or size_gb is None:
        return None
    return price * size_gb * alive_hours / HOURS_PER_MONTH


def auto_stop(instance: dict) -> tuple[datetime | None, str]:
    """When a running instance stops itself, and what it does then."""
    sched = instance.get("scheduling") or {}
    action = sched.get("instanceTerminationAction") or "STOP"
    if sched.get("terminationTime"):
        return parse_time(sched["terminationTime"]), action
    seconds = (sched.get("maxRunDuration") or {}).get("seconds")
    start = parse_time(instance.get("lastStartTimestamp"))
    if seconds and start:
        return start + timedelta(seconds=int(seconds)), action
    return None, "none"


@dataclass
class Line:
    kind: str  # VM or disk
    name: str
    zone: str
    spec: str  # machine type, or "<type> <size>GB"
    issue: str | None
    intervals: list
    hours: float
    usd: float | None
    exists: bool


def _has_external_ip(rec: dict) -> bool:
    return any(ni.get("accessConfigs") for ni in rec.get("networkInterfaces") or [])


def build_lines(project: Project, events: list[Event], instances: list[dict],
                disks: list[dict], now: datetime) -> list[Line]:
    """Combine current resources and audit events into priced report lines (ours only)."""
    # Unknown size/type (e.g. a boot disk sized by its image) stays unknown and is flagged.
    def gb(v):
        return float(v) if v else None

    cur_vm = {(basename(i["zone"]), i["name"]): i for i in instances}
    cur_disk = {(basename(d.get("zone") or d.get("region")), d["name"]): d for d in disks}
    vm_events: dict = {}
    disk_rec: dict = {}  # (zone, name) -> dict(type, size, start, end, labels, owner, auto)
    for ev in events:
        key = (ev.zone, ev.name)
        if ev.kind == "instances":
            vm_events.setdefault(key, []).append(ev)
            if ev.method == "insert":
                for d in ev.request.get("disks") or []:
                    ip = d.get("initializeParams") or {}
                    name = ip.get("diskName") or (ev.name if d.get("boot") else None)
                    if name and ip:
                        disk_rec[(ev.zone, name)] = dict(
                            type=basename(ip.get("diskType")) or "unknown-type",
                            size=gb(ip.get("diskSizeGb")), start=ev.time, end=None,
                            labels=labels_dict(ip.get("labels")), owner=key,
                            auto=bool(d.get("autoDelete")))
            elif ev.method == "delete":
                for rec in disk_rec.values():  # auto-delete disks go with their instance
                    if rec["owner"] == key and rec["auto"] and rec["end"] is None:
                        rec["end"] = ev.time
        elif ev.method == "insert":
            disk_rec[key] = dict(type=basename(ev.request.get("type")) or "pd-standard",
                                 size=gb(ev.request.get("sizeGb")), start=ev.time, end=None,
                                 labels=labels_dict(ev.request.get("labels")), owner=None,
                                 auto=False)
        elif ev.method == "delete" and key in disk_rec:
            disk_rec[key]["end"] = ev.time

    lines, vm_issue = [], {}
    for key in sorted(set(vm_events) | set(cur_vm)):
        zone, name = key
        cur = cur_vm.get(key)
        insert = next((e.request for e in vm_events.get(key, []) if e.method == "insert"), {})
        labels = labels_dict(cur["labels"] if cur and cur.get("labels") else insert.get("labels"))
        if not project.is_ours(name, labels):
            continue
        mt = basename((cur or insert).get("machineType")) or "unknown-machine-type"
        ivs = run_intervals(vm_events.get(key, []), cur, now)
        rate = vm_usd_per_hour(mt, _has_external_ip(cur or insert))
        h = hours(ivs)
        vm_issue[key] = project.issue(labels)
        lines.append(Line("VM", name, zone, mt, vm_issue[key], ivs, h,
                          None if rate is None else rate * h, cur is not None))

    for key, d in cur_disk.items():
        users = [re.search(r"/zones/([^/]+)/instances/([^/]+)$", u) for u in d.get("users") or []]
        owner = next((m.groups() for m in users if m), None)
        start = parse_time(d["creationTimestamp"])
        disk_rec[key] = dict(type=basename(d.get("type")), size=gb(d.get("sizeGb")),
                             start=start, end=None, labels=labels_dict(d.get("labels")),
                             owner=owner or (disk_rec.get(key) or {}).get("owner"), auto=False)
    for key in sorted(disk_rec):
        zone, name = key
        rec = disk_rec[key]
        issue = project.issue(rec["labels"]) or vm_issue.get(rec["owner"])
        if not project.is_ours(name, rec["labels"]) and rec["owner"] not in vm_issue:
            continue
        end = rec["end"] or now  # still exists, or gone without a logged delete
        ivs = [(max(rec["start"], SINCE), min(end, now))]
        ivs = [(a, b) for a, b in ivs if b > a]
        h = hours(ivs)
        size = "?" if rec["size"] is None else f"{rec['size']:g}"
        lines.append(Line("disk", name, zone, f"{rec['type']} {size}GB", issue, ivs, h,
                          disk_usd(rec["type"], rec["size"], h), key in cur_disk))
    return lines


def fmt_t(t: datetime | None) -> str:
    return t.strftime("%Y-%m-%d %H:%M UTC") if t else "-"


def render_project(project: Project, lines: list[Line], running: list[dict] | None,
                   now: datetime, errors: list[str]) -> str:
    """running=None means the instance list could not be read."""
    out = [f"== {project.id}  (cap {project.cap_pln:.0f} PLN)"]
    out += [f"   ! {e}" for e in errors]
    if not lines:
        out.append("   no VMs or disks of ours since " + fmt_t(SINCE))
    for ln in lines:
        cost = "price unknown" if ln.usd is None else \
            f"{ln.usd:7.2f} USD {ln.usd * USD_TO_PLN:7.2f} PLN"
        state = "" if ln.exists else " (deleted)"
        out.append(f"   {ln.kind:4} {ln.name}{state}  {ln.zone}  {ln.spec}  "
                   f"issue={ln.issue or 'UNLABELLED'}  {ln.hours:6.2f} h  {cost}")
        for a, b in ln.intervals:
            out.append(f"        {fmt_t(a)} -> {fmt_t(b)}{' (now)' if b == now else ''}")
    total_usd = sum(ln.usd or 0 for ln in lines)
    total_pln = total_usd * USD_TO_PLN
    out.append(f"   TOTAL (estimate) {total_usd:.2f} USD = {total_pln:.2f} PLN = "
               f"{100 * total_pln / project.cap_pln:.1f}% of the {project.cap_pln:.0f} PLN cap")
    by_issue: dict = {}
    for ln in lines:
        by_issue[ln.issue or "UNLABELLED"] = by_issue.get(ln.issue or "UNLABELLED", 0) + \
            (ln.usd or 0) * USD_TO_PLN
    for issue in sorted(by_issue):
        out.append(f"     {issue}: {by_issue[issue]:.2f} PLN")
    unlabelled = sorted({ln.name for ln in lines if not ln.issue})
    if unlabelled:
        out.append(f"   WARNING unlabelled (no {project.label_key} label): {', '.join(unlabelled)}")
    unpriced = sorted({ln.spec for ln in lines if ln.usd is None})
    if unpriced:
        out.append(f"   WARNING no pinned price for: {', '.join(unpriced)} (cost counted as 0)")
    if running is None:
        out.append("   running now: UNKNOWN (instance list not readable)")
        return "\n".join(out)
    out.append("   running now:" + ("" if running else " nothing"))
    for inst in running:
        when, action = auto_stop(inst)
        stop = f"auto-{action} at {fmt_t(when)}" if when else "NO AUTOMATIC STOP"
        out.append(f"     {inst['name']}  {basename(inst['zone'])}  "
                   f"{basename(inst['machineType'])}  {stop}")
    return "\n".join(out)


# ---------------------------------------------------------------- gcloud (read-only)

class GcloudError(Exception):
    pass


def gcloud_json(config: str, *args: str):
    env = dict(os.environ, CLOUDSDK_ACTIVE_CONFIG_NAME=config)
    try:
        res = subprocess.run(["gcloud", *args, "--format=json", "--quiet"], env=env,
                             capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GcloudError(f"gcloud {args[0]} {args[1]}: {exc}") from exc
    if res.returncode != 0:
        msg = [ln for ln in res.stderr.strip().splitlines() if ln.strip()]
        raise GcloudError(f"gcloud {' '.join(args[:3])}: {msg[0] if msg else 'failed'}")
    try:
        return json.loads(res.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GcloudError(f"gcloud {' '.join(args[:3])}: output is not JSON") from exc


def audit_filter(project_id: str) -> str:
    logs = " OR ".join(f'"projects/{project_id}/logs/cloudaudit.googleapis.com%2F{n}"'
                       for n in ("activity", "system_event"))
    return (f'timestamp>="{SINCE.strftime("%Y-%m-%dT%H:%M:%SZ")}" AND logName=({logs}) '
            'AND protoPayload.serviceName="compute.googleapis.com" '
            r'AND protoPayload.methodName=~"\.(instances\.(insert|delete|start|stop|suspend|'
            r'resume|preempted|guestTerminate)|disks\.(insert|delete))$"')


def report_project(project: Project, now: datetime) -> tuple[str, float]:
    errors = []
    c, p = project.gcloud_config, project.id
    queries = {"instances": ("compute", "instances", "list", f"--project={p}"),
               "disks": ("compute", "disks", "list", f"--project={p}"),
               "audit log": ("logging", "read", audit_filter(p), f"--project={p}",
                             f"--limit={LOG_LIMIT}")}
    got, failed = {}, set()
    for target, args in queries.items():
        try:
            got[target] = gcloud_json(c, *args) or []
        except GcloudError as exc:
            got[target] = []
            failed.add(target)
            errors.append(f"cannot read {target} with gcloud config '{c}': {exc}")
    if len(got["audit log"]) >= LOG_LIMIT:
        errors.append(f"audit log hit the {LOG_LIMIT}-entry limit: the oldest events are missing "
                      "and the total is too LOW; raise LOG_LIMIT")
    instances = got["instances"]
    lines = build_lines(project, parse_events(got["audit log"]), instances, got["disks"], now)
    running = None if "instances" in failed else [
        i for i in instances if i.get("status") in RUNNING_STATES
        and project.is_ours(i["name"], labels_dict(i.get("labels")))]
    total_pln = sum(ln.usd or 0 for ln in lines) * USD_TO_PLN
    return render_project(project, lines, running, now, errors), total_pln


def billing_backstop() -> str:
    out = ["== billing backstop (follow-the-fake billing account)"]
    try:
        info = gcloud_json("follow-the-fake", "billing", "projects", "describe", "follow-the-fake")
        out.append(f"   billing enabled: {info.get('billingEnabled')}")
        account = basename(info.get("billingAccountName"))
        try:
            budgets = gcloud_json("follow-the-fake", "billing", "budgets", "list",
                                  f"--billing-account={account}") or []
            out.append(f"   GCP budgets on the account: {len(budgets)}"
                       + ("" if budgets else " (NO BUDGET ALERT EXISTS)"))
        except GcloudError:
            out.append("   GCP budgets: not readable (Budget API disabled or no permission); "
                       "no budget alert is known to exist")
    except GcloudError as exc:
        out.append(f"   ! {exc}")
    out.append("   Billing data lags by hours and no BigQuery billing export is set up, so the "
               "billing console is only a backstop; the estimate above is the primary check.")
    return "\n".join(out)


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    print(f"Cloud spend since {fmt_t(SINCE)}, as of {fmt_t(now)}")
    print(f"ESTIMATE from GCP resource state + audit logs and pinned on-demand list prices "
          f"(scripts/spend.py); USD->PLN fixed at {USD_TO_PLN:.2f}. Excludes storage buckets, "
          f"network egress and taxes.\n")
    for project in PROJECTS:
        text, _ = report_project(project, now)
        print(text + "\n")
    print(billing_backstop())
    return 0


if __name__ == "__main__":
    sys.exit(main())
