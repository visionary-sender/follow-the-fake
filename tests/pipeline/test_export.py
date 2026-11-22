"""`ftf export` on the synthetic base (no organiser data)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys

from ftf.cli import build_parser, main
from ftf.contract import Dataset
from ftf.pipeline.export import REPORT_FILE, TABLE_FILES, campaign_report, export
from pipeline_synthetic import synthetic_base, write_base


def _rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_export_writes_every_table_and_a_validated_copy(tmp_path):
    ds = synthetic_base()
    out = tmp_path / "export"
    counts = export(ds, out)
    for name in TABLE_FILES:
        assert (out / f"{name}.csv").is_file()
    assert counts["incidents.csv"] == len(ds.incidents) == 3
    assert counts["captures.csv"] == sum(len(i.captures) for i in ds.incidents)
    assert counts["indicators.csv"] == len(ds.indicators())
    assert counts["links.csv"] == len(ds.links) == 2
    assert counts["evidence.csv"] == sum(len(link.evidence) for link in ds.links)
    assert counts["campaigns.csv"] == len(ds.campaigns) == 2
    copy = Dataset.from_json((out / "dataset.json").read_bytes())
    assert copy.model_dump() == ds.model_dump()


def test_tables_keep_contract_ids_and_flag_collector_rows(tmp_path):
    out = tmp_path / "export"
    export(synthetic_base(), out)
    indicators = {r["indicator_id"]: r for r in _rows(out / "indicators.csv")}
    assert indicators["s-1/vp"]["linkable"] == "no" and indicators["s-1/vp"]["layer"] == "collector"
    assert indicators["s-1/pixel"]["incident_id"] == "s-1"
    links = {r["link_id"]: r for r in _rows(out / "links.csv")}
    assert links["L1"]["cert_tier"] == "Strong" and links["L1"]["score_bits"] == "12"
    assert links["L2"]["cert_tier"] == "Insufficient alone"  # 2 bits, below the threshold of the 6-capture base
    evidence = _rows(out / "evidence.csv")
    assert {r["status"] for r in evidence} >= {"shared", "collector_excluded", "missing"}
    incidents = {r["incident_id"]: r for r in _rows(out / "incidents.csv")}
    assert incidents["s-2"]["decision"] == "assign"


def test_campaign_report_has_one_section_per_campaign_and_no_clickable_values(tmp_path):
    ds = synthetic_base()
    report = campaign_report(ds)
    assert report.count("\n## ") == len(ds.campaigns)
    assert "Top shared indicators by bits" in report
    assert "| Layer | Kind | Value | Links | Max bits |" in report
    assert "| tracking_account | meta_pixel | (not recorded) | 1 | 12 |" in report
    assert "No shared indicator carries bits in this campaign." in report  # the singleton C2
    assert "Relations under which all its captures sit together" in report
    assert "Stages seen: ad 2, lander 2" in report
    assert "Missing modalities: video" in report
    assert "http://" not in report and "https://" not in report
    assert "evil.example" not in report
    out = tmp_path / "export"
    export(ds, out)
    assert (out / REPORT_FILE).read_text(encoding="utf-8") == report


def test_campaign_patterns_hold_relation_of_each_campaign():
    ds = synthetic_base()
    report = campaign_report(ds)
    campaign_sections = report.split("\n## ")[1:]
    assert len(campaign_sections) == 2 and all("same campaign" in s for s in campaign_sections)


def test_export_command_and_bad_base(tmp_path, capsys):
    base = write_base(tmp_path)
    out = tmp_path / "out"
    assert main(["export", "--base", str(base), "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["out"] == str(out)
    assert (out / "campaign_patterns.md").is_file()
    assert main(["export", "--base", str(tmp_path / "missing.json"), "--out", str(out)]) == 2
    assert "cannot load the dataset" in capsys.readouterr().err


def test_export_parser_requires_out():
    args = build_parser().parse_args(["export", "--base", "b.json", "--out", "d"])
    assert (str(args.base), str(args.out)) == ("b.json", "d")


def test_export_runs_without_networkx(tmp_path):
    """networkx is an `app` extra; the container installs only the base package. The synthetic base has
    a singleton campaign with no stored links, the case that used to reach the networkx graph."""
    base = write_base(tmp_path)
    out = tmp_path / "out"
    code = (
        "import sys\n"
        "sys.modules['networkx'] = None\n"
        "from pathlib import Path\n"
        "from ftf.cli import main\n"
        "from ftf.contract import Dataset\n"
        "from ftf.pipeline.export import export\n"
        "export(Dataset.from_json(Path(sys.argv[1]).read_bytes()), Path(sys.argv[2]))\n"
        "try:\n"
        "    main(['export', '--help'])\n"
        "except SystemExit as exc:\n"
        "    sys.exit(exc.code)\n"
    )
    done = subprocess.run([sys.executable, "-c", code, str(base), str(out)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert (out / "campaign_patterns.md").is_file()


def test_export_reports_unwritable_output(tmp_path, capsys):
    base = write_base(tmp_path)
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    assert main(["export", "--base", str(base), "--out", str(blocker)]) == 1
    assert "cannot write the output" in capsys.readouterr().err


def test_csv_cells_cannot_start_a_spreadsheet_formula(tmp_path):
    from ftf.pipeline.export import _safe_cell

    assert _safe_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert _safe_cell("-4") == "-4" and _safe_cell("-4.5") == "-4.5"
    assert _safe_cell("plain") == "plain"
