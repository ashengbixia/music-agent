"""断点恢复测试：不调用真实模型，不使用已有歌曲目录。"""

import hashlib
import json
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from music_agent import workflow
from music_agent.clients.mock_music import generate_audio, inspect_audio
from music_agent.planner import STAGES
from music_agent.review import approve_lyrics
from music_agent.storage import load_state
from music_agent.storage import load_state, save_state

@pytest.fixture
def failed_audio_run(tmp_path, monkeypatch):
    """在临时目录创建作曲成功、音频失败的运行记录。"""
    monkeypatch.chdir(tmp_path)

    brief = {
        "project_id": "test_song",
        "theme": "测试主题",
        "genre": "流行",
        "mood": "温暖",
        "duration_seconds": 1,
        "audience": "测试受众",
        "lyrics_language": "中文",
        "platform": ["测试平台"],
        "primary_platform": "测试平台",
        "allow_sampling": False,
    }
    lyrics = {
        "project_name": "测试企划",
        "title": "测试歌曲",
        "sections": [
            {"name": "主歌", "emotion": "温暖", "lines": ["测试歌词"]}
        ],
    }
    plan = {
        "project_id": "test_song",
        "steps": [
            {"stage": stage, "description": "测试任务"}
            for stage in STAGES
        ],
    }
    composition = {
        "bpm": 100,
        "key": "C",
        "sections": [
            {
                "name": "主歌",
                "duration_seconds": 1,
                "arrangement": "钢琴",
            }
        ],
        "handoff": "仅用于测试",
    }

    # 替换模型调用，避免网络访问及 API 费用。
    monkeypatch.setattr(workflow, "generate_plan", Mock(return_value=plan))
    monkeypatch.setattr(workflow, "generate_lyrics", Mock(return_value=lyrics))
    compose_mock = Mock(return_value=composition)
    monkeypatch.setattr(workflow, "generate_composition", compose_mock)

    run_dir = workflow.run_creation(brief)
    approve_lyrics(run_dir, "测试审核人")

    monkeypatch.setattr(
        workflow, "generate_audio",
        Mock(side_effect=OSError("模拟音频失败")),
    )
    with pytest.raises(OSError, match="模拟音频失败"):
        workflow.resume_creation(run_dir)

    return run_dir, compose_mock


def test_resume_reuses_composition(failed_audio_run, monkeypatch):
    """音频失败后继续执行，应复用方案并保留原文件内容。"""
    run_dir, compose_mock = failed_audio_run
    composition_path = run_dir / "composition-v001.json"
    original = composition_path.read_bytes()

    state = load_state(run_dir)
    assert state["status"] == "lyrics_approved"
    assert state["last_error"]["stage"] == "audio"

    # 恢复真实的本地 mock 音频生成，不会调用外部服务。
    monkeypatch.setattr(workflow, "generate_audio", generate_audio)
    workflow.resume_creation(run_dir)

    state = load_state(run_dir)
    assert state["status"] == "audio_ready"
    assert "last_error" not in state
    assert (run_dir / "audio-v001.wav").is_file()
    assert composition_path.read_bytes() == original
    assert compose_mock.call_count == 1


def test_resume_rejects_changed_composition(failed_audio_run, monkeypatch):
    """方案内容变化时，不得继续生成音频或重新请求作曲。"""
    run_dir, compose_mock = failed_audio_run
    path = run_dir / "composition-v001.json"
    path.write_bytes(path.read_bytes() + b"\n")

    audio_mock = Mock()
    monkeypatch.setattr(workflow, "generate_audio", audio_mock)

    with pytest.raises(ValueError, match="检查点不一致"):
        workflow.resume_creation(run_dir)

    audio_mock.assert_not_called()
    assert compose_mock.call_count == 1
    assert load_state(run_dir)["status"] == "lyrics_approved"

def test_resume_reuses_complete_audio(failed_audio_run, monkeypatch):
    """音频已完成但状态尚未保存时，应直接复用完整文件。"""
    run_dir, compose_mock = failed_audio_run
    audio_path = run_dir / "audio-v001.wav"

    # 模拟生成完成后、更新 audio_ready 之前程序中断。
    generate_audio(audio_path, 1)
    original = audio_path.read_bytes()

    audio_mock = Mock()
    monkeypatch.setattr(workflow, "generate_audio", audio_mock)
    workflow.resume_creation(run_dir)

    audio_mock.assert_not_called()
    assert audio_path.read_bytes() == original
    assert compose_mock.call_count == 1

    state = load_state(run_dir)
    assert state["status"] == "audio_ready"
    assert state["audio"]["is_mock"] is True
    assert state["audio"]["model"] == state["audio_job"]["model"]
    assert state["audio"]["model_parameters"] == state["audio_job"]["model_parameters"]
    assert "last_error" not in state


def test_resume_recovers_truncated_audio(failed_audio_run, monkeypatch):
    """截断的未审核音频应保留为故障文件，再生成完整音频。"""
    run_dir, compose_mock = failed_audio_run
    audio_path = run_dir / "audio-v001.wav"

    # 保留 WAV 文件头，但截断采样数据。
    generate_audio(audio_path, 1)
    complete = audio_path.read_bytes()
    truncated = complete[:len(complete) // 2]
    audio_path.write_bytes(truncated)

    audio_mock = Mock(wraps=generate_audio)
    monkeypatch.setattr(workflow, "generate_audio", audio_mock)
    workflow.resume_creation(run_dir)

    failed_files = list(run_dir.glob("audio-v001.failed-*.wav"))
    assert len(failed_files) == 1
    assert failed_files[0].read_bytes() == truncated

    # 当前 mock 输出固定，因此重新生成的字节应与原完整文件一致。
    assert audio_path.read_bytes() == complete
    assert audio_mock.call_count == 1
    assert compose_mock.call_count == 1
    assert load_state(run_dir)["status"] == "audio_ready"


def test_resume_rejects_untracked_audio(failed_audio_run, monkeypatch):
    """没有生成记录的已有音频不能自动复用或替换。"""
    run_dir, compose_mock = failed_audio_run
    audio_path = run_dir / "audio-v001.wav"
    generate_audio(audio_path, 1)
    original = audio_path.read_bytes()

    state = load_state(run_dir)
    state.pop("audio_job")
    save_state(run_dir, state)

    audio_mock = Mock()
    monkeypatch.setattr(workflow, "generate_audio", audio_mock)

    with pytest.raises(ValueError, match="缺少生成记录"):
        workflow.resume_creation(run_dir)

    audio_mock.assert_not_called()
    assert audio_path.read_bytes() == original
    assert not list(run_dir.glob("audio-v001.failed-*.wav"))
    assert compose_mock.call_count == 1
    assert load_state(run_dir)["status"] == "lyrics_approved"


@pytest.mark.parametrize("legacy", [False, True])
def test_resume_rejects_missing_or_changed_model_parameters(failed_audio_run, monkeypatch, legacy):
    """不替旧任务补造参数，也不能复用参数发生变化的任务。"""
    run_dir, compose_mock = failed_audio_run
    audio_path = run_dir / "audio-v001.wav"
    generate_audio(audio_path, 1)
    original = audio_path.read_bytes()
    state = load_state(run_dir)
    if legacy:
        state["audio_job"].pop("model")
        state["audio_job"].pop("model_parameters")
    else:
        state["audio_job"]["model_parameters"]["frequency_hz"] = 880
    save_state(run_dir, state)
    audio_mock = Mock()
    monkeypatch.setattr(workflow, "generate_audio", audio_mock)

    with pytest.raises(ValueError, match="音频生成记录与当前输入不一致"):
        workflow.resume_creation(run_dir)
    audio_mock.assert_not_called()
    assert audio_path.read_bytes() == original
    assert not list(run_dir.glob("audio-v001.failed-*.wav"))
    assert compose_mock.call_count == 1


def test_composition_handoff_matches_actual_artifacts(failed_audio_run, monkeypatch):
    """交接清单必须指向实际产物，版本、哈希和模型参数均可校验。"""
    run_dir, compose_mock = failed_audio_run
    monkeypatch.setattr(workflow, "generate_audio", generate_audio)
    workflow.resume_creation(run_dir)
    state = load_state(run_dir)
    path = run_dir / state["composition_result_file"]
    data = path.read_bytes()
    handoff = json.loads(data)
    assert hashlib.sha256(data).hexdigest() == state["composition_result_sha256"]
    assert handoff["project_id"] == state["project_id"]
    assert handoff["path_base"] == "handoff_directory"
    assert handoff["requires_audio_review"] is True
    assert handoff["lyrics_source"] == {
        "file": state["lyrics_file"], "version": state["lyrics_version"],
        "sha256": state["lyrics_sha256"],
    }
    assert handoff["music_model"]["name"] == state["audio"]["model"]
    assert handoff["music_model"]["parameters"] == state["audio_job"]["model_parameters"]
    assert {item["kind"] for item in handoff["artifacts"]} == {"composition", "audio"}
    for artifact in handoff["artifacts"]:
        assert artifact["version"] == state[f"{artifact['kind']}_version"]
        assert artifact["sha256"] == hashlib.sha256((run_dir / artifact["file"]).read_bytes()).hexdigest()
    composition = json.loads((run_dir / state["composition_file"]).read_bytes())
    assert handoff["handoff"] == composition["handoff"]
    assert compose_mock.call_count == 1


def test_handoff_saved_before_state_can_be_reused(failed_audio_run, monkeypatch):
    """交接完成、保存最终状态前中断时，不能重复生成方案或音频。"""
    run_dir, compose_mock = failed_audio_run
    audio_mock = Mock(wraps=generate_audio)
    monkeypatch.setattr(workflow, "generate_audio", audio_mock)
    real_save = workflow.save_state

    class SimulatedCrash(BaseException):
        """模拟业务异常处理无法捕获的进程中断。"""

    def crash_before_final_state(run, state):
        if state.get("status") == "audio_ready":
            raise SimulatedCrash()
        real_save(run, state)

    monkeypatch.setattr(workflow, "save_state", crash_before_final_state)
    with pytest.raises(SimulatedCrash):
        workflow.resume_creation(run_dir)
    path = run_dir / "composition-result-v001.json"
    original = path.read_bytes()
    assert load_state(run_dir)["status"] == "lyrics_approved"

    monkeypatch.setattr(workflow, "save_state", real_save)
    workflow.resume_creation(run_dir)
    assert path.read_bytes() == original
    assert load_state(run_dir)["status"] == "audio_ready"
    assert audio_mock.call_count == compose_mock.call_count == 1
    assert len(list(run_dir.glob("composition-result-v*.json"))) == 1


def test_conflicting_handoff_is_not_overwritten(failed_audio_run, monkeypatch):
    """已有交接清单不一致时必须停止，不能覆盖或标记音频待审核成功。"""
    run_dir, compose_mock = failed_audio_run
    path = run_dir / "composition-result-v001.json"
    original = b"existing conflicting handoff"
    path.write_bytes(original)
    monkeypatch.setattr(workflow, "generate_audio", generate_audio)
    with pytest.raises(ValueError, match="交接文件与当前产物不一致"):
        workflow.resume_creation(run_dir)
    assert path.read_bytes() == original
    assert load_state(run_dir)["status"] == "lyrics_approved"
    assert compose_mock.call_count == 1


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("模拟音乐模型超时"),
        HTTPError(
            "https://mock-music.invalid/generate",
            429,
            "Too Many Requests",
            None,
            None,
        ),
    ],
    ids=["timeout", "http-429"],
)
def test_music_model_failure_does_not_advance(failed_audio_run, monkeypatch, error):
    """音乐模型超时或429时，记录失败，不推进到音频审核。"""
    run_dir, compose_mock = failed_audio_run
    audio_mock = Mock(side_effect=error)
    monkeypatch.setattr(workflow, "generate_audio", audio_mock)

    with pytest.raises(OSError) as caught:
        workflow.resume_creation(run_dir)

    assert caught.value is error
    state = load_state(run_dir)
    assert state["status"] == "lyrics_approved"
    assert state["last_error"]["stage"] == "audio"
    assert state["last_error"]["message"] == str(error)
    assert not (run_dir / "audio-v001.wav").exists()
    assert not (run_dir / "release-package").exists()
    assert audio_mock.call_count == 1
    assert compose_mock.call_count == 1


def test_empty_audio_rejected_and_recoverable(failed_audio_run, monkeypatch):
    """空音频不能通过；恢复时保留故障文件，再生成完整音频。"""
    run_dir, compose_mock = failed_audio_run

    def generate_empty_audio(output_path, duration_seconds):
        """模拟生成端返回空文件，交由真实客户端检查逻辑识别。"""
        output_path.write_bytes(b"")
        return inspect_audio(output_path, duration_seconds)

    monkeypatch.setattr(workflow, "generate_audio", generate_empty_audio)
    with pytest.raises(ValueError, match="音频文件为空"):
        workflow.resume_creation(run_dir)

    state = load_state(run_dir)
    assert state["status"] == "lyrics_approved"
    assert state["last_error"]["stage"] == "audio"
    assert not (run_dir / "composition-result-v001.json").exists()

    monkeypatch.setattr(workflow, "generate_audio", generate_audio)
    workflow.resume_creation(run_dir)
    failed_files = list(run_dir.glob("audio-v001.failed-*.wav"))
    assert len(failed_files) == 1
    assert failed_files[0].read_bytes() == b""

    state = load_state(run_dir)
    assert state["status"] == "audio_ready"
    assert "last_error" not in state
    inspected = inspect_audio(run_dir / "audio-v001.wav", 1)
    assert inspected["sha256"] == state["audio"]["sha256"]
    assert compose_mock.call_count == 1
