"""首次生成检查点与中断恢复测试，不访问真实模型。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from music_agent import workflow
from music_agent.planner import STAGES
from music_agent.storage import find_run_directory, load_state, write_new_artifact


class SimulatedCrash(BaseException):
    """模拟进程中断，绕过业务异常处理但不结束测试进程。"""


@pytest.fixture
def initial_models(tmp_path, monkeypatch):
    """提供独立需求单及已验证结构的模型模拟响应。"""
    monkeypatch.chdir(tmp_path)
    brief = {
        "project_id": "initial_test", "theme": "毕业告别", "genre": "流行",
        "mood": "温暖", "duration_seconds": 1, "audience": "毕业生",
        "lyrics_language": "中文", "platform": ["测试平台"],
        "primary_platform": "测试平台", "allow_sampling": False,
    }
    plan = {
        "project_id": brief["project_id"],
        "steps": [{"stage": stage, "description": "测试任务"} for stage in STAGES],
    }
    lyrics = {
        "project_name": "毕业企划", "title": "再见",
        "sections": [{"name": "主歌", "emotion": "温暖", "lines": ["测试歌词"]}],
    }
    plan_model, lyrics_model = Mock(return_value=plan), Mock(return_value=lyrics)
    monkeypatch.setattr(workflow, "generate_plan", plan_model)
    monkeypatch.setattr(workflow, "generate_lyrics", lyrics_model)
    return brief, plan, lyrics, plan_model, lyrics_model


def test_new_state_exists_before_first_request(initial_models):
    """模型请求开始前，项目必须已经能被状态查询定位。"""
    brief, plan, _, plan_model, _ = initial_models

    def inspect_initial_state(_brief, **_kwargs):
        run = find_run_directory(brief["project_id"])
        assert load_state(run)["status"] == "new"
        assert (run / "run_report.json").is_file()
        assert (run / "brief.json").is_file()
        return plan

    plan_model.side_effect = inspect_initial_state
    run = workflow.run_creation(brief)
    assert load_state(run)["status"] == "lyrics_ready"


def test_plan_failure_can_resume_and_run_does_not_implicitly_retry(initial_models):
    """首次计划失败仍可找到项目，run 复用目录，resume 显式重试。"""
    brief, plan, _, plan_model, lyrics_model = initial_models
    plan_model.side_effect = [TimeoutError("模拟计划超时"), plan]
    with pytest.raises(TimeoutError):
        workflow.run_creation(brief)
    run = find_run_directory(brief["project_id"])
    state = load_state(run)
    assert state["status"] == "new"
    assert state["last_error"]["stage"] == "plan"
    assert workflow.run_creation(brief) == run
    assert plan_model.call_count == 1
    lyrics_model.assert_not_called()

    workflow.resume_creation(run)
    assert plan_model.call_count == 2
    assert lyrics_model.call_count == 1
    assert load_state(run)["status"] == "lyrics_ready"
    assert "last_error" not in load_state(run)
    assert list(run.parent.glob("run-*")) == [run]


def test_lyrics_failure_reuses_saved_plan(initial_models):
    """歌词失败后的恢复应复用计划，不重复请求已完成阶段。"""
    brief, _, lyrics, plan_model, lyrics_model = initial_models
    lyrics_model.side_effect = [ValueError("模拟歌词非法输出"), lyrics]
    with pytest.raises(ValueError, match="非法输出"):
        workflow.run_creation(brief)
    run = find_run_directory(brief["project_id"])
    original = (run / "plan.json").read_bytes()
    assert load_state(run)["last_error"]["stage"] == "lyrics"

    workflow.resume_creation(run)
    assert (run / "plan.json").read_bytes() == original
    assert plan_model.call_count == 1
    assert lyrics_model.call_count == 2
    assert load_state(run)["status"] == "lyrics_ready"


@pytest.mark.parametrize("stage", ["plan", "lyrics"])
def test_complete_file_before_state_save_can_resume(initial_models, monkeypatch, stage):
    """产物完整写出、检查点尚未保存就中断时，应复用该产物。"""
    brief, _, _, plan_model, lyrics_model = initial_models
    real_save = workflow.save_state

    def crash_before_checkpoint(run, state):
        if f"{stage}_sha256" in state:
            raise SimulatedCrash()
        real_save(run, state)

    monkeypatch.setattr(workflow, "save_state", crash_before_checkpoint)
    with pytest.raises(SimulatedCrash):
        workflow.run_creation(brief)
    run = find_run_directory(brief["project_id"])
    artifact = run / f"{stage}.json"
    original = artifact.read_bytes()
    assert load_state(run)["status"] == "new"

    monkeypatch.setattr(workflow, "save_state", real_save)
    workflow.resume_creation(run)
    assert artifact.read_bytes() == original
    assert plan_model.call_count == lyrics_model.call_count == 1
    assert load_state(run)["status"] == "lyrics_ready"


@pytest.mark.parametrize("missing", [False, True])
def test_registered_plan_changed_or_missing_is_rejected(initial_models, missing):
    """已登记计划被修改或丢失时，不得静默重生成。"""
    brief, _, _, plan_model, lyrics_model = initial_models
    lyrics_model.side_effect = TimeoutError("模拟歌词超时")
    with pytest.raises(TimeoutError):
        workflow.run_creation(brief)
    run = find_run_directory(brief["project_id"])
    path = run / "plan.json"
    if missing:
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="已丢失|哈希不一致"):
        workflow.resume_creation(run)
    assert plan_model.call_count == lyrics_model.call_count == 1
    assert load_state(run)["status"] == "new"


def test_invalid_unregistered_lyrics_are_not_adopted(initial_models):
    """恢复时也必须通过歌词 Schema，不接受仅能解析的 JSON。"""
    brief, _, _, plan_model, lyrics_model = initial_models
    lyrics_model.side_effect = TimeoutError("模拟歌词超时")
    with pytest.raises(TimeoutError):
        workflow.run_creation(brief)
    run = find_run_directory(brief["project_id"])
    (run / "lyrics.json").write_bytes(b'{"title":"invalid"}')
    with pytest.raises(ValueError, match="歌词结构校验失败"):
        workflow.resume_creation(run)
    assert plan_model.call_count == lyrics_model.call_count == 1
    assert load_state(run)["status"] == "new"


def test_saved_brief_change_blocks_initial_resume(initial_models):
    """需求快照变化后，不能将旧检查点用于新需求。"""
    brief, _, _, plan_model, lyrics_model = initial_models
    plan_model.side_effect = TimeoutError("模拟计划超时")
    with pytest.raises(TimeoutError):
        workflow.run_creation(brief)
    run = find_run_directory(brief["project_id"])
    path = run / "brief.json"
    path.write_text(path.read_text(encoding="utf-8").replace("毕业告别", "另一个主题"), encoding="utf-8")
    with pytest.raises(ValueError, match="需求单与初始检查点不一致"):
        workflow.resume_creation(run)
    assert plan_model.call_count == 1
    lyrics_model.assert_not_called()


def test_new_artifact_does_not_overwrite_existing_file(tmp_path):
    """已有文件必须保持原样，失败时清理本次临时文件。"""
    path = tmp_path / "plan.json"
    path.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        write_new_artifact(path, b"replacement")
    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob(".*.tmp"))


def test_failed_artifact_commit_leaves_no_partial_target(tmp_path, monkeypatch):
    """最终改名失败时，不能留下半份正式产物。"""
    path = tmp_path / "plan.json"
    monkeypatch.setattr(Path, "rename", Mock(side_effect=OSError("模拟提交失败")))
    with pytest.raises(OSError, match="提交失败"):
        write_new_artifact(path, b"complete data")
    assert not path.exists()
    assert not list(tmp_path.glob(".*.tmp"))
