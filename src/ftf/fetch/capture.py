"""Capture folder layout and the contract records written for one page visit.

A capture folder looks like an organiser case folder, so Ingestion reads both the same way:

    <out>/<capture_id>/
        url.txt          requested URL, then every main-frame URL visited, one per line
        page.har         network traffic (HAR 1.2, bodies embedded)
        page.html        final DOM as HTML
        screenshot.png   full-page screenshot (height capped)
        capture.json     manifest: fetch metadata plus the contract `Incident`

Artefact paths are relative to `<out>`, as organiser paths are relative to the raw data root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ftf import __version__
from ftf import egress as eg
from ftf.contract import (
    CONTRACT_VERSION,
    Artefact,
    ArtefactType,
    Capture,
    Dataset,
    Incident,
    Indicator,
    Layer,
    Level,
    MissingModality,
    Observed,
    Provenance,
    Stage,
)
from ftf.fetch import classify as cl

if TYPE_CHECKING:
    from ftf.fetch.guard import GuardProxy

URL_TXT = "url.txt"
HAR = "page.har"
HTML = "page.html"
SCREENSHOT = "screenshot.png"
MANIFEST = "capture.json"
MANIFEST_VERSION = 1
SOURCE = "live_fetch"
EXTRACTOR = f"ftf-fetch@{__version__}"


def _slug(host: str) -> str:
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    s = re.sub(r"[^a-z0-9.-]+", "_", host.lower()).strip("._-")
    return s[:60] or "nohost"


def new_capture_id(url: str, when: float) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    stamp = datetime.fromtimestamp(when, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(f"{url}\n{when}".encode()).hexdigest()[:8]
    return f"{stamp}-{_slug(host)}-{digest}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Recorder:
    """Collects what happens during one visit and writes the capture folder."""

    def __init__(self, *, url: str, capture_dir: Path, capture_id: str, started: float, config: Any) -> None:
        self.url = url
        self.dir = capture_dir
        self.capture_id = capture_id
        self.started = started
        self.config = config
        self.final_url: str | None = None
        self.last_nav_url: str | None = None
        self.chain: list[dict] = []
        self.blocked_requests: list[dict] = []
        self.popups: list[str] = []
        self.notes: list[str] = []
        self.user_agent: str | None = None
        self.viewport_width: int = config.viewport_width
        self.profile: dict = {"name": getattr(config, "profile", "default")}
        self.browser_version: str | None = None
        self.html_written = False
        self.html_truncated = False
        self.page_text: str | None = None
        self.html_head: str = ""
        self.guard: GuardProxy | None = None
        self.failed_after_load = False
        self.egress: dict | None = eg.current()  # read at the start: rotation happens between visits

    # -- during the visit --------------------------------------------------------------------

    def hop(self, url: str, status: int, location: str | None, guard: str | None) -> None:
        self.chain.append({"url": url, "status": status, "location": location, "guard": guard})

    def blocked(self, url: str, method: str, why: str) -> None:
        if len(self.blocked_requests) < 500:
            self.blocked_requests.append({"url": url[:2000], "method": method, "reason": why})

    def capture_page(self, page: Any, pw_error: type[Exception]) -> None:
        """Save the final DOM and a screenshot. Failures are noted, never raised."""
        html = None
        for _ in range(2):
            try:
                html = page.content()
                break
            except pw_error as e:  # page still navigating
                self.notes.append(f"html: {str(e).splitlines()[0]}")
                try:
                    page.wait_for_timeout(1000)
                except pw_error:
                    break
        if html is not None:
            data = html.encode("utf-8", "replace")
            if len(data) > self.config.max_html_bytes:
                data = data[: self.config.max_html_bytes]
                self.html_truncated = True
            (self.dir / HTML).write_bytes(data)
            self.html_written = True
            self.html_head = data[:2_000_000].decode("utf-8", "replace")
        try:
            self.page_text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 20000) : ''")
        except pw_error:
            self.page_text = None
        try:
            height = int(page.evaluate("() => Math.max(document.documentElement.scrollHeight || 0, document.body ? document.body.scrollHeight : 0)") or 0)
        except (pw_error, TypeError, ValueError):
            height = 0
        try:
            # scale="css": the cap is in CSS pixels, also for profiles with a device scale factor.
            kwargs: dict[str, Any] = {"path": str(self.dir / SCREENSHOT), "full_page": True, "timeout": 15000, "scale": "css"}
            if height > self.config.max_screenshot_height:
                kwargs["clip"] = {"x": 0, "y": 0, "width": self.viewport_width, "height": self.config.max_screenshot_height}
                self.notes.append(f"screenshot cut at {self.config.max_screenshot_height}px of {height}px")
            page.screenshot(**kwargs)
        except pw_error as e:
            self.notes.append(f"screenshot: {str(e).splitlines()[0]}")

    # -- verdict -----------------------------------------------------------------------------

    def _guard_verdict(self) -> cl.Verdict | None:
        """If the guard refused the main document's host, say why."""
        if self.guard is None:
            return None
        target = self.last_nav_url or self.url
        try:
            parts = urlsplit(target)
            host, port = (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return None
        for event in reversed(self.guard.events_for(host, port)):
            outcome = cl.outcome_from_guard(event.outcome)
            if outcome is not None:
                return cl.Verdict(outcome, f"{event.outcome} for {host}:{port}: {event.detail}".strip())
            if event.outcome == "ok":
                return None
        return None

    def finish_from_visit(self, nav_error: str | None) -> dict:
        if self.failed_after_load:
            return self._finish_failed_after_load(nav_error)
        last = self.chain[-1] if self.chain else None
        if last is not None and last.get("guard"):
            verdict = self._guard_verdict() or cl.Verdict(cl.outcome_from_guard(last["guard"]) or cl.BLOCKED_ADDRESS, f"guard: {last['guard']}")
        elif nav_error is not None and (last is None or not self.html_written):
            verdict = self._guard_verdict()
            if verdict is None:
                outcome = cl.TIMEOUT if nav_error.startswith("timeout") else cl.outcome_from_net_error(nav_error)
                verdict = cl.Verdict(outcome, nav_error)
        elif last is None:
            verdict = self._guard_verdict() or cl.Verdict(cl.NAVIGATION_ERROR, nav_error or "no document response")
        else:
            verdict = cl.classify_content(self.url, self.final_url or last["url"], last["status"], self.html_head, self.page_text)
            if nav_error is not None:
                verdict = cl.Verdict(verdict.outcome, f"{verdict.reason}; load did not finish ({nav_error})")
        if self.guard is not None and self.guard.budget_exceeded:
            self.notes.append(f"byte limit {self.guard.max_bytes} reached; later responses were cut")
        return self.finish(verdict.outcome, verdict.reason)

    def _finish_failed_after_load(self, nav_error: str | None) -> dict:
        """A document loaded, then a later main-frame navigation failed or was refused."""
        first = next(h for h in self.chain if not h.get("guard") and not 300 <= h["status"] < 400)
        verdict = self._guard_verdict()
        if verdict is None:
            outcome = cl.outcome_from_net_error(nav_error) if nav_error else cl.NAVIGATION_ERROR
            verdict = cl.Verdict(outcome, nav_error or "Chromium error page")
        where = f"{first['url']} loaded (HTTP {first['status']}), then navigated to {self.last_nav_url}"
        if verdict.outcome == cl.BLOCKED_ADDRESS:
            # Sending a visitor to a private or loopback address is a cloaker's way of hiding.
            return self.finish(cl.DECOY, f"{where}, a refused address ({verdict.reason}); the first page is in the HAR")
        return self.finish(verdict.outcome, f"{where}, which failed ({verdict.reason}); the first page is in the HAR")

    # -- writing -----------------------------------------------------------------------------

    def finish(self, outcome: str, reason: str) -> dict:
        finished = time.time()
        dead = outcome in cl.DEAD
        if dead:
            # Chromium's own error page or a guard reply is not evidence about the site.
            for name in (HTML, SCREENSHOT):
                p = self.dir / name
                if p.exists():
                    p.unlink()
            self.html_written = False
        urls = [self.url] + [h["url"] for h in self.chain if not h.get("guard")]
        if self.final_url and self.final_url.startswith(("http://", "https://")):
            urls.append(self.final_url)
        seen: list[str] = []
        for u in urls:
            if u not in seen:
                seen.append(u)
        (self.dir / URL_TXT).write_text("\n".join(seen) + "\n", encoding="utf-8")

        rel = self.dir.name
        artefacts: list[Artefact] = []
        missing: list[MissingModality] = []
        for name, atype in ((URL_TXT, ArtefactType.url), (HAR, ArtefactType.har), (HTML, ArtefactType.html), (SCREENSHOT, ArtefactType.screenshot)):
            p = self.dir / name
            if p.exists() and p.stat().st_size > 0:
                artefacts.append(Artefact(id=f"fetch:{self.capture_id}:{atype.value}", type=atype, path=f"{rel}/{name}",
                                          sha256=_sha256(p), size=p.stat().st_size))
            elif atype is not ArtefactType.url:
                if p.exists():
                    p.unlink()
                missing.append(MissingModality(modality=atype, reason=f"{outcome}: {reason}"[:500]))

        cap_id = f"fetch:{self.capture_id}:c1"
        capture = Capture(
            id=cap_id,
            stage=Stage.decoy if outcome == cl.DECOY else Stage.unknown,
            observed=Observed.live,
            captured_on=datetime.fromtimestamp(self.started, timezone.utc).strftime("%Y-%m-%d"),
            artefacts=artefacts,
            missing=missing,
        )
        indicator = Indicator(
            id=f"fetch:{self.capture_id}:fetch_outcome",
            capture_id=cap_id,
            layer=Layer.collector,  # describes our fetch, never the scam: never links
            kind="fetch_outcome",
            level=Level.capture,
            value=f"{outcome}: {reason}"[:500],
            observed=Observed.live,
            provenance=Provenance(artefact_id=f"fetch:{self.capture_id}:url", note="ftf fetch verdict"),
            extractor=EXTRACTOR,
        )
        indicators = [indicator]
        if self.egress is not None:
            indicators.append(Indicator(
                id=f"fetch:{self.capture_id}:egress",
                capture_id=cap_id,
                layer=Layer.collector,  # our network address: describes the collector, never links
                kind="analyst_ip",
                level=Level.capture,
                value=eg.label(self.egress),
                observed=Observed.live,
                provenance=Provenance(artefact_id=f"fetch:{self.capture_id}:url", note="egress worker (salted IP hash)"),
                extractor=EXTRACTOR,
            ))
        incident = Incident(id=f"fetch:{self.capture_id}", source=SOURCE, source_ref=self.capture_id,
                            captures=[capture], indicators=indicators)
        guard = self.guard
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "contract_version": CONTRACT_VERSION,
            "tool": EXTRACTOR,
            "capture_id": self.capture_id,
            "requested_url": self.url,
            "final_url": self.final_url if self.final_url and self.final_url.startswith(("http://", "https://")) else None,
            "outcome": outcome,
            "category": cl.category(outcome),
            "reason": reason,
            "http_status": next((h["status"] for h in reversed(self.chain)
                                 if not h.get("guard") and not 300 <= h["status"] < 400),
                                self.chain[-1]["status"] if self.chain else None) if not dead else None,
            "started_at": _iso(self.started),
            "finished_at": _iso(finished),
            "duration_s": round(finished - self.started, 2),
            "redirect_chain": self.chain,
            "blocked_requests": self.blocked_requests,
            "popups_closed": self.popups,
            "guard_events": [e.as_dict() for e in guard.events if e.outcome != "ok"][:500] if guard else [],
            "bytes_received": guard.bytes_in if guard else 0,
            "html_truncated": self.html_truncated,
            "notes": self.notes,
            "browser": {"version": self.browser_version, "user_agent": self.user_agent},
            # How the visit presented itself (collector data: describes us, never link on it).
            "profile": self.profile,
            # Which egress worker sent the traffic (region and salted IP hash, never the raw IP);
            # null when fetched from a machine without one (the sandbox, the jury's container).
            "egress": self.egress,
            "limits": {k: v for k, v in asdict(self.config).items() if k not in ("allow_networks", "allow_ports")},
            "incident": json.loads(incident.model_dump_json()),
        }
        tmp = self.dir / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.dir / MANIFEST)
        return manifest


def load_incident(capture_dir: Path) -> Incident:
    data = json.loads((Path(capture_dir) / MANIFEST).read_text(encoding="utf-8"))
    return Incident.model_validate(data["incident"])


def write_dataset(out_root: Path, name: str = "live_fetch") -> Path:
    """Collect every capture under `out_root` into `<out_root>/dataset.json` (a contract Dataset)."""
    out_root = Path(out_root)
    incidents = []
    for m in sorted(out_root.glob(f"*/{MANIFEST}")):
        try:
            incidents.append(load_incident(m.parent))
        except (ValueError, KeyError, OSError):
            continue
    ds = Dataset(name=name, description="Captures made by `ftf fetch`.", incidents=incidents)
    path = out_root / "dataset.json"
    tmp = out_root / "dataset.json.tmp"
    tmp.write_text(ds.to_json(), encoding="utf-8")
    os.replace(tmp, path)
    return path
