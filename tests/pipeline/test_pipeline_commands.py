import json

from ftf.cli import build_parser, main
from ftf.contract import Dataset
from ftf.pipeline.commands import streamlit_command
from pipeline_synthetic import write_base


def test_parse_analyze_and_serve():
    p = build_parser()
    a = p.parse_args(["analyze", "x", "--kind", "keyword", "--base", "b.json", "--online", "--out", "o.json"])
    assert (a.input, a.kind, str(a.base), a.online, str(a.out)) == ("x", "keyword", "b.json", True, "o.json")
    s = p.parse_args(["serve"])
    assert (s.port, s.address, s.base) == (8501, "127.0.0.1", None)


def test_analyze_command_writes_valid_dataset(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FTF_RUN_DIR", str(tmp_path / "runs"))
    base = write_base(tmp_path)
    out = tmp_path / "result.json"
    assert main(["analyze", "s-2", "--base", str(base), "--out", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["decision"] == "assign" and summary["from_base"]
    Dataset.from_json(out.read_bytes())
    assert main(["analyze", "unknown words", "--base", str(base)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "review"


def test_analyze_command_errors(tmp_path, capsys):
    base = write_base(tmp_path)
    assert main(["analyze", "nope", "--kind", "dataset_item", "--base", str(base)]) == 1
    assert main(["analyze", "x", "--kind", "har", "--base", str(base)]) == 2
    assert main(["analyze", "x", "--base", str(tmp_path / "missing.json")]) == 2


def test_serve_command_line():
    cmd = streamlit_command(8502, "127.0.0.1")
    assert cmd[1:4] == ["-m", "streamlit", "run"] and cmd[4].endswith("app/Home.py")
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
