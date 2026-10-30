"""相同需求重跑及旧项目兼容测试，不调用真实模型。"""

import json
import shutil
from unittest.mock import Mock

import pytest

from music_agent import workflow
from music_agent.planner import STAGES
from music_agent.storage import calculate_brief_fingerprint, load_state, save_state


@pytest.fixture
def existing_run(tmp_path, monkeypatch):
    """在临时目录生成一个项目，并保留模型调用计数。"""
    monkeypatch.chdir(tmp_path)
    brief = {
        "project_id": "idempotency_test",
        "theme": "毕业告别",
        "genre": "流行",
        "mood": "温暖",
        "duration_seconds": 1,
        "audience": "毕业生",
        "lyrics_language": "中文",
        "platform": ["测试平台"],
        "primary_platform": "测试平台",
        "allow_sampling": False,
    }
    plan_model = Mock(return_value={
        "project_id": brief["project_id"],
        "steps": [{"stage": stage, "description": "测试任务"} for stage in STAGES],
    })
    lyrics_model = Mock(return_value={
        "project_name": "告别企划",
        "title": "再见",
        "sections": [{"name": "主歌", "emotion": "温暖", "lines": ["测试歌词"]}],
    })
    monkeypatch.setattr(workflow, "generate_plan", plan_model)
    monkeypatch.setattr(workflow, "generate_lyrics", lyrics_model)
    run_dir = workflow.run_creation(brief)
    return brief, run_dir, plan_model, lyrics_model


def test_same_brief_reuses_run_without_modifying_files(existing_run):
    """同需求重跑不能新增目录、改动产物或重新调用模型。"""
    brief, run_dir, plan_model, lyrics_model = existing_run
    before = {path.name: path.read_bytes() for path in run_dir.iterdir()}
    assert workflow.run_creation(brief) == run_dir
    assert list(run_dir.parent.glob("run-*")) == [run_dir]
    assert {path.name: path.read_bytes() for path in run_dir.iterdir()} == before
    assert plan_model.call_count == lyrics_model.call_count == 1


def test_json_order_and_format_do_not_create_new_project(existing_run):
    """字段顺序和文件排版变化不能被误认为新需求。"""
    brief, run_dir, plan_model, lyrics_model = existing_run
    reordered = dict(reversed(list(brief.items())))
    (run_dir / "brief.json").write_text(
        json.dumps(reordered, ensure_ascii=False), encoding="utf-8"
    )
    assert calculate_brief_fingerprint(brief) == calculate_brief_fingerprint(reordered)
    assert workflow.run_creation(reordered) == run_dir
    assert plan_model.call_count == lyrics_model.call_count == 1


def test_changed_brief_with_same_id_is_rejected(existing_run):
    """同编号的新需求不能静默复用旧项目或创建另一个项目。"""
    brief, run_dir, plan_model, lyrics_model = existing_run
    with pytest.raises(ValueError, match="需求内容不同"):
        workflow.run_creation(dict(brief, theme="另一个主题"))
    assert list(run_dir.parent.glob("run-*")) == [run_dir]
    assert plan_model.call_count == lyrics_model.call_count == 1


def test_legacy_run_without_fingerprint_can_be_reused(existing_run):
    """旧状态缺少指纹时，使用保存的需求单判断，不改动旧状态。"""
    brief, run_dir, plan_model, lyrics_model = existing_run
    state = load_state(run_dir)
    state.pop("brief_fingerprint")
    save_state(run_dir, state)
    original = (run_dir / "state.json").read_bytes()
    assert workflow.run_creation(brief) == run_dir
    assert (run_dir / "state.json").read_bytes() == original
    assert plan_model.call_count == lyrics_model.call_count == 1


def test_changed_saved_brief_is_rejected(existing_run):
    """保存的快照与已登记指纹不一致时，不得继续复用。"""
    brief, run_dir, plan_model, lyrics_model = existing_run
    (run_dir / "brief.json").write_text(
        json.dumps(dict(brief, theme="被修改的主题")), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="快照发生变化"):
        workflow.run_creation(brief)
    assert plan_model.call_count == lyrics_model.call_count == 1


def test_missing_saved_brief_is_rejected(existing_run):
    """缺少快照时无法确认同需求，不应重新调用模型。"""
    brief, run_dir, plan_model, lyrics_model = existing_run
    (run_dir / "brief.json").unlink()
    with pytest.raises(ValueError, match="缺少需求单快照"):
        workflow.run_creation(brief)
    assert plan_model.call_count == lyrics_model.call_count == 1


def test_multiple_existing_runs_are_rejected(existing_run):
    """发现多个同编号项目时，应报告歧义而不是随机选择。"""
    brief, run_dir, plan_model, lyrics_model = existing_run
    shutil.copytree(run_dir, run_dir.parent / "run-duplicate")
    with pytest.raises(ValueError, match="多个运行记录"):
        workflow.run_creation(brief)
    assert plan_model.call_count == lyrics_model.call_count == 1
