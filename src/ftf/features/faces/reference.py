"""Reference set of public figures that scam ads impersonate.

Built once, on a VM, from a names file kept outside git; loaded at run time from a directory:

* ``python -m ftf.features.faces.reference build --names NAMES --out DIR`` reads one name per line
  (``#`` starts a comment; ``Name=Qid`` pins an ambiguous name). For each name it finds the Wikidata
  human, collects that person's portraits (Wikidata P18 and the Commons category), keeps the JPEG
  and PNG files that show exactly one clear face (YuNet, score >= 0.9, at least 60 px) and keeps up
  to five that agree with the person's portrait. It writes ``DIR/manifest.json`` (name, source URL,
  file, licence per image), ``DIR/images/`` and ``DIR/embeddings.npy`` (SFace, one row per image),
  plus ``DIR/build-report.json``. Only Wikidata and Wikimedia Commons are contacted.
* ``load_reference()`` reads ``FTF_FACE_REFERENCE`` (default ``model_dir()/face-reference``). With no
  manifest there, faces are still embedded but no ``person_face`` is emitted (one stderr note).

Nothing is downloaded at scan time, and no reference image is ever cropped from an organiser
creative.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from ftf.features.common import model_dir
from ftf.features.faces.models import EMBEDDING_MODEL

MANIFEST = "manifest.json"
EMBEDDINGS = "embeddings.npy"
FORMAT = 1
DET_THRESHOLD = 0.9  # a face for the reference is a confident detection
MIN_FACE_PX = 60
ANCHOR_COS = 0.4  # kept images must agree with the person's portrait (or medoid) at least this much

USER_AGENT = "ftf-features/0.1 (research prototype; reference-set builder)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PACE_S = 1.0  # Wikimedia rate-limits bursts; stay well under it
MAX_CANDIDATES = 40  # files tried per person
THUMB_WIDTH = 500  # standard Wikimedia thumbnail step (other widths are throttled)


def reference_dir() -> Path:
    """``FTF_FACE_REFERENCE``, else the old ``FTF_FACES_REFERENCE`` (one release, with a note), else
    ``model_dir()/face-reference``."""
    if os.environ.get("FTF_FACE_REFERENCE"):
        return Path(os.environ["FTF_FACE_REFERENCE"])
    if os.environ.get("FTF_FACES_REFERENCE"):
        print("warning: FTF_FACES_REFERENCE is deprecated; use FTF_FACE_REFERENCE", file=sys.stderr)
        return Path(os.environ["FTF_FACES_REFERENCE"])
    return model_dir() / "face-reference"


@dataclass
class Reference:
    names: list[str]  # one per row: the person
    sources: list[str]  # one per row: source URL of the image
    matrix: np.ndarray  # rows L2-normalised SFace embeddings
    manifest_sha: str

    def __post_init__(self) -> None:
        self.matrix = np.asarray(self.matrix, dtype=np.float32)
        norms = np.linalg.norm(self.matrix, axis=1, keepdims=True)
        self.matrix = self.matrix / np.where(norms == 0, 1.0, norms)

    @property
    def people(self) -> list[str]:
        return sorted(set(self.names))

    def match(self, embedding: np.ndarray, k: int = 3) -> list[tuple[str, float]]:
        """Best people for one face: per person the highest cosine over their images."""
        v = np.asarray(embedding, dtype=np.float32).ravel()
        n = float(np.linalg.norm(v))
        if n == 0 or not self.names:
            return []
        sims = self.matrix @ (v / n)
        best: dict[str, float] = {}
        for name, s in zip(self.names, sims.tolist()):
            if s > best.get(name, -2.0):
                best[name] = s
        return sorted(best.items(), key=lambda t: -t[1])[:k]


def _embed_rows(d: Path, rows: list[dict]) -> tuple[list[dict], np.ndarray]:
    """Embeddings for manifest rows that lack ``embeddings.npy``; rows without one clear face drop."""
    from ftf.features.faces.models import FaceEngine

    engine = FaceEngine(det_threshold=DET_THRESHOLD)
    keep, vecs = [], []
    for r in rows:
        vec = _single_face(engine, d / r["file"])
        if vec is None:
            print(f"faces: reference image {r['file']} has no single clear face; skipped", file=sys.stderr)
            continue
        keep.append(r)
        vecs.append(vec)
    return keep, np.stack(vecs) if vecs else np.zeros((0, 128), np.float32)


@lru_cache(maxsize=1)
def load_reference() -> Optional[Reference]:
    """The reference set from ``reference_dir()``, or None (faces are then embedded only)."""
    d = reference_dir()
    mpath = d / MANIFEST
    if not mpath.exists():
        print(f"ftf features: faces: no reference set (manifest.json) in {d}; "
              "no person_face indicators", file=sys.stderr)
        return None
    try:
        raw = mpath.read_bytes()
        doc = json.loads(raw)
        if doc.get("embedding_model") not in (None, EMBEDDING_MODEL):
            raise ValueError(f"built with {doc.get('embedding_model')}, not {EMBEDDING_MODEL}")
        rows = doc["images"]
        sha = hashlib.sha256(raw).hexdigest()[:12]
        emb = d / EMBEDDINGS
        if emb.exists():
            mat = np.load(emb).astype(np.float32)
            if mat.ndim != 2 or mat.shape[0] != len(rows):
                raise ValueError(f"embeddings {mat.shape} for {len(rows)} images")
        else:
            rows, mat = _embed_rows(d, rows)
        if not rows:
            raise ValueError("no usable images")
    except Exception as e:  # a broken set must degrade the faces stage, never stop the run
        print(f"warning: face reference set in {d} is unusable ({type(e).__name__}: {e}); "
              "no person_face indicators", file=sys.stderr)
        return None
    return Reference([r["name"] for r in rows], [r["source"] for r in rows], mat, sha)


# ---------------------------------------------------------------- builder (run on a VM)


def _get(url: str, params: Optional[dict] = None, timeout: int = 60) -> bytes:
    # Commons serves thumbnails from thumb.wikimedia.org; the upload.wikimedia.org thumb path is
    # heavily throttled for scripts, so use the same path on the thumbnail host.
    url = url.replace("https://upload.wikimedia.org/wikipedia/commons/thumb/",
                      "https://thumb.wikimedia.org/wikipedia/commons/thumb/")
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tries = 4
    for attempt in range(tries):
        time.sleep(PACE_S)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            if "upload.wikimedia.org" in url and attempt >= 1:
                raise  # originals throttled for long periods: skip the file, do not wait minutes
            retry_after = e.headers.get("Retry-After", "")
            wait = int(retry_after) if retry_after.isdigit() else 5 * 2**attempt
            print(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}; waiting {min(wait, 120)} s",
                  file=sys.stderr)
            time.sleep(min(wait, 120))
    raise AssertionError("unreachable")


def _wikidata_person(name: str, qid: Optional[str] = None) -> Optional[dict]:
    """The Wikidata human for a name (first human among the search hits), or the pinned ``qid``."""
    if qid:
        ids = [qid]
    else:
        hits = json.loads(_get(WIKIDATA_API, {"action": "wbsearchentities", "search": name, "language": "pl",
                                              "type": "item", "limit": 5, "format": "json"}))["search"]
        ids = [h["id"] for h in hits]
    if not ids:
        return None
    ents = json.loads(_get(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(ids),
                                          "props": "claims|descriptions", "languages": "pl|en",
                                          "format": "json"}))["entities"]
    for wid in ids:
        claims = ents.get(wid, {}).get("claims", {})
        p31 = {c["mainsnak"].get("datavalue", {}).get("value", {}).get("id") for c in claims.get("P31", [])}
        if "Q5" not in p31:
            continue

        def vals(p: str) -> list:
            return [c["mainsnak"]["datavalue"]["value"] for c in claims.get(p, []) if "datavalue" in c["mainsnak"]]

        return {"qid": wid, "p18": vals("P18"), "commons_category": (vals("P373") or [None])[0]}
    return None


def _category_files(cat: str, limit: int = 200) -> list[str]:
    out: list[str] = []
    cont: dict = {}
    while len(out) < limit:
        r = json.loads(_get(COMMONS_API, {"action": "query", "list": "categorymembers", "cmtitle": f"Category:{cat}",
                                          "cmtype": "file", "cmlimit": 100, "format": "json", **cont}))
        out += [m["title"] for m in r["query"]["categorymembers"]]
        if "continue" not in r:
            break
        cont = r["continue"]
    return out[:limit]


def _imageinfo(titles: list[str], width: int) -> list[dict]:
    out = []
    for i in range(0, len(titles), 40):
        r = json.loads(_get(COMMONS_API, {"action": "query", "titles": "|".join(titles[i : i + 40]),
                                          "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
                                          "iiurlwidth": width, "format": "json"}))
        for page in r["query"].get("pages", {}).values():
            if "imageinfo" not in page:
                continue
            ii = page["imageinfo"][0]
            md = {k: v.get("value", "") for k, v in ii.get("extmetadata", {}).items()}
            out.append({
                "title": page["title"],
                "source": ii.get("descriptionurl", ""),
                "thumb_url": ii.get("thumburl", ""),
                "mime": ii.get("mime", ""),
                "licence": md.get("LicenseShortName", ""),
            })
    return out


def _single_face(engine, path: Path) -> Optional[np.ndarray]:
    """SFace embedding of the image's only clear face, or None when there is not exactly one."""
    from ftf.features.faces.models import load_bgr

    faces = [f for f in engine.faces(load_bgr(path)) if min(f.box[2], f.box[3]) >= MIN_FACE_PX]
    return np.asarray(faces[0].embedding, np.float32) if len(faces) == 1 else None


def read_names(path: Path) -> list[tuple[str, Optional[str]]]:
    """``[(name, qid or None)]`` from a names file: one per line, ``#`` comments, ``Name=Qid``."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, qid = line.partition("=")
        out.append((name.strip(), qid.strip() or None))
    return out


def build(names: list[tuple[str, Optional[str]]], out: Path, per_person: int = 5,
          keep_work: bool = False) -> dict:
    """Write a reference set into ``out`` (replacing its images, embeddings and manifest)."""
    from ftf.features.faces.models import FaceEngine

    engine = FaceEngine(det_threshold=DET_THRESHOLD)
    work, images = out / "work", out / "images"
    shutil.rmtree(images, ignore_errors=True)  # a rebuild replaces the set; the thumbnail cache is kept
    images.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    vecs: list[np.ndarray] = []
    report: list[dict] = []
    for name, qid in names:
        try:
            wd = _wikidata_person(name, qid)
        except Exception as e:  # one failed lookup must not stop the other names
            report.append({"name": name, "images": 0, "reason": f"lookup failed: {type(e).__name__}"})
            print(f"not found: {name} (lookup failed: {type(e).__name__})", file=sys.stderr)
            continue
        if not wd:
            report.append({"name": name, "images": 0, "reason": "no Wikidata human"})
            continue
        portraits = {f"File:{t}" for t in wd["p18"]}
        titles = sorted(portraits)
        if wd["commons_category"]:
            titles += [t for t in _category_files(wd["commons_category"]) if t not in portraits]
        infos = [i for i in _imageinfo(titles, THUMB_WIDTH)
                 if i["mime"] in ("image/jpeg", "image/png") and i["thumb_url"]]
        infos.sort(key=lambda i: i["title"] not in portraits)  # Wikidata portrait first
        cands = []  # (is portrait, info, local file, embedding)
        work.mkdir(parents=True, exist_ok=True)
        for info in infos[:MAX_CANDIDATES]:
            p = work / (hashlib.sha1(info["title"].encode()).hexdigest()[:16] + ".img")
            try:
                if not p.exists():
                    p.write_bytes(_get(info["thumb_url"]))
                vec = _single_face(engine, p)
            except Exception as e:  # a bad file is skipped, not fatal
                print(f"{name}: {info['title']}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            if vec is not None:
                cands.append((info["title"] in portraits, info, p, vec))
        if not cands:
            report.append({"name": name, "images": 0, "candidates": len(infos), "reason": "no single-face portrait"})
            print(f"not found: {name}", file=sys.stderr)
            continue
        m = np.stack([c[3] for c in cands])
        m = m / np.linalg.norm(m, axis=1, keepdims=True)
        anchors = [i for i, c in enumerate(cands) if c[0]] or [int(np.argmax((m @ m.T).sum(1)))]
        anchor = m[anchors].mean(0)
        sims = m @ (anchor / np.linalg.norm(anchor))
        kept = [int(i) for i in np.argsort(-sims)[:per_person] if sims[i] >= ANCHOR_COS]
        for i in kept:
            _, info, p, vec = cands[i]
            ext = Path(urllib.parse.urlparse(info["thumb_url"]).path).suffix.lower() or ".jpg"
            dest = images / (hashlib.sha1(info["title"].encode()).hexdigest()[:16] + ext)
            shutil.copyfile(p, dest)
            rows.append({
                "name": name,
                "source": info["source"],
                "file": f"images/{dest.name}",
                "licence": info["licence"],
                "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            })
            vecs.append(vec)
        report.append({"name": name, "wikidata": wd["qid"], "candidates": len(cands), "images": len(kept)})
        if not kept:
            print(f"not found: {name} (no portrait agrees with the rest)", file=sys.stderr)
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    if not rows:
        raise SystemExit("no reference portraits found; nothing written")
    np.save(out / EMBEDDINGS, np.stack(vecs).astype(np.float32))
    doc = {
        "format": FORMAT,
        "built": date.today().isoformat(),
        "embedding_model": EMBEDDING_MODEL,
        "det_threshold": DET_THRESHOLD,
        "min_face_px": MIN_FACE_PX,
        "embeddings": EMBEDDINGS,
        "images": rows,
    }
    (out / MANIFEST).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "build-report.json").write_text(json.dumps({"people": report}, ensure_ascii=False, indent=1) + "\n",
                                           encoding="utf-8")
    return doc


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ftf.features.faces.reference")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a reference set from a names file (network: Wikidata, Commons)")
    b.add_argument("--names", type=Path, required=True, help="names file, kept outside git")
    b.add_argument("--out", type=Path, required=True, help="directory to write (manifest, images, embeddings)")
    b.add_argument("--per-person", type=int, default=5)
    b.add_argument("--keep-work", action="store_true", help="keep the downloaded thumbnails in OUT/work")
    a = ap.parse_args(argv)
    doc = build(read_names(a.names), a.out, a.per_person, a.keep_work)
    people = {r["name"] for r in doc["images"]}
    print(f"{a.out}: {len(doc['images'])} images of {len(people)} people", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
