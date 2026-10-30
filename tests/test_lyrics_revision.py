"""歌词修订与当前版本选择测试，所有模型响应和文件均隔离。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from PIL import Image

from music_agent import workflow
from music_agent.agents import composer, lyricist
from music_agent.packaging import export_package
from music_agent.planner import STAGES
from music_agent.review import approve_audio, approve_lyrics, reject_lyrics
from music_agent.storage import get_lyrics_path, load_state


@pytest.fixture
def revision_run(tmp_path, monkeypatch):
    """创建待审核歌词，模拟后续两次修订及作曲响应，不调用 API。"""
    monkeypatch.chdir(tmp_path)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (8, 8), "blue").save(cover)
    brief = {
        "project_id": "revision_test",
        "theme": "毕业告别",
        "genre": "流行",
        "mood": "温暖",
        "duration_seconds": 1,
        "audience": "毕业生",
        "lyrics_language": "中文",
        "platform": ["测试平台"],
        "primary_platform": "测试平台",
        "allow_sampling": False,
        "cover_placeholder_path": str(cover),
    }
    lyrics = {
        "project_name": "毕业企划",
        "title": "再见",
        "sections": [{"name": "主歌", "emotion": "温暖", "lines": ["旧稿"]}],
    }
    second, third = deepcopy(lyrics), deepcopy(lyrics)
    second["sections"][0]["lines"] = ["第二稿的毕业场景"]
    third["sections"][0]["lines"] = ["第三稿的毕业场景"]
    lyrics_model = Mock(side_effect=[deepcopy(lyrics), second, third])
    monkeypatch.setattr(lyricist, "request_json", lyrics_model)
    plan = {
        "project_id": brief["project_id"],
        "steps": [{"stage": stage, "description": "测试任务"} for stage in STAGES],
    }
    monkeypatch.setattr(workflow, "generate_plan", Mock(return_value=plan))
    composition_model = Mock(return_value={
        "bpm": 100,
        "key": "C",
        "sections": [{"name": "主歌", "duration_seconds": 1, "arrangement": "钢琴"}],
        "handoff": "测试音频",
    })
    monkeypatch.setattr(composer, "request_json", composition_model)
    return workflow.run_creation(brief), lyrics_model, composition_model


def test_revision_preserves_old_file_and_waits_for_review(revision_run):
    """修订保存新文件、反馈传入模型，且新稿不能自动进入作曲。"""
    run_dir, lyrics_model, composition_model = revision_run
    original = (run_dir / "lyrics.json").read_bytes()
    reject_lyrics(run_dir, "测试审核人", "增加毕业场景")
    workflow.resume_creation(run_dir)

    state = load_state(run_dir)
    assert state["status"] == "lyrics_ready"
    assert state["lyrics_version"] == 2
    assert state["lyrics_file"] == "lyrics-v002.json"
    assert (run_dir / "lyrics.json").read_bytes() == original
    assert state["review_history"][0]["decision"] == "rejected"
    context = json.loads(lyrics_model.call_args.args[1])
    assert context["feedback"] == "增加毕业场景"
    assert context["previous_lyrics"]["sections"][0]["lines"] == ["旧稿"]
    with pytest.raises(ValueError):
        workflow.resume_creation(run_dir)
    composition_model.assert_not_called()


def test_multiple_revisions_follow_state_pointer(revision_run):
    """连续拒绝时依次修订当前稿，历史稿件保持原样。"""
    run_dir, lyrics_model, _ = revision_run
    reject_lyrics(run_dir, "测试审核人", "第二稿意见")
    workflow.resume_creation(run_dir)
    second_path = get_lyrics_path(run_dir, load_state(run_dir))
    second_data = second_path.read_bytes()
    reject_lyrics(run_dir, "测试审核人", "第三稿意见")
    workflow.resume_creation(run_dir)

    state = load_state(run_dir)
    assert state["lyrics_file"] == "lyrics-v003.json"
    assert state["lyrics_version"] == 3
    assert second_path.read_bytes() == second_data
    assert [record["version"] for record in state["review_history"]] == [1, 2]
    context = json.loads(lyrics_model.call_args.args[1])
    assert context["previous_lyrics"] == json.loads(second_data)


def test_existing_revision_not_overwritten(revision_run):
    """目标文件已存在时，不能覆盖或再次付费生成。"""
    run_dir, lyrics_model, _ = revision_run
    reject_lyrics(run_dir, "测试审核人", "修改意见")
    target = run_dir / "lyrics-v002.json"
    target.write_bytes(b"existing revision")
    with pytest.raises(ValueError, match="目标歌词版本已存在"):
        workflow.resume_creation(run_dir)
    assert target.read_bytes() == b"existing revision"
    assert lyrics_model.call_count == 1
    assert load_state(run_dir)["status"] == "lyrics_rejected"


def test_revision_cannot_change_project_name(revision_run):
    """模型改变项目名时，拒绝新稿，不切换当前版本。"""
    run_dir, lyrics_model, _ = revision_run
    changed = json.loads((run_dir / "lyrics.json").read_bytes())
    changed["project_name"] = "另一个项目"
    lyrics_model.side_effect = [changed]
    reject_lyrics(run_dir, "测试审核人", "修改意见")
    with pytest.raises(ValueError, match="不能改变创作项目名"):
        workflow.resume_creation(run_dir)
    assert load_state(run_dir)["lyrics_file"] == "lyrics.json"
    assert not (run_dir / "lyrics-v002.json").exists()


def test_revised_lyrics_used_by_approval_composition_and_export(revision_run):
    """审核、作曲和打包必须跟随当前歌词指针，不读取旧稿。"""
    run_dir, _, composition_model = revision_run
    reject_lyrics(run_dir, "测试审核人", "增加毕业场景")
    workflow.resume_creation(run_dir)
    approve_lyrics(run_dir, "测试审核人")
    workflow.resume_creation(run_dir)

    context = json.loads(composition_model.call_args.args[1])
    assert context["approved_lyrics"]["sections"][0]["lines"] == ["第二稿的毕业场景"]
    handoff = json.loads((run_dir / load_state(run_dir)["composition_result_file"]).read_bytes())
    assert handoff["lyrics_source"]["file"] == "lyrics-v002.json"
    assert handoff["lyrics_source"]["version"] == 2
    approve_audio(run_dir, "测试审核人")
    package = export_package(run_dir)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["lyrics_source"]["file"] == "lyrics-v002.json"
    assert manifest["versions"]["lyrics"] == 2
    assert manifest["audio"]["model"] == load_state(run_dir)["audio_job"]["model"]
    assert manifest["audio"]["model_parameters"] == load_state(run_dir)["audio_job"]["model_parameters"]
    assert "第二稿的毕业场景" in (package / "lyrics.md").read_text(encoding="utf-8")
    assert {path.name for path in package.iterdir()} == {
        "manifest.json", "lyrics.md", "composition.json", "audio.wav",
        "cover.png", "approvals.json", "README.md",
    }
    assert load_state(run_dir)["status"] == "package_ready"
    report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
    assert report["current_status"] == "package_ready"
    assert any(event["progress"]["status"] == "lyrics_rejected" for event in report["history"])
    assert report["latest_progress"]["versions"]["lyrics"] == 2
