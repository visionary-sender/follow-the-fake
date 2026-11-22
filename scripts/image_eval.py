"""evaluation of the image family on the organiser data (run on the VM, never locally).

    python -I scripts/image_eval.py --pairs pairs.json --out /data/derived/visual/eval

1. Labelled pairs: known near-duplicates from the cross-case signal report (positives) against
   random cross-case pairs of page assets and of ad screenshots (negatives). Reports Hamming
   distance distributions per hash, embedding cosine, and ROC AUC; no thresholds.
2. Synthetic robustness: hue rotation, JPEG/WebP re-encoding and resizing of real creatives,
   with the distance change per hash; the hue rows also show a plain BT.601 grey baseline.
3. CPU time per image (hashes, embedding) and the model's download size.

Positives are groups: every image of side A is paired with its closest image of side B (the
report names the groups, not the exact image-to-image mapping), all other pairs inside a group
are left out. Images are read as inert bytes; SVG is never decoded.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import random
import time
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image

from ftf.features.image import embed, har
from ftf.features.image.hashing import CSS_LUMA, SkipImage, hamming, hashes, hue_rotate, load

KINDS = ("image_phash", "image_dhash", "image_pdq")
BT601 = np.array([0.299, 0.587, 0.114], dtype=np.float32)

# The labelled pairs name organiser files, so they live in the bucket, not in git
# (gs://$FTF_BUCKET/outputs/visual/labels/pairs.json):
#   {"positives": [{"name", "a": [side...], "b": [side...]}],
#    "by_name": {"name", "a": glob, "b_dir": glob prefix, "siblings": [file names]}}
# A side is ["file", glob] or ["har", har glob, URL substring, URL suffix]. "by_name" pairs files
# of the same name across two cases; "siblings" are same-template images that show different
# content, reported separately and kept out of both the positives and the negatives.


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def g(raw: Path, pattern: str) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(raw / pattern)))


def case_of(raw: Path, p: Path) -> str:
    parts = nfc(p.relative_to(raw).as_posix()).split("/")
    return f"{parts[0][:3]}/{parts[1]}"


class Item:
    def __init__(self, label: str, case: str, data: bytes):
        self.label, self.case, self.data = label, case, data
        self.sha = hashlib.sha256(data).hexdigest()
        self.img = load(data)
        self.h = hashes(self.img.rgb)
        self.h601 = hashes(self.img.rgb, BT601)
        self.vec = None


def items_for(raw: Path, spec) -> list[Item]:
    out = []
    if spec[0] == "file":
        for p in g(raw, spec[1]):
            out.append(Item(nfc(p.relative_to(raw).as_posix()), case_of(raw, p), p.read_bytes()))
    else:
        _, hglob, needle, suffix = spec
        for hp in g(raw, hglob):
            for i, url, body in har.iter_images(hp):
                if needle in url and url.split("?")[0].endswith(suffix):
                    try:
                        out.append(Item(f"{nfc(hp.relative_to(raw).as_posix())}#{i} {url[-60:]}", case_of(raw, hp), body))
                    except SkipImage:
                        pass
    return out


def dist(a: Item, b: Item, kind: str, variant: str = "css") -> int:
    ha, hb = (a.h, b.h) if variant == "css" else (a.h601, b.h601)
    return hamming(ha[kind], hb[kind])


def cos(a: Item, b: Item):
    return None if a.vec is None or b.vec is None else float(a.vec @ b.vec)


def auc(pos, neg, higher_is_positive: bool) -> float:
    """ROC AUC via the Mann-Whitney statistic (ties count half)."""
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if not higher_is_positive:
        pos, neg = -pos, -neg
    gt = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(gt / (len(pos) * len(neg)))


def describe(v) -> dict:
    v = np.asarray(v, float)
    q = np.percentile(v, [0, 1, 5, 25, 50, 75, 95, 100])
    return {"n": int(len(v)), **{k: round(float(x), 3) for k, x in zip(("min", "p1", "p5", "p25", "median", "p75", "p95", "max"), q)}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", type=Path, default=Path("/data/raw"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--pairs", type=Path, required=True, help="labelled pair spec (JSON)")
    ap.add_argument("--negatives", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=14)
    args = ap.parse_args()
    raw = args.raw_root
    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    emb = embed.get()

    # --- positives
    pos_pairs = []  # (group, a, b)
    siblings = []
    spec = json.loads(args.pairs.read_text())
    for grp in spec["positives"]:
        name, side_a, side_b = grp["name"], grp["a"], grp["b"]
        A = [it for s in side_a for it in items_for(raw, s)]
        B = [it for s in side_b for it in items_for(raw, s)]
        if not A or not B:
            raise SystemExit(f"positive group {name!r} not found: {len(A)} / {len(B)}")
        for a in A:
            b = min(B, key=lambda b: dist(a, b, "image_pdq"))
            pos_pairs.append((name, a, b))
    by_name = spec.get("by_name") or {}
    for pa in g(raw, by_name["a"]) if by_name else []:
        mates = g(raw, by_name["b_dir"] + pa.name)
        if not mates:
            continue
        try:
            a = Item(nfc(pa.relative_to(raw).as_posix()), case_of(raw, pa), pa.read_bytes())
            b = Item(nfc(mates[0].relative_to(raw).as_posix()), case_of(raw, mates[0]), mates[0].read_bytes())
        except SkipImage:
            continue
        if pa.name in by_name.get("siblings", []):
            siblings.append((by_name["name"] + " (sibling)", a, b))
            continue
        pos_pairs.append((by_name["name"], a, b))

    # --- negative pools: page assets and ad screenshots (files in a case's top folders)
    assets, shots = [], []
    for p in g(raw, "*/*/**/*_files/*") + g(raw, "*/*/*_files/*"):
        if p.is_file() and p.suffix.lower() not in {".svg", ".js", ".css", ".html", ".json"}:
            assets.append(p)
    assets = sorted(set(assets))
    for p in g(raw, "*/*/*.png") + g(raw, "*/*/*.PNG") + g(raw, "*/*/*.jpg") + g(raw, "*/*/*/*.png"):
        if "_files" not in p.as_posix():
            shots.append(p)
    pool = {}

    def get(p: Path):
        if p not in pool:
            try:
                pool[p] = Item(nfc(p.relative_to(raw).as_posix()), case_of(raw, p), p.read_bytes())
            except SkipImage:
                pool[p] = None
        return pool[p]

    pos_shas = {it.sha for _, a, b in pos_pairs + siblings for it in (a, b)}
    neg_pairs = []
    for kind_name, src, n in (("asset", assets, args.negatives), ("ad_screenshot", shots, args.negatives // 4)):
        tries = 0
        got = 0
        while got < n and tries < n * 30:
            tries += 1
            a, b = get(rng.choice(src)), get(rng.choice(src))
            if a is None or b is None or a.case == b.case or a.sha == b.sha:
                continue
            if a.sha in pos_shas and b.sha in pos_shas:
                continue
            neg_pairs.append((kind_name, a, b))
            got += 1

    # --- embeddings for every image in a pair
    t_emb = []
    if emb is not None:
        for it in {id(x): x for _, a, b in pos_pairs + neg_pairs + siblings for x in (a, b)}.values():
            t0 = time.process_time()
            it.vec, _ = emb(it.img.rgb)
            t_emb.append(time.process_time() - t0)

    report: dict = {"positives": [], "template_siblings": [], "separation": {}, "negatives_by_pool": {}}
    for name, a, b in pos_pairs + siblings:
        report["positives" if (name, a, b) in pos_pairs else "template_siblings"].append(
            {"group": name, "a": a.label, "b": b.label, "a_size": [a.img.width, a.img.height],
             "b_size": [b.img.width, b.img.height], "same_bytes": a.sha == b.sha,
             **{k: dist(a, b, k) for k in KINDS}, "cosine": cos(a, b)}
        )
    for k in KINDS:
        P = [dist(a, b, k) for _, a, b in pos_pairs]
        N = [dist(a, b, k) for _, a, b in neg_pairs]
        report["separation"][k] = {"positives": describe(P), "negatives": describe(N), "auc": auc(P, N, False),
                                   "negatives_at_or_below_max_positive": int(sum(n <= max(P) for n in N))}
        P6 = [dist(a, b, k, "bt601") for _, a, b in pos_pairs]
        N6 = [dist(a, b, k, "bt601") for _, a, b in neg_pairs]
        report["separation"][k + "_bt601_baseline"] = {"positives": describe(P6), "negatives": describe(N6), "auc": auc(P6, N6, False)}
    if emb is not None:
        P = [cos(a, b) for _, a, b in pos_pairs]
        N = [cos(a, b) for _, a, b in neg_pairs]
        report["separation"]["image_embedding_cosine"] = {"positives": describe(P), "negatives": describe(N), "auc": auc(P, N, True),
                                                          "negatives_at_or_above_min_positive": int(sum(n >= min(P) for n in N))}
    for pool_name in ("asset", "ad_screenshot"):
        sub = [(a, b) for kn, a, b in neg_pairs if kn == pool_name]
        report["negatives_by_pool"][pool_name] = {k: describe([dist(a, b, k) for a, b in sub]) for k in KINDS}
        if emb is not None:
            report["negatives_by_pool"][pool_name]["cosine"] = describe([cos(a, b) for a, b in sub])
    # closest negatives, for inspection (possible unknown true duplicates)
    report["closest_negatives_pdq"] = [
        {"a": a.label, "b": b.label, **{k: dist(a, b, k) for k in KINDS}, "cosine": cos(a, b)}
        for _, a, b in sorted(neg_pairs, key=lambda t: dist(t[1], t[2], "image_pdq"))[:15]
    ]

    # --- synthetic robustness on real creatives (ad screenshots and larger assets)
    creatives = [it for it in (get(p) for p in shots) if it is not None]
    big_assets = [it for it in (get(p) for p in assets) if it is not None and min(it.img.width, it.img.height) >= 200]
    rng.shuffle(big_assets)
    base = creatives + big_assets[: max(0, 40 - len(creatives))]

    def enc(img, fmt, q):
        buf = io.BytesIO()
        img.save(buf, format=fmt, quality=q)
        return load(buf.getvalue()).rgb

    transforms = {f"hue_rotate_{d}deg": (lambda im, d=d: hue_rotate(im, d)) for d in (2, 5, 8, 30, 90, 180)}
    transforms.update({"jpeg_q50": lambda im: enc(im, "JPEG", 50), "jpeg_q85": lambda im: enc(im, "JPEG", 85),
                       "webp_q50": lambda im: enc(im, "WEBP", 50), "webp_q85": lambda im: enc(im, "WEBP", 85)})
    for s in (0.25, 0.5, 2.0):
        transforms[f"resize_{s}x"] = lambda im, s=s: im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.Resampling.BILINEAR)
    transforms["hue_8deg+webp_q75+resize_0.5x"] = lambda im: enc(hue_rotate(im, 8), "WEBP", 75).resize((im.width // 2, im.height // 2), Image.Resampling.BILINEAR)
    transforms["hflip"] = lambda im: im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    transforms["crop_10pct"] = lambda im: im.crop((im.width // 20, im.height // 20, im.width - im.width // 20, im.height - im.height // 20))
    robust = {}
    for tname, fn in transforms.items():
        rows = {k: [] for k in KINDS}
        rows601 = {k: [] for k in KINDS}
        coss = []
        for it in base:
            try:
                im2 = fn(it.img.rgb)
                h2 = hashes(im2)
                h2b = hashes(im2, BT601)
            except SkipImage:
                continue
            for k in KINDS:
                rows[k].append(hamming(it.h[k], h2[k]))
                rows601[k].append(hamming(it.h601[k], h2b[k]))
            if emb is not None and tname in ("hue_rotate_8deg", "hue_rotate_90deg", "webp_q50", "resize_0.25x", "hue_8deg+webp_q75+resize_0.5x", "hflip", "crop_10pct"):
                if it.vec is None:
                    it.vec, _ = emb(it.img.rgb)
                v2, _ = emb(im2)
                coss.append(float(it.vec @ v2))
        robust[tname] = {k: describe(v) for k, v in rows.items()}
        if tname.startswith("hue"):
            robust[tname].update({k + "_bt601_baseline": describe(v) for k, v in rows601.items()})
        if coss:
            robust[tname]["cosine"] = describe(coss)
    report["robustness"] = {"n_images": len(base), "transforms": robust}

    # --- CPU time per image
    sample = base[:30]
    t0 = time.process_time()
    for it in sample:
        hashes(it.img.rgb)
    t_hash = (time.process_time() - t0) / max(1, len(sample))
    t0 = time.process_time()
    for it in sample:
        load(it.data)
    t_load = (time.process_time() - t0) / max(1, len(sample))
    report["cpu"] = {"threads": int(os.environ.get("FTF_THREADS", "4")),
                     "decode_s_per_image": round(t_load, 4), "hashes_s_per_image": round(t_hash, 4)}
    if emb is not None:
        wall = []
        for it in sample[:20]:
            t0 = time.perf_counter()
            emb(it.img.rgb)
            wall.append(time.perf_counter() - t0)
        cache = Path(embed.model_dir()) / "hf"
        size = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file() and not f.is_symlink())
        report["cpu"].update({"embedding_model": emb.model_id, "embedding_dim": int(sample[0].vec.shape[0]) if sample and sample[0].vec is not None else None,
                              "embedding_cpu_s_per_image": round(float(np.median(t_emb)), 4) if t_emb else None,
                              "embedding_wall_s_per_image": round(float(np.median(wall)), 4),
                              "model_download_bytes": size})
    (args.out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps({"positives": len(pos_pairs), "negatives": len(neg_pairs), "separation": {k: v["auc"] for k, v in report["separation"].items()}, "cpu": report["cpu"]}, indent=1))


if __name__ == "__main__":
    main()
