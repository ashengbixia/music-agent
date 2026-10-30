"""运行报告的历史保留、引用和原子写入测试。"""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from music_agent.reporting import write_run_report
from music_agent.review import approve_lyrics
from music_agent.storage import load_state, save_state


def read_report(run_dir):
    """读取测试生成的报告。"""
    return json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))


def test_save_state_creates_report_without_reading_artifacts(tmp_path):
    """报告复用传入状态，不要求产物存在，也不替代业务校验。"""
    state = {"project_id": "report_test", "status": "new", "lyrics_file": "not-created.json"}
    save_state(tmp_path, state)
    report = read_report(tmp_path)
    assert report["project_id"] == state["project_id"]
    assert report["current_status"] == "new"
    assert report["latest_progress"]["checkpoints"]["lyrics"]["file"] == "not-created.json"
    assert len(report["history"]) == 1
    assert report["history"][0]["progress"] == report["latest_progress"]
    assert report["updated_at"].endswith("（北京时间）")
    assert load_state(tmp_path) == state


def test_failure_history_survives_recovery(tmp_path):
    """恢复成功清除当前错误后，旧失败仍保留在历史中。"""
    state = {"project_id": "report_test", "status": "new"}
    save_state(tmp_path, state)
    state["last_error"] = {"stage": "lyrics", "message": "模拟超时"}
    save_state(tmp_path, state)
    state.pop("last_error")
    state.update(status="lyrics_ready", lyrics_version=1)
    save_state(tmp_path, state)
    report = read_report(tmp_path)
    assert len(report["history"]) == 3
    assert report["history"][1]["progress"]["last_error"]["message"] == "模拟超时"
    assert report["latest_progress"]["last_error"] is None
    assert report["current_status"] == "lyrics_ready"


def test_identical_progress_deduplicated_but_record_references_updated(tmp_path):
    """同状态保存不新增事件，新调用记录仍能被索引。"""
    state = {"project_id": "report_test", "status": "new"}
    save_state(tmp_path, state)
    records_dir = tmp_path / "model-calls"
    records_dir.mkdir()
    (records_dir / "plan-v001-b.json").write_text("{}", encoding="utf-8")
    (records_dir / "plan-v001-a.json").write_text("{}", encoding="utf-8")
    save_state(tmp_path, state)
    report = read_report(tmp_path)
    assert len(report["history"]) == 1
    assert report["model_call_record_count"] == 2
    assert report["model_call_records"] == [
        "model-calls/plan-v001-a.json", "model-calls/plan-v001-b.json",
    ]


def test_checkpoint_change_is_recorded_even_when_status_unchanged(tmp_path):
    """计划完成时状态仍是 new，但进度变化必须保留。"""
    state = {"project_id": "report_test", "status": "new"}
    save_state(tmp_path, state)
    state.update(plan_file="plan.json", plan_sha256="test-hash")
    save_state(tmp_path, state)
    report = read_report(tmp_path)
    assert len(report["history"]) == 2
    assert all(event["progress"]["status"] == "new" for event in report["history"])
    assert report["history"][0]["progress"]["checkpoints"]["plan"]["sha256"] is None
    assert report["latest_progress"]["checkpoints"]["plan"]["sha256"] == "test-hash"


@pytest.mark.parametrize("previous", [
    [],
    {"project_id": "other_project", "history": []},
    {"project_id": "report_test", "history": [{}]},
])
def test_invalid_old_report_is_not_overwritten(tmp_path, previous):
    """损坏或串项目的报告不能被静默覆盖而丢失历史。"""
    path = tmp_path / "run_report.json"
    original = json.dumps(previous).encode("utf-8")
    path.write_bytes(original)
    with pytest.raises(ValueError, match="运行报告"):
        write_run_report(tmp_path, {"project_id": "report_test", "status": "new"})
    assert path.read_bytes() == original


def test_failed_report_replace_preserves_previous_report(tmp_path, monkeypatch):
    """报告提交失败时保留旧文件，并清理本次临时文件。"""
    state = {"project_id": "report_test", "status": "new"}
    write_run_report(tmp_path, state)
    original = (tmp_path / "run_report.json").read_bytes()
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("模拟替换失败")))
    with pytest.raises(OSError, match="替换失败"):
        write_run_report(tmp_path, dict(state, status="lyrics_ready"))
    assert (tmp_path / "run_report.json").read_bytes() == original
    assert not list(tmp_path.glob(".run-report-*.tmp"))


def test_legacy_project_does_not_invent_earlier_history(tmp_path):
    """旧项目首次生成报告时，仅记录当前观察到的状态。"""
    state = {"project_id": "report_test", "status": "package_ready", "package_version": 1}
    save_state(tmp_path, state)
    report = read_report(tmp_path)
    assert len(report["history"]) == 1
    assert report["history"][0]["progress"]["status"] == "package_ready"
    assert report["latest_progress"]["versions"]["package"] == 1


def test_lyrics_approval_immediately_updates_report(tmp_path):
    """审核命令完成时报告必须同步，无需再执行 resume。"""
    lyrics_data = b'{"title": "test"}'
    (tmp_path / "lyrics.json").write_bytes(lyrics_data)
    save_state(tmp_path, {
        "project_id": "report_test", "status": "lyrics_ready",
        "lyrics_file": "lyrics.json", "lyrics_version": 1,
        "lyrics_sha256": hashlib.sha256(lyrics_data).hexdigest(),
    })
    previous_history = read_report(tmp_path)["history"]

    approve_lyrics(tmp_path, "测试审核人")

    state = load_state(tmp_path)
    report = read_report(tmp_path)
    assert state["status"] == report["current_status"] == "lyrics_approved"
    assert report["latest_progress"]["lyrics_approval"] == state["lyrics_approval"]
    assert report["history"][:-1] == previous_history
    assert report["history"][-1]["progress"] == report["latest_progress"]


def test_failed_lyrics_approval_keeps_state_and_report(tmp_path):
    """审核哈希不符时，不得更新状态或产生审核通过事件。"""
    (tmp_path / "lyrics.json").write_bytes(b"changed lyrics")
    save_state(tmp_path, {
        "project_id": "report_test", "status": "lyrics_ready",
        "lyrics_file": "lyrics.json", "lyrics_version": 1,
        "lyrics_sha256": hashlib.sha256(b"original lyrics").hexdigest(),
    })
    before = {
        name: (tmp_path / name).read_bytes()
        for name in ("state.json", "run_report.json")
    }

    with pytest.raises(ValueError, match="歌词文件发生变化"):
        approve_lyrics(tmp_path, "测试审核人")

    assert all((tmp_path / name).read_bytes() == data for name, data in before.items())
