"""Reference set of frequently impersonated public figures: manifest, build, matching.

The manifest (``reference_manifest.json``, in git) lists for every person the Wikimedia Commons
images used, with source page, author and licence. Images and embeddings are not in git: they
are built from the manifest on first use (``build``), which downloads the listed thumbnails from
Wikimedia and embeds the largest face with SFace. ``FTF_FACES_REFERENCE=<dir>`` points at a
prebuilt copy instead (ours lives in ``gs://follow-the-fake-data/outputs/visual/hack-15/reference/``).

Only public-domain, CC0 and CC BY images are allowed (no share-alike, no non-commercial).

``python -m ftf.features.faces.reference select ...`` drafts a manifest from Wikidata/Commons;
``... build`` builds the embeddings. Neither ever contacts a scam URL.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from ftf.features.common import model_dir

MANIFEST_PATH = Path(__file__).with_name("reference_manifest.json")
USER_AGENT = "ftf-features/0.1 (research prototype; reference-set builder)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PACE_S = 1.0
MAX_CANDIDATES = 40  # licensed files tried per person (P18 first)
THUMB_WIDTH = 500  # a standard Wikimedia thumbnail step (non-standard widths are throttled)


def licence_ok(short_name: str) -> bool:
    """Public domain, CC0 or CC BY (any version); never BY-SA, NC or ND."""
    s = short_name.strip().lower()
    if not s:
        return False
    if s in {"public domain", "cc0", "pd"} or s.startswith(("pd-", "pd ", "cc0 ", "cc-zero")):
        return True
    # "CC BY 4.0", "CC BY 3.0 pl" (a port); anything after "by" other than a version is SA/NC/ND
    return re.fullmatch(r"cc[ -]by(?:[ -]\d(?:\.\d)?(?:[ -][a-z]{2})?)?", s) is not None


# ---------------------------------------------------------------- matching


@dataclass
class Reference:
    names: list[str]  # one per row
    titles: list[str]  # Commons file per row
    matrix: np.ndarray  # rows L2-normalised SFace embeddings
    manifest_sha: str

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


def manifest_sha(path: Path = MANIFEST_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _save(ref: Reference, d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / "embeddings.npy", ref.matrix)
    (d / "rows.json").write_text(
        json.dumps(
            {"manifest_sha": ref.manifest_sha, "names": ref.names, "titles": ref.titles},
            ensure_ascii=False,
            indent=1,
        )
    )


def _load(d: Path) -> Reference:
    meta = json.loads((d / "rows.json").read_text())
    mat = np.load(d / "embeddings.npy")
    if mat.shape[0] != len(meta["names"]):
        raise ValueError(f"reference {d}: {mat.shape[0]} rows but {len(meta['names'])} names")
    return Reference(meta["names"], meta["titles"], mat.astype(np.float32), meta["manifest_sha"])


def _get(url: str, params: Optional[dict] = None, timeout: int = 60) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tries = 7
    for attempt in range(tries):
        time.sleep(PACE_S)  # Wikimedia rate-limits bursts; stay well under it
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            retry_after = e.headers.get("Retry-After", "")
            wait = int(retry_after) if retry_after.isdigit() else 5 * 2**attempt
            time.sleep(min(wait, 120))
    raise AssertionError("unreachable")


def build(dest: Path, manifest: Path = MANIFEST_PATH, keep_images: bool = False) -> Reference:
    """Download the manifest's images and embed the largest face of each into ``dest``."""
    from ftf.features.faces.models import FaceEngine, load_bgr

    doc = json.loads(manifest.read_text())
    engine = FaceEngine()
    img_dir = dest / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    names, titles, rows = [], [], []
    for person in doc["people"]:
        for im in person["images"]:
            if not licence_ok(im["licence"]):
                raise ValueError(f"{im['title']}: licence {im['licence']!r} is not allowed")
            suffix = Path(urllib.parse.urlparse(im["thumb_url"]).path).suffix.lower() or ".jpg"
            p = img_dir / (hashlib.sha1(im["title"].encode()).hexdigest()[:16] + suffix)
            if not p.exists():
                p.write_bytes(_get(im["thumb_url"]))
            got = hashlib.sha256(p.read_bytes()).hexdigest()
            if im.get("sha256") and got != im["sha256"]:
                print(f"warning: {im['title']}: thumbnail bytes changed upstream", file=sys.stderr)
            faces = engine.faces(load_bgr(p))
            if not faces:
                print(f"warning: {im['title']}: no face found, skipped", file=sys.stderr)
                continue
            face = max(faces, key=lambda f: f.box[2] * f.box[3])
            v = face.embedding / (np.linalg.norm(face.embedding) or 1.0)
            names.append(person["name"])
            titles.append(im["title"])
            rows.append(v)
    ref = Reference(names, titles, np.stack(rows).astype(np.float32), manifest_sha(manifest))
    _save(ref, dest)
    if not keep_images:
        for p in img_dir.iterdir():
            p.unlink()
        img_dir.rmdir()
    return ref


@lru_cache(maxsize=1)
def load_reference() -> Optional[Reference]:
    """The reference set for the current manifest, building it on first use.

    Order: ``FTF_FACES_REFERENCE`` (a prebuilt directory), then the cache in
    ``model_dir()/faces-reference/<manifest sha>``, then a fresh build from Wikimedia unless
    ``FTF_OFFLINE=1``. Returns None if none of that works; faces are then still embedded.
    """
    sha = manifest_sha()
    candidates = []
    if os.environ.get("FTF_FACES_REFERENCE"):
        candidates.append(Path(os.environ["FTF_FACES_REFERENCE"]))
    cache = model_dir() / "faces-reference" / sha
    candidates.append(cache)
    for d in candidates:
        if (d / "rows.json").exists():
            ref = _load(d)
            if ref.manifest_sha != sha:
                print(f"warning: reference {d} was built from another manifest", file=sys.stderr)
            return ref
    if os.environ.get("FTF_OFFLINE") == "1":
        return None
    try:
        return build(cache)
    except Exception as e:  # network down, Wikimedia changed: degrade to embeddings only
        print(f"warning: could not build the face reference set: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- drafting a manifest


def _wikidata_person(name: str) -> Optional[dict]:
    hits = json.loads(
        _get(
            WIKIDATA_API,
            {"action": "wbsearchentities", "search": name, "language": "pl", "type": "item",
             "limit": 5, "format": "json"},
        )
    )["search"]
    ids = [h["id"] for h in hits]
    if not ids:
        return None
    ents = json.loads(
        _get(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(ids), "props":
             "claims|descriptions", "languages": "pl|en", "format": "json"})
    )["entities"]
    for qid in ids:
        claims = ents[qid].get("claims", {})
        p31 = {c["mainsnak"].get("datavalue", {}).get("value", {}).get("id") for c in claims.get("P31", [])}
        if "Q5" not in p31:
            continue

        def vals(p):
            return [c["mainsnak"]["datavalue"]["value"] for c in claims.get(p, []) if "datavalue" in c["mainsnak"]]

        desc = ents[qid].get("descriptions", {})
        return {
            "qid": qid,
            "description": (desc.get("en") or desc.get("pl") or {}).get("value", ""),
            "p18": vals("P18"),
            "commons_category": (vals("P373") or [None])[0],
        }
    return None


def _category_files(cat: str, limit: int = 200) -> list[str]:
    out, cont = [], {}
    while len(out) < limit:
        r = json.loads(
            _get(COMMONS_API, {"action": "query", "list": "categorymembers", "cmtitle": f"Category:{cat}",
                               "cmtype": "file", "cmlimit": 100, "format": "json", **cont})
        )
        out += [m["title"] for m in r["query"]["categorymembers"]]
        if "continue" not in r:
            break
        cont = r["continue"]
    return out[:limit]


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _imageinfo(titles: list[str], width: int) -> list[dict]:
    out = []
    for i in range(0, len(titles), 40):
        r = json.loads(
            _get(COMMONS_API, {"action": "query", "titles": "|".join(titles[i : i + 40]), "prop": "imageinfo",
                               "iiprop": "url|extmetadata|mime|size", "iiurlwidth": width, "format": "json"})
        )
        for page in r["query"].get("pages", {}).values():
            if "imageinfo" not in page:
                continue
            ii = page["imageinfo"][0]
            md = {k: v.get("value", "") for k, v in ii.get("extmetadata", {}).items()}
            out.append(
                {
                    "title": page["title"],
                    "source": ii.get("descriptionurl", ""),
                    "thumb_url": ii.get("thumburl", ""),
                    "mime": ii.get("mime", ""),
                    "width": ii.get("width", 0),
                    "author": _strip_html(md.get("Artist", "")),
                    "credit": _strip_html(md.get("Credit", ""))[:200],
                    "licence": _strip_html(md.get("LicenseShortName", "")),
                    "licence_url": md.get("LicenseUrl", ""),
                    "restrictions": md.get("Restrictions", ""),
                }
            )
    return out


def select(names: list[str], per_person: int, work: Path, min_face: int = 60) -> dict:
    """Draft a manifest: licence-filtered Commons portraits with one clear face each.

    Curation only (what goes into the reference), never used at matching time: keep images
    with exactly one detected face of at least ``min_face`` px, then keep those whose face is
    consistent with the person's Wikidata portrait (P18) or, without one, the medoid.
    """
    from ftf.features.faces.models import FaceEngine, load_bgr

    engine = FaceEngine(det_threshold=0.9)
    work.mkdir(parents=True, exist_ok=True)
    people, skipped = [], []
    for name in names:
        wd = _wikidata_person(name)
        if not wd:
            skipped.append({"name": name, "reason": "no Wikidata human"})
            continue
        titles = [f"File:{t}" for t in wd["p18"]]
        if wd["commons_category"]:
            titles += [t for t in _category_files(wd["commons_category"]) if t not in titles]
        infos = [
            i for i in _imageinfo(titles, THUMB_WIDTH)
            if licence_ok(i["licence"]) and i["mime"] in ("image/jpeg", "image/png") and i["thumb_url"]
        ]
        infos.sort(key=lambda i: i["title"][5:] not in wd["p18"])  # Wikidata portrait first
        cands = []
        for info in infos[:MAX_CANDIDATES]:
            p = work / (hashlib.sha1(info["title"].encode()).hexdigest()[:16] + ".img")
            try:
                if not p.exists():
                    p.write_bytes(_get(info["thumb_url"]))
                faces = engine.faces(load_bgr(p))
            except Exception as e:
                print(f"{info['title']}: {e}", file=sys.stderr)
                continue
            if len(faces) != 1 or faces[0].box[2] < min_face:
                continue
            e = faces[0].embedding / np.linalg.norm(faces[0].embedding)
            info["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
            info["is_p18"] = info["title"][5:] in wd["p18"]
            cands.append((info, e))
        if not cands:
            skipped.append({"name": name, "qid": wd["qid"], "reason": f"{len(infos)} licensed files, none with one clear face"})
            continue
        mat = np.stack([e for _, e in cands])
        anchors = [k for k, (i, _) in enumerate(cands) if i["is_p18"]]
        if anchors:
            anchor = mat[anchors].mean(0)
        else:
            anchor = mat[int(np.argmax((mat @ mat.T).sum(1)))]
        anchor /= np.linalg.norm(anchor)
        sims = mat @ anchor
        order = [k for k in np.argsort(-sims) if sims[k] >= 0.4][:per_person]
        imgs = []
        for k in order:
            info = dict(cands[k][0])
            info["consistency_cosine"] = round(float(sims[k]), 3)
            info.pop("mime", None)
            info.pop("width", None)
            info.pop("is_p18", None)
            imgs.append(info)
        if imgs:
            people.append({"name": name, "wikidata": wd["qid"], "description": wd["description"], "images": imgs})
        else:
            skipped.append({"name": name, "qid": wd["qid"], "reason": "no consistent face"})
        print(f"{name}: {wd['qid']} {wd['description']!r}: {len(infos)} licensed, {len(cands)} one-face, {len(imgs)} kept", file=sys.stderr)
    return {
        "about": "Reference faces of public figures impersonated in scam ads. Images are not "
                 "redistributed; they are downloaded from Wikimedia Commons at build time. "
                 "Allowed licences: public domain, CC0, CC BY.",
        "people": people,
        "skipped": skipped,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ftf.features.faces.reference")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select", help="draft a manifest from Wikidata/Commons")
    s.add_argument("--names", nargs="+", required=True)
    s.add_argument("--per-person", type=int, default=4)
    s.add_argument("--work", type=Path, required=True, help="scratch dir for downloaded thumbnails")
    s.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("build", help="download the manifest's images and embed them")
    b.add_argument("--dest", type=Path, default=None)
    b.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    b.add_argument("--keep-images", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "select":
        doc = select(a.names, a.per_person, a.work)
        a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    else:
        dest = a.dest or model_dir() / "faces-reference" / manifest_sha(a.manifest)
        ref = build(dest, a.manifest, a.keep_images)
        print(f"{dest}: {len(ref.names)} images of {len(ref.people)} people")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
