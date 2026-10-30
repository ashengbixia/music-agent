"""歌词修订、作曲产物提交窗口的模拟崩溃与输入绑定测试。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from music_agent import workflow
from music_agent.planner import STAGES
from music_agent.review import approve_lyrics, reject_lyrics
from music_agent.storage import load_state, save_state


class SimulatedCrash(BaseException):
    """模拟进程中断，绕过业务异常处理。"""


@pytest.fixture
def pending_run(tmp_path, monkeypatch):
    """创建待审核歌词及可重复使用的模型模拟响应。"""
    monkeypatch.chdir(tmp_path)
    brief = {
        "project_id": "crash_test", "theme": "毕业告别", "genre": "流行",
        "mood": "温暖", "duration_seconds": 1, "audience": "毕业生",
        "lyrics_language": "中文", "platform": ["测试平台"],
        "primary_platform": "测试平台", "allow_sampling": False,
    }
    original = {
        "project_name": "毕业企划", "title": "再见",
        "sections": [{"name": "主歌", "emotion": "温暖", "lines": ["旧稿"]}],
    }
    revised = deepcopy(original)
    revised["sections"][0]["lines"] = ["修改后的毕业场景"]
    lyrics_model = Mock(side_effect=lambda *args, **kwargs: deepcopy(
        revised if kwargs.get("previous_lyrics") is not None else original
    ))
    monkeypatch.setattr(workflow, "generate_lyrics", lyrics_model)
    monkeypatch.setattr(workflow, "generate_plan", Mock(return_value={
        "project_id": brief["project_id"],
        "steps": [{"stage": stage, "description": "测试任务"} for stage in STAGES],
    }))
    composition_model = Mock(return_value={
        "bpm": 100, "key": "C",
        "sections": [{"name": "主歌", "duration_seconds": 1, "arrangement": "钢琴"}],
        "handoff": "mock 测试",
    })
    monkeypatch.setattr(workflow, "generate_composition", composition_model)
    return workflow.run_creation(brief), lyrics_model, composition_model


def crash_after_artifact(monkeypatch, filename):
    """在正式文件提交后立刻中断，保留磁盘上的生成任务记录。"""
    real_write = workflow.write_new_artifact

    def write_then_crash(path, data):
        real_write(path, data)
        if path.name == filename:
            raise SimulatedCrash()

    monkeypatch.setattr(workflow, "write_new_artifact", write_then_crash)
    return real_write


@pytest.mark.parametrize("point", ["job", "artifact", "state"])
def test_revision_crash_reuses_target_and_preserves_old_version(pending_run, monkeypatch, point):
    """不同提交窗口的中断均不丢旧稿、不重复修订，也不跳过新稿审核。"""
    run, lyrics_model, composition_model = pending_run
    old_data = (run / "lyrics.json").read_bytes()
    reject_lyrics(run, "测试审核人", "增加毕业场景")
    real_save, real_write = workflow.save_state, workflow.write_new_artifact

    if point == "artifact":
        crash_after_artifact(monkeypatch, "lyrics-v002.json")
    else:
        def crash_on_save(directory, state):
            if point == "job" and state.get("lyrics_revision_job"):
                real_save(directory, state)
                raise SimulatedCrash()
            if point == "state" and state.get("lyrics_version") == 2:
                raise SimulatedCrash()
            real_save(directory, state)
        monkeypatch.setattr(workflow, "save_state", crash_on_save)

    with pytest.raises(SimulatedCrash):
        workflow.resume_creation(run)
    assert load_state(run)["status"] == "lyrics_rejected"
    assert load_state(run)["lyrics_file"] == "lyrics.json"
    assert "lyrics_revision_job" in load_state(run)
    before = (run / "lyrics-v002.json").read_bytes() if point != "job" else None

    monkeypatch.setattr(workflow, "save_state", real_save)
    monkeypatch.setattr(workflow, "write_new_artifact", real_write)
    workflow.resume_creation(run)
    state = load_state(run)
    assert state["status"] == "lyrics_ready"
    assert state["lyrics_file"] == "lyrics-v002.json"
    assert state["lyrics_version"] == 2
    assert "lyrics_revision_job" not in state
    assert "last_error" not in state
    assert (run / "lyrics.json").read_bytes() == old_data
    if before is not None:
        assert (run / state["lyrics_file"]).read_bytes() == before
    assert lyrics_model.call_count == 2
    composition_model.assert_not_called()
    with pytest.raises(ValueError):
        workflow.resume_creation(run)


@pytest.mark.parametrize("point", ["job", "artifact", "state"])
def test_composition_crash_reuses_output_without_model_call(pending_run, monkeypatch, point):
    """作曲登记、落盘或检查点窗口中断后，不产生重复方案和版本。"""
    run, _, composition_model = pending_run
    approve_lyrics(run, "测试审核人")
    real_save, real_write = workflow.save_state, workflow.write_new_artifact
    if point == "artifact":
        crash_after_artifact(monkeypatch, "composition-v001.json")
    else:
        def crash_on_save(directory, state):
            if point == "job" and state.get("composition_job") and not state.get("composition_sha256"):
                real_save(directory, state)
                raise SimulatedCrash()
            if point == "state" and state.get("composition_sha256"):
                raise SimulatedCrash()
            real_save(directory, state)
        monkeypatch.setattr(workflow, "save_state", crash_on_save)
    with pytest.raises(SimulatedCrash):
        workflow.resume_creation(run)
    assert load_state(run)["status"] == "lyrics_approved"
    assert "composition_job" in load_state(run)
    before = (run / "composition-v001.json").read_bytes() if point != "job" else None
    monkeypatch.setattr(workflow, "save_state", real_save)
    monkeypatch.setattr(workflow, "write_new_artifact", real_write)
    workflow.resume_creation(run)
    state = load_state(run)
    assert state["status"] == "audio_ready"
    assert state["composition_version"] == state["audio_version"] == 1
    assert composition_model.call_count == 1
    if before is not None:
        assert (run / "composition-v001.json").read_bytes() == before
    assert len(list(run.glob("composition-v*.json"))) == 1


@pytest.mark.parametrize("changed", ["feedback", "brief", "plan"])
def test_revision_pending_output_not_reused_with_changed_inputs(pending_run, monkeypatch, changed):
    """任务登记后输入变化，不能将旧结果冒充新任务结果。"""
    run, lyrics_model, _ = pending_run
    reject_lyrics(run, "测试审核人", "原修改意见")
    real_write = crash_after_artifact(monkeypatch, "lyrics-v002.json")
    with pytest.raises(SimulatedCrash):
        workflow.resume_creation(run)
    monkeypatch.setattr(workflow, "write_new_artifact", real_write)
    before = (run / "lyrics-v002.json").read_bytes()
    if changed == "feedback":
        state = load_state(run)
        state["lyrics_rejection"]["feedback"] = "另一份意见"
        save_state(run, state)
    else:
        path = run / ("brief.json" if changed == "brief" else "plan.json")
        document = json.loads(path.read_bytes())
        if changed == "brief":
            document["mood"] = "悲伤"
        else:
            document["steps"][0]["description"] = "另一份任务"
        path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="歌词修订任务与当前输入不一致"):
        workflow.resume_creation(run)
    assert (run / "lyrics-v002.json").read_bytes() == before
    assert load_state(run)["status"] == "lyrics_rejected"
    assert lyrics_model.call_count == 2


@pytest.mark.parametrize("changed", ["brief", "plan", "job"])
def test_composition_pending_output_not_reused_with_changed_inputs(pending_run, monkeypatch, changed):
    """作曲任务的输入绑定不匹配时拒绝恢复，不重请求模型。"""
    run, _, composition_model = pending_run
    approve_lyrics(run, "测试审核人")
    real_write = crash_after_artifact(monkeypatch, "composition-v001.json")
    with pytest.raises(SimulatedCrash):
        workflow.resume_creation(run)
    monkeypatch.setattr(workflow, "write_new_artifact", real_write)
    before = (run / "composition-v001.json").read_bytes()
    if changed == "job":
        state = load_state(run)
        state["composition_job"]["inputs_sha256"] = "invalid"
        save_state(run, state)
    else:
        path = run / ("brief.json" if changed == "brief" else "plan.json")
        document = json.loads(path.read_bytes())
        if changed == "brief":
            document["mood"] = "悲伤"
        else:
            document["steps"][2]["description"] = "另一份作曲任务"
        path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="作曲生成任务与当前输入不一致"):
        workflow.resume_creation(run)
    assert (run / "composition-v001.json").read_bytes() == before
    assert load_state(run)["last_error"]["stage"] == "composition"
    assert composition_model.call_count == 1


@pytest.mark.parametrize("stage", ["revision", "composition"])
def test_invalid_pending_json_schema_is_not_adopted(pending_run, monkeypatch, stage):
    """有任务记录不代表可以绕过产物 Schema 校验。"""
    run, lyrics_model, composition_model = pending_run
    if stage == "revision":
        reject_lyrics(run, "测试审核人", "修改意见")
        filename, error_message = "lyrics-v002.json", "歌词结构校验失败"
    else:
        approve_lyrics(run, "测试审核人")
        filename, error_message = "composition-v001.json", "作曲方案结构校验失败"
    real_write = crash_after_artifact(monkeypatch, filename)
    with pytest.raises(SimulatedCrash):
        workflow.resume_creation(run)
    monkeypatch.setattr(workflow, "write_new_artifact", real_write)
    (run / filename).write_bytes(b"{}")
    counts = lyrics_model.call_count, composition_model.call_count
    with pytest.raises(ValueError, match=error_message):
        workflow.resume_creation(run)
    assert counts == (lyrics_model.call_count, composition_model.call_count)
    assert load_state(run)["status"] == ("lyrics_rejected" if stage == "revision" else "lyrics_approved")


def test_untracked_composition_is_not_adopted(pending_run):
    """没有生成任务或检查点的已有作曲文件不能被接管。"""
    run, _, composition_model = pending_run
    approve_lyrics(run, "测试审核人")
    path = run / "composition-v001.json"
    original = b"untracked composition"
    path.write_bytes(original)
    with pytest.raises(ValueError, match="缺少生成任务记录"):
        workflow.resume_creation(run)
    assert path.read_bytes() == original
    composition_model.assert_not_called()


@pytest.mark.parametrize("stage", ["revision", "composition"])
def test_failed_artifact_write_keeps_recoverable_task(pending_run, monkeypatch, stage):
    """产物尚未提交时失败，恢复可以重试生成，但不能产生重复版本。"""
    run, lyrics_model, composition_model = pending_run
    if stage == "revision":
        reject_lyrics(run, "测试审核人", "修改意见")
        filename, error_stage = "lyrics-v002.json", "lyrics_revision"
    else:
        approve_lyrics(run, "测试审核人")
        filename, error_stage = "composition-v001.json", "composition"
    real_write = workflow.write_new_artifact
    monkeypatch.setattr(workflow, "write_new_artifact", Mock(side_effect=OSError("模拟写入失败")))
    with pytest.raises(OSError, match="模拟写入失败"):
        workflow.resume_creation(run)
    assert not (run / filename).exists()
    assert load_state(run)["last_error"]["stage"] == error_stage
    monkeypatch.setattr(workflow, "write_new_artifact", real_write)
    workflow.resume_creation(run)
    assert (run / filename).is_file()
    state = load_state(run)
    assert state["status"] == ("lyrics_ready" if stage == "revision" else "audio_ready")
    assert "last_error" not in state
    assert (lyrics_model.call_count == 3) if stage == "revision" else (composition_model.call_count == 2)
