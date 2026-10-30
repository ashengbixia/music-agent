"""最终验收测试：非法方案、防覆盖及题目正常样例完整流程。"""

import json
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

from music_agent import packaging, workflow
from music_agent.brief import load_brief
from music_agent.clients.mock_music import generate_audio
from music_agent.review import approve_audio, approve_lyrics
from music_agent.storage import load_state

# 复用现有测试场景，不重复搭建模型和审核产物。
from tests.test_packaging import approved_run
from tests.test_review_gates import review_scenario


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    """禁止本文件内的测试建立真实 TCP 连接。"""
    denied = Mock(side_effect=AssertionError("验收测试禁止真实网络连接"))
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    yield denied
    denied.assert_not_called()


@pytest.mark.parametrize("error", ["bpm", "missing_key", "order", "duration"])
def test_invalid_composition_stops_before_audio(review_scenario, error):
    """非法作曲方案必须停在歌词已审核状态，不能生成音频。"""
    brief, _, lyrics, documents, send, _ = review_scenario

    # 使用两个不同段落，才能检测段落顺序错误。
    brief["duration_seconds"] = 2
    lyrics["sections"].append({
        "name": "副歌",
        "emotion": "温暖",
        "lines": ["约好下一次见面"],
    })
    composition = documents[2]
    composition["sections"].append({
        "name": "副歌",
        "duration_seconds": 1,
        "arrangement": "吉他",
    })

    if error == "bpm":
        composition["bpm"] = 500
    elif error == "missing_key":
        composition.pop("key")
    elif error == "order":
        composition["sections"].reverse()
    else:
        composition["sections"][0]["duration_seconds"] = 2

    run_dir = workflow.run_creation(brief)
    approve_lyrics(run_dir, "测试审核人")

    with pytest.raises(ValueError, match="作曲方案"):
        workflow.resume_creation(run_dir)

    state = load_state(run_dir)
    assert state["status"] == "lyrics_approved"
    assert state["last_error"]["stage"] == "composition"
    assert not (run_dir / "composition-v001.json").exists()
    assert not (run_dir / "audio-v001.wav").exists()
    assert not (run_dir / "release-package").exists()
    assert send.call_count == 3


def test_approved_audio_cannot_be_overwritten(approved_run):
    """底层生成和流程恢复均不能覆盖已经审核通过的音频。"""
    run_dir = approved_run

    def snapshot():
        """保存运行目录内全部文件的字节，检查是否发生修改。"""
        return {
            path.relative_to(run_dir): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }

    before = snapshot()

    # 底层客户端也必须保护已存在的文件。
    with pytest.raises(FileExistsError):
        generate_audio(run_dir / "audio-v001.wav", 1)

    # 已通过音频审核的项目不能重新进入创作恢复流程。
    with pytest.raises(ValueError):
        workflow.resume_creation(run_dir)

    assert snapshot() == before
    assert load_state(run_dir)["status"] == "audio_approved"


@pytest.mark.parametrize("project_id", ["song_001", "song_002"])
def test_normal_brief_complete_workflow(review_scenario, project_id):
    """题目正常需求单应完成全部流程，生成可校验的本地物料包。"""
    _, plan, lyrics, documents, send, network = review_scenario
    brief = load_brief(
        PROJECT_ROOT / "fixtures" / "briefs" / f"{project_id}.json"
    )

    # 按样例调整模拟响应，保留需求单原始字段和目标时长。
    plan["project_id"] = project_id
    lyrics["project_name"] = f"测试企划{project_id}"
    lyrics["title"] = f"测试歌曲{project_id}"
    lyrics["sections"][0]["emotion"] = brief["mood"]
    lyrics["sections"][0]["lines"] = [brief["theme"]]
    documents[2]["sections"][0]["duration_seconds"] = brief["duration_seconds"]

    run_dir = workflow.run_creation(brief)
    assert load_state(run_dir)["status"] == "lyrics_ready"

    approve_lyrics(run_dir, "测试歌词审核人")
    workflow.resume_creation(run_dir)
    assert load_state(run_dir)["status"] == "audio_ready"

    approve_audio(run_dir, "测试音频审核人")
    package = packaging.export_package(run_dir)

    # 核对所有包内文件哈希、音频参数及审核绑定。
    manifest = packaging._read_verified_package(package)
    assert manifest["status"] == "ready"
    assert manifest["project_id"] == project_id
    assert manifest["audio"]["duration_seconds"] == brief["duration_seconds"]
    assert load_state(run_dir)["status"] == "package_ready"

    report = json.loads(
        (run_dir / "run_report.json").read_text(encoding="utf-8")
    )
    assert report["current_status"] == "package_ready"
    assert report["model_call_record_count"] == 3
    assert send.call_count == 3
    network.assert_not_called()
