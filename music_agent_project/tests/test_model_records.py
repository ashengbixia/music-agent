"""模型调用记录测试：模拟接口，不发送真实请求。"""

import hashlib
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from music_agent import workflow
from music_agent.clients import deepseek
from music_agent.planner import STAGES
from music_agent.review import approve_lyrics, reject_lyrics
from music_agent.storage import find_run_directory, load_state


def response_for(document, response_id="test-response", finish_reason="stop"):
    """构造与业务产物结构无关的接口响应信封。"""
    content = document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
    return {
        "id": response_id,
        "model": "test-returned-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
    }


@pytest.fixture
def recorded_client(monkeypatch):
    """隔离密钥及网络访问，仅允许读取模拟响应。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-must-not-be-recorded")
    monkeypatch.setenv("DEEPSEEK_MODEL", "test-requested-model")
    send = Mock()
    monkeypatch.setattr(deepseek, "send_request", send)
    return send


def test_response_metadata_contains_parameters_and_raw_summary(recorded_client):
    """参数来自实际请求，原始内容哈希和摘要不包含提示词或密钥。"""
    raw = '{ "answer": "测试结果" }'
    recorded_client.return_value = response_for(raw)
    records = []
    result = deepseek.request_json(
        "private-system-prompt", "private-user-prompt", on_response=records.append
    )
    assert result == {"answer": "测试结果"}
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "response_received"
    assert record["parameters"]["max_tokens"] == 8192
    assert record["parameters"]["response_format"] == {"type": "json_object"}
    assert record["requested_model"] == "test-requested-model"
    assert record["returned_model"] == "test-returned-model"
    assert record["usage"]["completion_tokens"] == 20
    assert record["elapsed_seconds"] >= 0
    assert record["response_summary"] == {
        "characters": len(raw), "preview": raw,
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }
    saved = json.dumps(record)
    for secret in ("test-secret-must-not-be-recorded", "private-system-prompt", "private-user-prompt"):
        assert secret not in saved


@pytest.mark.parametrize("content,finish", [("not json", "stop"), ("", "stop"), ("{}", "length")])
def test_rejected_content_still_has_response_record(recorded_client, content, finish):
    """响应内容不合法或被截断时，仍保存已收到的原始摘要。"""
    recorded_client.return_value = response_for(content, finish_reason=finish)
    records = []
    with pytest.raises(ValueError):
        deepseek.request_json("JSON", "test", on_response=records.append)
    assert len(records) == 1
    assert records[0]["status"] == "response_received"
    assert records[0]["finish_reason"] == finish
    assert records[0]["response_summary"]["preview"] == content


@pytest.mark.parametrize("envelope", [[], {}, {"choices": []}])
def test_invalid_envelope_records_failure(recorded_client, envelope):
    """异常响应结构也留下记录，但不能返回业务结果。"""
    recorded_client.return_value = envelope
    records = []
    with pytest.raises(ValueError, match="响应结构不合法"):
        deepseek.request_json("JSON", "test", on_response=records.append)
    assert len(records) == 1
    assert records[0]["status"] == "invalid_response"


def test_network_failure_does_not_record_exception_body(recorded_client):
    """请求失败记录错误类型，不保留可能含敏感信息的异常正文。"""
    recorded_client.side_effect = TimeoutError("test-secret-must-not-be-recorded")
    records = []
    with pytest.raises(TimeoutError):
        deepseek.request_json("JSON", "test", on_response=records.append)
    assert records[0]["status"] == "request_failed"
    assert records[0]["error_type"] == "TimeoutError"
    assert "test-secret-must-not-be-recorded" not in json.dumps(records)


def test_record_write_failure_is_not_silenced(recorded_client):
    """记录无法保存时必须报错，不能悄悄继续交付未留痕结果。"""
    recorded_client.return_value = response_for({"ok": True})
    callback = Mock(side_effect=OSError("模拟记录写入失败"))
    with pytest.raises(OSError, match="记录写入失败"):
        deepseek.request_json("JSON", "test", on_response=callback)
    assert recorded_client.call_count == callback.call_count == 1


@pytest.fixture
def trace_workflow(tmp_path, monkeypatch, recorded_client):
    """通过真实 Agent 和客户端代码串联记录，仅替换接口传输层。"""
    monkeypatch.chdir(tmp_path)
    brief = {
        "project_id": "trace_test", "theme": "毕业告别", "genre": "流行",
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
    composition = {
        "bpm": 100, "key": "C",
        "sections": [{"name": "主歌", "duration_seconds": 1, "arrangement": "钢琴"}],
        "handoff": "mock 测试",
    }
    return brief, plan, lyrics, composition, recorded_client


def read_records(run):
    """读取独立记录文件，不依赖文件系统返回顺序。"""
    return [json.loads(path.read_text(encoding="utf-8")) for path in (run / "model-calls").glob("*.json")]


def test_all_agents_persist_records_without_changing_lyrics_schema(trace_workflow):
    """计划、作词、作曲均留痕，复用产物时不产生额外记录。"""
    brief, plan, lyrics, composition, send = trace_workflow
    send.side_effect = [response_for(item) for item in (plan, lyrics, composition)]
    run = workflow.run_creation(brief)
    assert json.loads((run / "lyrics.json").read_bytes()) == lyrics
    assert workflow.run_creation(brief) == run
    assert len(read_records(run)) == send.call_count == 2
    approve_lyrics(run, "测试审核人")
    workflow.resume_creation(run)

    records = read_records(run)
    assert {record["stage"] for record in records} == {"plan", "lyrics", "composition"}
    assert len(records) == send.call_count == 3
    assert len({record["trace_id"] for record in records}) == 3
    assert all(record["artifact_version"] == 1 for record in records)
    assert all(record["project_id"] == brief["project_id"] for record in records)
    report = json.loads((run / "run_report.json").read_text(encoding="utf-8"))
    assert report["current_status"] == "audio_ready"
    assert report["model_call_record_count"] == 3
    assert all((run / path).is_file() for path in report["model_call_records"])


def test_lyrics_revision_records_target_version(trace_workflow):
    """新版歌词记录对应新版号，旧记录不覆盖。"""
    brief, plan, lyrics, _, send = trace_workflow
    revised = deepcopy(lyrics)
    revised["sections"][0]["lines"] = ["增加了毕业场景"]
    send.side_effect = [response_for(item) for item in (plan, lyrics, revised)]
    run = workflow.run_creation(brief)
    reject_lyrics(run, "测试审核人", "增加毕业场景")
    workflow.resume_creation(run)
    versions = sorted(record["artifact_version"] for record in read_records(run) if record["stage"] == "lyrics")
    assert versions == [1, 2]
    assert load_state(run)["lyrics_file"] == "lyrics-v002.json"


def test_failed_request_record_survives_initial_resume(trace_workflow):
    """失败及恢复请求分别保留，不伪造成功响应或覆盖失败记录。"""
    brief, plan, lyrics, _, send = trace_workflow
    send.side_effect = [TimeoutError("模拟超时"), response_for(plan), response_for(lyrics)]
    with pytest.raises(TimeoutError):
        workflow.run_creation(brief)
    run = find_run_directory(brief["project_id"])
    failed_record = read_records(run)[0]
    workflow.resume_creation(run)
    records = read_records(run)
    assert failed_record in records
    assert len(records) == 3
    assert sorted(record["response"]["status"] for record in records) == [
        "request_failed", "response_received", "response_received",
    ]
    assert load_state(run)["status"] == "lyrics_ready"
