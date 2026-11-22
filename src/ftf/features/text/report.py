"""Pairwise text similarity report over a text-family output (for docs and acceptance checks).

    python -I -m ftf.features.text.report OUT_DIR [--pair SUBSTR_A SUBSTR_B] [--kind page_text]

For every pair of source texts of one kind: exact Jaccard of word 5-shingles on the raw and the
masked text, the MinHash estimate from ``text_template`` and the embedding cosine. Prints the
named pair (artefact paths containing the substrings), its rank and the distribution over all
other pairs. Raw similarity only: no thresholds.
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np

from ftf.features.common import load_vectors, read_dataset
from ftf.features.text import template as tpl
from ftf.features.text.mask import tokens


def rows(out_dir: Path, kind: str) -> list[dict]:
    ds = read_dataset(out_dir / "dataset.json")
    masked = json.loads((out_dir / "text" / "masked.json").read_text())
    vec_cache: dict[str, dict[str, np.ndarray]] = {}
    out = []
    for inc in ds.incidents:
        paths = {a.id: a.path for c in inc.captures for a in c.artefacts}
        by_src: dict[str, dict] = {}
        for ind in inc.indicators:
            loc = ind.provenance.locator or ""
            if ind.kind == kind and ind.id in masked:
                by_src.setdefault(ind.id, {})["text"] = ind.value
                by_src[ind.id]["path"] = paths.get(ind.provenance.artefact_id)
            elif loc.startswith("src="):
                d = by_src.setdefault(loc[4:], {})
                if ind.kind == "text_template":
                    d["template"] = ind.value
                elif ind.kind == "text_embedding" and ind.vector_ref:
                    store = ind.vector_ref.store
                    if store not in vec_cache:
                        keys, mat = load_vectors(out_dir, store)
                        vec_cache[store] = dict(zip(keys, mat))
                    d["vec"] = vec_cache[store].get(ind.id)
        for sid, d in by_src.items():
            if "text" in d:
                d["id"] = sid
                d["raw"] = tpl.shingles(tokens(d["text"]))
                d["masked"] = tpl.shingles(tokens(masked[sid]["masked_text"]))
                out.append(d)
    return out


def pair_metrics(a: dict, b: dict) -> dict:
    m = {"raw_jaccard": tpl.jaccard(a["raw"], b["raw"]), "masked_jaccard": tpl.jaccard(a["masked"], b["masked"])}
    if a.get("template") and b.get("template"):
        m["minhash_estimate"] = tpl.estimate(a["template"], b["template"])
    if a.get("vec") is not None and b.get("vec") is not None:
        m["embedding_cosine"] = float(np.dot(a["vec"], b["vec"]))
    return {k: round(v, 4) for k, v in m.items()}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out_dir", type=Path)
    p.add_argument("--kind", default="page_text")
    p.add_argument("--pair", nargs=2, default=["BACKUP/p3/", "BACKUP/p4/"])
    p.add_argument("--top", type=int, default=10)
    a = p.parse_args(argv)
    rs = rows(a.out_dir, a.kind)
    pairs = []
    for x, y in itertools.combinations(rs, 2):
        pairs.append({"a": x["path"], "b": y["path"], **pair_metrics(x, y)})
    sa, sb = a.pair

    def is_target(q):
        return (sa in (q["a"] or "") and sb in (q["b"] or "")) or (sb in (q["a"] or "") and sa in (q["b"] or ""))

    target = [q for q in pairs if is_target(q)]
    others = [q for q in pairs if not is_target(q)]
    summary = {"kind": a.kind, "texts": len(rs), "pairs": len(pairs), "target": target, "others": {}}
    for metric in ("raw_jaccard", "masked_jaccard", "minhash_estimate", "embedding_cosine"):
        vals = np.array([q[metric] for q in others if metric in q])
        if not len(vals):
            continue
        summary["others"][metric] = {
            "median": round(float(np.median(vals)), 4),
            "p95": round(float(np.percentile(vals, 95)), 4),
            "p99": round(float(np.percentile(vals, 99)), 4),
            "max": round(float(vals.max()), 4),
        }
        for t in target:
            if metric in t:
                t[f"{metric}_rank"] = int((vals > t[metric]).sum()) + 1
    summary["top_masked"] = sorted(pairs, key=lambda q: -q["masked_jaccard"])[: a.top]
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
