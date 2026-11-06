"""Exercise the actual GEval integration with an offline judge response."""

import hashlib
import json

import pytest

from music_agent.evaluation import load_case, main


@pytest.fixture
def saved_run(tmp_path):
    lyrics = {"project_name": "评测", "title": "告别", "sections": [
        {"name": "副歌", "emotion": "温暖", "lines": ["约好下次再相见"]}
    ]}
    for name, data in {
        "state.json": {"lyrics_file": "lyrics-v002.json"},
        "brief.json": {"project_id": "eval-test", "theme": "毕业告别"},
        "lyrics-v002.json": lyrics,
    }.items():
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def test_loads_current_revision_and_dry_run_without_key(saved_run, monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    case = load_case(saved_run)
    assert case["lyrics_file"] == "lyrics-v002.json"
    assert case["lyrics_sha256"] == hashlib.sha256(
        (saved_run / "lyrics-v002.json").read_bytes()
    ).hexdigest()
    assert main(["--run", str(saved_run), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"


def test_geval_report_and_failure_exit_code(saved_run, tmp_path, monkeypatch):
    pytest.importorskip("deepeval")
    from music_agent.clients import deepseek

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setenv("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    calls = []

    def fake_request(system, prompt, *, on_response):
        calls.append(prompt)
        on_response({"status": "response_received", "usage": {"total_tokens": 20}})
        return {"score": 8, "reason": "主题清楚，副歌表达自然。"}

    monkeypatch.setattr(deepseek, "request_json", fake_request)
    before = {p.name: p.read_bytes() for p in saved_run.iterdir() if p.is_file()}
    output = tmp_path / "reports"
    assert main(["--run", str(saved_run), "--output", str(output)]) == 0
    report = json.loads(next(output.glob("*.json")).read_text(encoding="utf-8"))
    assert report["passed_cases"] == 1
    assert len(calls) == 3
    assert all(m["score"] == 0.8 for m in report["cases"][0]["metrics"])
    assert len(report["model_calls"]) == 3
    assert all((saved_run / name).read_bytes() == raw for name, raw in before.items())
    assert main(["--run", str(saved_run), "--output", str(output), "--threshold", "0.9"]) == 1


def test_judge_errors_are_not_quality_scores(saved_run, tmp_path, monkeypatch):
    pytest.importorskip("deepeval")
    from music_agent.clients import deepseek

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")

    def fail(*args, **kwargs):
        raise TimeoutError("network timeout")

    monkeypatch.setattr(deepseek, "request_json", fail)
    output = tmp_path / "errors"
    assert main(["--run", str(saved_run), "--output", str(output)]) == 1
    report = json.loads(next(output.glob("*.json")).read_text(encoding="utf-8"))
    assert all(m["score"] is None and m["error_type"] == "TimeoutError"
               for m in report["cases"][0]["metrics"])
