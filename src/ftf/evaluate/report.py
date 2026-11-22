"""`ftf eval report`: the README-ready evaluation report, regenerated on the workbench.

Runs labels, leak, hidden and timesplit (each for both engines), sensitivity, and the open-set,
degrade and distractor commands when they are registered (skipped with a note otherwise). Writes
`report.md` and `report.json` to `<out-dir>/report/`; each test keeps its own output folder beside
it. Every table holds raw counts, macro scores average over label groups, and the two engines sit
side by side. The report names case folders (incident ids), kinds and layers. It never prints
capture ids or organiser values, and it reads only the JSON that the sub-commands wrote.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
from typing import Any

REPORT_SCHEMA = "ftf-eval-report@0.1"
ENGINES = ("baseline", "linking")
MOVE_ORDER = ("-2", "-1", "+1", "+2")

# step name -> (sub-command, extra argv)
STEP_ARGS: dict[str, tuple[str, list[str]]] = {
    "labels": ("labels", []),
    "leak": ("leak", []),
    "hidden baseline": ("hidden", ["--engine", "baseline"]),
    "hidden linking": ("hidden", ["--engine", "linking"]),
    "timesplit baseline": ("timesplit", ["--engine", "baseline"]),
    "timesplit linking": ("timesplit", ["--engine", "linking"]),
    "sensitivity": ("sensitivity", []),
}
REQUIRED_STEPS = tuple(STEP_ARGS)
OPTIONAL_STEPS = ("openset", "degrade", "distractors")

# step name -> result file, relative to the output root
RESULT_FILES: dict[str, str] = {
    "labels": "labels/labels.json",
    "leak": "leak/leak.json",
    "hidden baseline": "hidden/hidden-baseline.json",
    "hidden linking": "hidden/hidden-linking.json",
    "timesplit baseline": "timesplit/timesplit-baseline-hidden.json",
    "timesplit linking": "timesplit/timesplit-linking-hidden.json",
    "sensitivity": "sensitivity/sensitivity-linking.json",
}


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _load(path: Path) -> Any | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


# --- steps -----------------------------------------------------------------------------------


def _eval_parser() -> argparse.ArgumentParser:
    from ftf.evaluate.commands import register  # noqa: PLC0415

    top = argparse.ArgumentParser(prog="ftf")
    register(top.add_subparsers(dest="command"))
    return top


def registered_eval_commands(top: argparse.ArgumentParser) -> set[str]:
    """Names of the `ftf eval` sub-commands that the parser knows."""
    root = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))  # noqa: SLF001
    eval_parser = root.choices["eval"]
    return set(next(a for a in eval_parser._actions if isinstance(a, argparse._SubParsersAction)).choices)  # noqa: SLF001


def _run_step(top: argparse.ArgumentParser, name: str, sub: str, extra: list[str],
              args: argparse.Namespace, out: Path, logs: Path) -> dict[str, Any]:
    argv = ["eval", sub, *extra, "--out-dir", str(out), "--dataset", str(args.dataset), "--spec", str(args.spec)]
    log = logs / f"{name.replace(' ', '-')}.log"
    code = 0
    with log.open("w", encoding="utf-8") as fh, contextlib.redirect_stdout(fh), contextlib.redirect_stderr(fh):
        try:
            ns = top.parse_args(argv)
            code = ns.func(ns)
        except SystemExit as exc:  # argparse errors
            code = int(exc.code or 0)
    status = "ok" if code == 0 else "failed"
    return {"status": status, "exit": code, "log": str(log), "argv": " ".join(argv)}


def run_steps(args: argparse.Namespace, out: Path, logs: Path) -> dict[str, dict[str, Any]]:
    top = _eval_parser()
    available = registered_eval_commands(top)
    status: dict[str, dict[str, Any]] = {}
    for name, (sub, extra) in STEP_ARGS.items():
        status[name] = _run_step(top, name, sub, extra, args, out, logs)
        print(f"{name}: {status[name]['status']} (exit {status[name]['exit']})")
    for sub in OPTIONAL_STEPS:
        if sub not in available:
            status[sub] = {"status": "skipped", "exit": None, "log": None,
                           "note": f"`ftf eval {sub}` is not registered on this build"}
            print(f"{sub}: skipped (not registered)")
            continue
        status[sub] = _run_step(top, sub, sub, [], args, out, logs)
        print(f"{sub}: {status[sub]['status']} (exit {status[sub]['exit']})")
    return status


# --- report content --------------------------------------------------------------------------


def _dataset_section(labels: dict[str, Any], leak: dict[str, Any] | None, build: str | None) -> dict[str, Any]:
    summary = labels["summary"]
    captures = labels["captures"].values()
    return {
        "build": build,
        "captures": summary["captures"],
        "canonical_captures": summary["canonical_captures"],
        "incidents": len({(c["source"], c["folder"]) for c in captures}),
        "groups_per_level": summary["groups_per_level"],
        "must_not_link_pairs": summary["must_not_link_pairs"],
        "missing_captures": len(summary["missing_captures"]),
        "largest_group": max((len(g["members"]) for g in labels["groups"]), default=0),
        "indicators_hidden": leak["hidden_indicators"] if leak else None,
        "indicators_remaining": leak["remaining_indicators"] if leak else None,
        "leak_hits": leak["hits"] if leak else None,
    }


def _e1_section(hidden: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    """Per (level, relation): both engines' pairwise counts, macro B-cubed and must-not-link counts."""
    rows = {e: {(r["level"], r["relation"]): r for r in hidden[e]["levels"]} for e in ENGINES if hidden.get(e)}
    keys: list[tuple[str, str]] = []
    for e in ENGINES:
        for key in rows.get(e, {}):
            if key not in keys:
                keys.append(key)
    out = []
    for level, relation in keys:
        rec: dict[str, Any] = {"level": level, "relation": relation}
        for e in ENGINES:
            r = rows.get(e, {}).get((level, relation))
            if r is None:
                rec[e] = None
                continue
            rec[e] = {
                "label_groups": r["label_groups"],
                "label_groups_non_singleton": r["label_groups_non_singleton"],
                "pairwise": r["pairwise"],
                "macro": r["macro"],
                "must_not_link": r["must_not_link"],
            }
        out.append(rec)
    return out


def _e7_section(ts: dict[str, Any] | None) -> dict[str, Any] | None:
    if ts is None:
        return None
    c = ts["counts"]
    fallback = set(ts["fallback_incidents"])
    return {
        "reference_folders": len(ts["reference"]),
        "later_items": ts["later"],
        "correct_assignment": c.get("correct", 0),
        "wrong_assignment": c.get("wrong", 0),
        "false_accept": c.get("false_accepts", 0),
        "correct_new": c.get("new_correct", 0),
        "missed_new": c.get("new", 0) - c.get("new_correct", 0),
        "review": c.get("review", 0),
        "bridging_folders": len(ts["bridging_folders"]),
        "fragmented_groups": ts["fragmentation"],
        "fallback_items": sum(1 for i in ts["items"] if i["item"] in fallback),
        "truth_groups": ts["truth_groups"],
        "items": [
            {"item": i["item"], "date": i["date"], "expected": i["expected"], "decision": i["decision"],
             "verdict": i["verdict"]}
            for i in ts["items"]
        ],
    }


def _e6_section(s: dict[str, Any] | None, folder: dict[str, str]) -> dict[str, Any] | None:
    from ftf.evaluate.sensitivity import incident_pairs  # noqa: PLC0415

    if s is None:
        return None
    ranking = s["ranking"]
    relations = sorted(s["baseline"]["pair_links"])
    per_relation = {}
    for rel in relations:
        fr = s["fragile"]["by_relation"].get(rel, {"links": 0, "fragile": 0, "worst_case": 0})
        per_relation[rel] = {
            "pair_links": s["baseline"]["pair_links"][rel],
            "flips": sum(r["pair_flips_by_relation"].get(rel, 0) for r in ranking),
            "fragile_links": fr["fragile"],
            "worst_case_links": fr["worst_case"],
        }
    fragile = [r for r in incident_pairs(s["fragile"]["records"], folder) if r["fragile"]]
    return {
        "weights_in_table": s["weights_in_table"],
        "weights_tested": s["weights_tested"],
        "baseline": s["baseline"],
        "per_relation": per_relation,
        "top": [
            {"weight": r["weight"], "bits": r["bits"], "moves": [r["by_move"][m]["total"] for m in MOVE_ORDER],
             "total": r["flips_total"]}
            for r in ranking[:10]
        ],
        "weights_with_no_flip": sum(1 for r in ranking if r["flips_total"] == 0),
        "fragile_links_total": s["fragile"]["fragile"],
        "links_total": s["fragile"]["links"],
        "worst_case_only": s["fragile"]["worst_case_only"],
        "fragile_list": fragile,
    }


def limitations(report: dict[str, Any]) -> list[str]:
    """README-ready limitations, 5 to 8 sentences, with the numbers this run produced."""
    d, e1, e7, e6 = report["dataset"], report["e1"], report["e7"], report["e6"]
    campaign = next((r for r in e1 if r["level"] == "campaign"), None)
    camp_text = ""
    if campaign and campaign["linking"] and campaign["baseline"]:
        lk, bl = campaign["linking"], campaign["baseline"]
        camp_text = (
            f"On the hidden dataset the Linking engine recovers {lk['pairwise']['tp']} of "
            f"{lk['pairwise']['true_pairs']} true campaign pairs and violates "
            f"{lk['must_not_link']['violated']} of {lk['must_not_link']['pairs']} must-not-link pairs, "
            f"against {bl['pairwise']['tp']} of {bl['pairwise']['true_pairs']} and "
            f"{bl['must_not_link']['violated']} of {bl['must_not_link']['pairs']} for the baseline."
        )
    campaign_groups = d["groups_per_level"].get("campaign", {}).get("non_singleton", 0)
    later = e7["linking"]["later_items"] if e7.get("linking") else (e7.get("baseline") or {}).get("later_items", 0)
    fallback = e7["linking"]["fallback_items"] if e7.get("linking") else 0
    sentences = [
        f"The sample is small: {d['canonical_captures']} canonical captures in {d['incidents']} case folders, "
        f"{campaign_groups} non-singleton campaign groups and {later} later items, so these counts are a "
        "regression check, not a benchmark.",
        f"Cluster sizes are skewed: the largest label group holds {d['largest_group']} of "
        f"{d['canonical_captures']} captures, so pair counts are dominated by it and macro-averages over groups "
        "are reported next to them.",
        f"The E7 time split uses the organiser's source set instead of a capture date for {fallback} of {later} "
        "later items, so it tests organiser sets rather than dates.",
        "The reference base is not grown during E7: each later item is assigned against the reference folders "
        "alone, so later items never join one another or the base.",
        "The negative controls come from one batch, so a clean result says little about other sources; "
        + ("the open-set, degrade and distractor outputs are in their own folders and are not tabulated here."
           if report["optional"] and any(v["status"] == "ok" for v in report["optional"].values())
           else "the open-set, degrade and distractor tests (E2 to E4) are not in this report, so no "
                "assignment-rate, false-accept or negative-control claim is made."),
        f"The sensitivity check moves {e6['weights_tested']} weights by ±1 and ±2 bits one at a time: "
        f"{e6['fragile_links_total']} of {e6['links_total']} pair links change verdict under a single-layer ±2 "
        "move, so their verdicts depend on the hand-set priors and are flagged as fragile."
        if e6 else "The sensitivity check (E6) did not run, so no fragility claim is made.",
    ]
    if camp_text:
        sentences.append(camp_text)
    return sentences


def build_report(results: dict[str, Any], steps: dict[str, dict[str, Any]], build: str | None) -> dict[str, Any]:
    """The report as data: tables with raw counts, both engines side by side, plus the limitations."""
    labels = results.get("labels")
    if labels is None:
        raise ValueError("labels.json is missing: the report needs the ground truth")
    leak = results.get("leak")
    folder = {cid: f"{c['source']}:{c['folder']}" for cid, c in labels["captures"].items()}
    report = {
        "schema": REPORT_SCHEMA,
        "dataset": _dataset_section(labels, leak, build),
        "steps": steps,
        "e1": _e1_section({e: results.get(f"hidden {e}") for e in ENGINES}),
        "e7": {e: _e7_section(results.get(f"timesplit {e}")) for e in ENGINES},
        "e6": _e6_section(results.get("sensitivity"), folder),
        "optional": {s: {"status": steps[s]["status"], "exit": steps[s]["exit"]} for s in OPTIONAL_STEPS if s in steps},
    }
    report["limitations"] = limitations(report)
    return report


# --- markdown --------------------------------------------------------------------------------


def _pair_row(rec: dict[str, Any] | None) -> str:
    if rec is None:
        return "not run"
    return rec["pairwise"]["text"]


def _macro_row(rec: dict[str, Any] | None) -> str:
    if rec is None:
        return "not run"
    m = rec["macro"]
    return f"{_f(m['precision'])} / {_f(m['recall'])} over {m['groups']} groups"


def _mnl_row(rec: dict[str, Any] | None) -> str:
    if rec is None:
        return "not run"
    return f"{rec['must_not_link']['violated']} of {rec['must_not_link']['pairs']} pairs"


def render_markdown(report: dict[str, Any]) -> str:
    d = report["dataset"]
    lines = [
        "# Follow the Fake: evaluation report", "",
        f"Dataset build: `{d['build'] or 'unknown'}`. Platform ids are hidden for every test except labels and "
        "leak. Counts are raw: `P 2 of 11 pairs` means 2 of the 11 predicted pairs are true, and `R 2 of 12 pairs` "
        "means 2 of the 12 true pairs were found. Macro scores average over label groups first, then over groups. "
        "`baseline` is the loose floor; `linking` is the Linking engine.", "",
        "## Ground truth and leak check", "",
        "| item | count |", "|---|---|",
        f"| captures (with duplicates) | {d['captures']} |",
        f"| canonical captures | {d['canonical_captures']} |",
        f"| case folders (incidents) | {d['incidents']} |",
        f"| analyst must-not-link pairs | {d['must_not_link_pairs']} |",
        f"| leak check hits (expected 0) | {d['leak_hits']} |",
        f"| hidden indicators (removed) | {d['indicators_hidden']} |", "",
        "| level | groups | non-singleton groups | captures |", "|---|---|---|---|",
    ]
    for level, g in d["groups_per_level"].items():
        lines.append(f"| {level} | {g['groups']} | {g['non_singleton']} | {g['captures']} |")

    lines += ["", "## E1 hidden-layer recovery", "",
              "| relation vs level | baseline pairwise | linking pairwise | baseline macro B-cubed P / R | "
              "linking macro B-cubed P / R | must-not-link violated, baseline | must-not-link violated, linking |",
              "|---|---|---|---|---|---|---|"]
    for r in report["e1"]:
        b, lk = r["baseline"], r["linking"]
        lines.append(f"| {r['relation']} vs {r['level']} | {_pair_row(b)} | {_pair_row(lk)} | {_macro_row(b)} | "
                     f"{_macro_row(lk)} | {_mnl_row(b)} | {_mnl_row(lk)} |")

    e7 = report["e7"]
    lines += ["", "## E7 time split (reference = 8 October captures)", "",
              "| engine | reference folders | later items | correct assignment | wrong assignment | false accept | "
              "correct new | missed (new, should assign) | review | bridging folders | fragmented groups |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name in ENGINES:
        s = e7.get(name)
        if s is None:
            lines.append(f"| {name} | not run | | | | | | | | | |")
            continue
        lines.append(
            f"| {name} | {s['reference_folders']} | {s['later_items']} | {s['correct_assignment']} | "
            f"{s['wrong_assignment']} | {s['false_accept']} | {s['correct_new']} | {s['missed_new']} | "
            f"{s['review']} | {s['bridging_folders']} | {s['fragmented_groups']} |"
        )
    if e7.get("linking") or e7.get("baseline"):
        src = e7.get("linking") or e7.get("baseline")
        base_items = {i["item"]: i for i in (e7.get("baseline") or {"items": []})["items"]}
        lines += ["", "| case folder | date | expected | baseline verdict | linking verdict |", "|---|---|---|---|---|"]
        for i in src["items"]:
            bv = base_items.get(i["item"], {}).get("verdict", "not run")
            lv = next((j["verdict"] for j in (e7.get("linking") or {"items": []})["items"] if j["item"] == i["item"]),
                      "not run")
            lines.append(f"| {i['item']} | {i['date'] or '-'} | {i['expected']} | {bv} | {lv} |")

    e6 = report["e6"]
    lines += ["", "## E6 sensitivity (linking table, moves of −2, −1, +1, +2 bits)", ""]
    if e6 is None:
        lines.append("Not run.")
    else:
        lines += [f"Weights tested: {e6['weights_tested']} of {e6['weights_in_table']} in the table; "
                  f"{e6['weights_with_no_flip']} flip no decision.", "",
                  "| relation | pair links (unperturbed) | pair flips, all moves | fragile links (±2, one layer) | "
                  "worst-case only |", "|---|---|---|---|---|"]
        for rel, s in e6["per_relation"].items():
            lines.append(f"| {rel} | {s['pair_links']} | {s['flips']} | {s['fragile_links']} of {s['pair_links']} | "
                         f"{s['worst_case_links']} |")
        base = e6["baseline"]["e7"]
        lines += ["", f"E7 verdicts, unperturbed: assign {base['assign']}, new {base['new']}, review {base['review']}.",
                  "", "| weight | bits | −2 | −1 | +1 | +2 | total |", "|---|---|---|---|---|---|---|"]
        for r in e6["top"]:
            cells = " | ".join(str(m) for m in r["moves"])
            lines.append(f"| {r['weight']} | {r['bits']:g} | {cells} | {r['total']} |")
        lines += ["", "Fragile links, by case folder (a fragile link flips under one layer moved ±2 bits):", "",
                  "| relation | case folder a | case folder b | state | flips when |", "|---|---|---|---|---|"]
        for r in e6["fragile_list"][:40]:
            moves = "; ".join(f"{x['layer']} {x['delta']:+g} to {x['to']}" for x in r["flips"])
            lines.append(f"| {r['relation']} | {r['a']} | {r['b']} | {r['state']} | {moves} |")
        if len(e6["fragile_list"]) > 40:
            lines.append(f"| ... | {len(e6['fragile_list']) - 40} more in sensitivity-linking.json | | | |")

    lines += ["", "## Open-set, degrade and distractors (E2 to E4)", "", "| command | status |", "|---|---|"]
    for name in OPTIONAL_STEPS:
        s = report["steps"].get(name, {"status": "not registered"})
        code = s.get("exit")
        suffix = "" if code in (None, 0) else f" (exit {code})"
        lines.append(f"| {name} | {s['status']}{suffix} |")

    lines += ["", "## Limitations", "", " ".join(report["limitations"]), ""]
    return "\n".join(lines)


# --- command ---------------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    from ftf.evaluate.commands import add_common, write_json  # noqa: PLC0415
    from ftf.evaluate.hidden import dataset_build  # noqa: PLC0415
    from ftf.evaluate.sensitivity import register as register_sensitivity  # noqa: PLC0415
    from ftf.evaluate.units import DEFAULT_SPEC  # noqa: PLC0415

    register_sensitivity(subparsers)
    p = subparsers.add_parser("report", help="regenerate report.md and report.json (runs every evaluation test)")
    add_common(p)

    def run(args: argparse.Namespace) -> int:
        out = Path(args.out_dir)
        rep = out / "report"
        logs = rep / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        args.spec = Path(args.spec or DEFAULT_SPEC)
        steps = run_steps(args, out, logs)
        results = {name: _load(out / rel) for name, rel in RESULT_FILES.items()}
        if results["labels"] is None:
            print("ftf eval report: error: labels.json missing; the labels step failed (see logs)")
            return 2
        report = build_report(results, steps, dataset_build(args.dataset))
        write_json(rep / "report.json", report)
        (rep / "report.md").write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {rep / 'report.md'} and {rep / 'report.json'}")
        failed = [n for n, s in steps.items() if s["status"] == "failed"]
        if failed:
            print("failed steps: " + ", ".join(failed))
            return 2
        return 0

    p.set_defaults(func=run)
