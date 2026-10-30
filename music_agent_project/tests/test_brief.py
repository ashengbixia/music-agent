"""需求单校验测试，不调用模型或创建歌曲项目。"""

import json

import pytest

from music_agent.brief import load_brief


@pytest.fixture
def brief():
    """提供一份正常的需求单，各测试独立使用。"""
    return {
        "project_id": "song_test",
        "theme": "毕业告别",
        "genre": "流行摇滚",
        "mood": "温暖",
        "duration_seconds": 210,
        "duration_text": "3分30秒",
        "audience": "毕业生",
        "lyrics_language": "中文",
        "platform": ["网易云音乐", "抖音"],
        "primary_platform": "网易云音乐",
        "allow_sampling": False,
        "references": ["校园乐队"],
        "constraints": ["原创", "不使用版权采样"],
        "cover_placeholder_path": "fixtures/covers/cover_01.png",
    }


@pytest.fixture
def save_brief(tmp_path):
    """将测试需求单写入临时文件，返回文件路径。"""
    def save(data):
        path = tmp_path / "brief.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    return save


@pytest.mark.parametrize(
    "seconds,text",
    [
        (210, "3分30秒"),
        (180, "3分钟"),
        (210, "210秒"),
    ],
)
def test_valid_brief(brief, save_brief, seconds, text):
    """支持的时长格式与秒数一致时，应通过且保留原始数据。"""
    brief["duration_seconds"] = seconds
    brief["duration_text"] = text

    assert load_brief(save_brief(brief)) == brief


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"duration_seconds": True}, "duration_seconds"),
        ({"duration_text": "2分45秒"}, "时长冲突"),
        ({"primary_platform": "QQ音乐"}, "primary_platform"),
        ({"platform": []}, "platform"),
        ({"audience": "   "}, "audience"),
        ({"allow_sampling": "false"}, "allow_sampling"),
        ({"theme": None}, "theme"),
        ({"references": {"name": "参考"}}, "references"),
    ],
)
def test_invalid_brief_rejected(brief, save_brief, changes, message):
    """冲突字段、错误类型及空文本必须被拒绝。"""
    brief.update(changes)

    with pytest.raises(ValueError, match=message):
        load_brief(save_brief(brief))


def test_missing_audience_rejected(brief, save_brief):
    """缺少必填受众字段时，应明确报告。"""
    brief.pop("audience")

    with pytest.raises(ValueError, match="audience"):
        load_brief(save_brief(brief))


def test_missing_cover_allows_creation(brief, save_brief):
    """缺少封面不阻止创作，缺项由后续打包流程处理。"""
    brief.pop("cover_placeholder_path")

    result = load_brief(save_brief(brief))

    assert "cover_placeholder_path" not in result
    assert result["project_id"] == brief["project_id"]


def test_invalid_json_rejected(tmp_path):
    """非法 JSON 不得作为需求单继续执行。"""
    path = tmp_path / "invalid.json"
    path.write_text('{"project_id":', encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        load_brief(path)