"""Download creative images of new ads (opt-in, sandbox only).

Creatives are hostile data: only HTTPS URLs on the ad platforms' own image CDNs are fetched,
only `image/*` responses up to 5 MB are kept, and the bytes are stored next to the capture with their SHA-256 and
never opened, decoded or rendered here. Signed CDN links expire within days, so they are
fetched in the same run that finds the ad.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from ftf.contract import Artefact, ArtefactType
from ftf.scanner.http import Client, FetchError

ALLOWED_HOSTS = ("fbcdn.net", "tpc.googlesyndication.com", "googleusercontent.com", "ytimg.com", "ggpht.com")
MAX_BYTES = 5 * 1024 * 1024
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def download(urls: list[str], out_root: Path, folder: str, capture_id: str, client: Client, limit: int = 4) -> tuple[list[Artefact], list[str]]:
    """Fetch up to `limit` creatives into `<out_root>/<folder>/creative-<n>.<ext>`.

    Returns artefacts (path relative to `out_root`, as the URL fetcher does) and errors.
    """
    arts: list[Artefact] = []
    errors: list[str] = []
    seen_hashes: set[str] = set()
    for url in [u for u in urls if allowed(u)][:limit]:
        try:
            resp = client.request(url, headers={"Accept": "image/*"}, max_bytes=MAX_BYTES)
        except FetchError as exc:
            errors.append(str(exc)[:200])
            continue
        ctype = resp.content_type.split(";", 1)[0].strip().lower()
        if not ctype.startswith("image/") or not allowed(resp.url):
            errors.append(f"skipped {ctype or 'unknown'} from {urlsplit(resp.url).hostname}")
            continue
        sha = hashlib.sha256(resp.body).hexdigest()
        if sha in seen_hashes:
            continue
        seen_hashes.add(sha)
        n = len(arts) + 1
        rel = Path(folder) / f"creative-{n}.{_EXT.get(ctype, 'bin')}"
        path = out_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.body)
        arts.append(
            Artefact(id=f"{capture_id}:creative-{n}", type=ArtefactType.asset, path=str(rel), sha256=sha, size=len(resp.body))
        )
    return arts, errors
