"""Run each page visit in its own process tree with a wall-clock kill.

A hostile page can wedge the renderer so that Playwright calls never return, and Playwright
starts Chromium in its own session, so killing one process group is not enough. Each visit
therefore runs as:

    caller ── supervisor (python -I -m ftf.fetch.runner, new session, child subreaper)
                 └── worker (python -I -m ftf.fetch.runner --worker: Playwright, Chromium)

The supervisor is plain Python that never touches page content. It waits for the worker up to
the time limit, then freezes (SIGSTOP) and kills (SIGKILL) everything below itself. Because it is a
subreaper, anything orphaned during the visit is reparented to it, so it kills and reaps exactly
this visit's processes and nothing else. The caller is left unchanged (it does not become a
subreaper) and only keeps a backstop timeout on the supervisor. The caller never imports Playwright.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from ftf.fetch import capture as cap
from ftf.fetch import classify as cl
from ftf.fetch.browser import FetchConfig

_ERROR_PREFIX = "ftf-fetch-error: "
_PR_SET_CHILD_SUBREAPER = 36
_BACKSTOP_S = 20  # extra time the caller gives the supervisor before killing it


def fetch_isolated(url: str, out_root: Path, config: FetchConfig | None = None) -> dict:
    config = config or FetchConfig()
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    capture_id = cap.new_capture_id(url, started)
    job = json.dumps({"url": url, "out": str(out_root), "capture_id": capture_id, "config": asdict(config)})
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "ftf.fetch.runner"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    killed, err, status = False, "", {}
    try:
        out, err = proc.communicate(job, timeout=config.total_timeout_s + _BACKSTOP_S)
        try:
            status = json.loads((out or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            status = {}
        killed = bool(status.get("killed"))
    except subprocess.TimeoutExpired:
        # The supervisor itself is stuck (should not happen): kill its tree and group, stop waiting.
        killed = True
        victims = [proc.pid, *_descendants(proc.pid)]
        _signal_all(victims, signal.SIGSTOP)
        _signal_all(victims, signal.SIGKILL)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            err = ""

    cdir = out_root / capture_id
    manifest_path = cdir / cap.MANIFEST
    if manifest_path.exists() and not killed:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    cdir.mkdir(parents=True, exist_ok=True)
    # A visit killed mid-way can leave a half-written HAR or manifest: never keep them.
    for name in (cap.MANIFEST, cap.HAR, cap.HTML, cap.SCREENSHOT):
        p = cdir / name
        if p.exists() and (killed or name == cap.MANIFEST):
            p.unlink()
    rec = cap.Recorder(url=url, capture_dir=cdir, capture_id=capture_id, started=started, config=config)
    if killed:
        return rec.finish(cl.TIMEOUT, f"visit killed after {config.total_timeout_s:.0f}s wall-clock limit")
    reason = status.get("error") or ""
    if not reason:
        lines = (err or "").strip().splitlines()
        marked = [ln for ln in lines if ln.startswith(_ERROR_PREFIX)]
        reason = marked[-1][len(_ERROR_PREFIX):] if marked else (lines[-1] if lines else f"exit code {proc.returncode}")
    return rec.finish(cl.FETCHER_ERROR, reason.strip()[:300])


# -- supervisor (one per visit) -------------------------------------------------------------


def _parents() -> dict[int, int]:
    """pid -> ppid for every visible process (Linux /proc); empty elsewhere."""
    out: dict[int, int] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                stat = f.read().decode("latin-1")
            out[int(name)] = int(stat.rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return out


def _descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid in _parents().items():
        children.setdefault(ppid, []).append(pid)
    found, stack = [], [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in found:
                found.append(c)
                stack.append(c)
    return found


def _signal_all(pids, sig) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_everything_below_me(worker: subprocess.Popen) -> None:
    """Freeze, then kill, every descendant of this (subreaper) process, then reap them."""
    me = os.getpid()
    seen: list[int] = []
    for _ in range(5):  # catch processes spawned while we were walking
        new = [p for p in _descendants(me) if p not in seen]
        if not new:
            break
        _signal_all(new, signal.SIGSTOP)
        seen += new
    _signal_all(seen, signal.SIGKILL)
    worker.poll()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return  # no children left
        if pid == 0:
            time.sleep(0.05)


def _supervise() -> int:
    job_text = sys.stdin.read()
    job = json.loads(job_text)
    total = float(job["config"].get("total_timeout_s", 75))
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        except (OSError, AttributeError):
            pass
    # Per-visit temp dir: the browser profile, Playwright's artefacts and the worker's own temp
    # files go here, and it is removed even when the visit had to be killed.
    tmp = tempfile.mkdtemp(prefix="ftf-visit-")
    env = {**os.environ, "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp}
    try:
        worker = subprocess.Popen(
            [sys.executable, "-I", "-m", "ftf.fetch.runner", "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env,
        )
        killed = False
        try:
            _, err = worker.communicate(job_text, timeout=total)
        except subprocess.TimeoutExpired:
            killed = True
            err = ""
        returncode = worker.returncode
        _kill_everything_below_me(worker)  # leftovers (e.g. a Chromium helper) never outlive the visit
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lines = (err or "").strip().splitlines()
    marked = [ln for ln in lines if ln.startswith(_ERROR_PREFIX)]
    if killed or returncode == 0:
        error = ""
    elif marked:
        error = marked[-1][len(_ERROR_PREFIX):]
    else:
        error = lines[-1] if lines else f"exit code {returncode}"
    if lines and error:
        sys.stderr.write("\n".join(lines[-20:]) + "\n")
    print(json.dumps({"killed": killed, "error": error}), flush=True)
    return 0


def _worker() -> int:
    from ftf.fetch.browser import FetcherRefused, fetch_url

    job = json.loads(sys.stdin.read())
    try:
        fetch_url(job["url"], Path(job["out"]), FetchConfig(**job["config"]), capture_id=job["capture_id"])
    except FetcherRefused as e:
        print(f"{_ERROR_PREFIX}refused: {e}", file=sys.stderr, flush=True)
        return 3
    except Exception as e:  # noqa: BLE001 - reported to the caller, which writes the capture
        import traceback

        traceback.print_exc()
        print(f"{_ERROR_PREFIX}{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker() if "--worker" in sys.argv[1:] else _supervise())
