"""题目原始需求单验收测试，样例中的错误应保持原样。"""

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import main as cli
from music_agent.brief import load_brief


BRIEFS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "briefs"


@pytest.mark.parametrize(
    "project_id,seconds,platform,cover",
    [
        ("song_001", 210, "网易云音乐", "fixtures/covers/cover_01.png"),
        ("song_002", 180, "抖音", "fixtures/covers/cover_02.png"),
    ],
)
def test_normal_requirement_briefs_are_preserved(project_id, seconds, platform, cover):
    """两份正常需求单无需新增字段，即可校验并保留原始内容。"""
    path = BRIEFS_DIR / f"{project_id}.json"
    original = json.loads(path.read_text(encoding="utf-8"))

    result = load_brief(path)

    assert result == original
    assert result["project_id"] == project_id
    assert result["duration_seconds"] == seconds
    assert result["primary_platform"] == platform
    assert result["cover_placeholder_path"] == cover


@pytest.mark.parametrize(
    "repairs,message",
    [
        ({}, "audience"),
        ({"audience": "城市音乐听众"}, "时长冲突"),
        ({"audience": "城市音乐听众", "duration_text": "3分30秒"}, "primary_platform"),
    ],
    ids=["missing-audience", "conflicting-duration", "conflicting-platform"],
)
def test_abnormal_requirement_brief_reports_each_blocker(tmp_path, repairs, message):
    """逐个修复前置错误后，后续冲突仍必须被校验识别。"""
    data = json.loads((BRIEFS_DIR / "song_003.json").read_text(encoding="utf-8"))
    data.update(repairs)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_brief(path)


def test_abnormal_brief_cli_stops_before_creation(tmp_path, monkeypatch, capsys):
    """异常需求单必须返回失败，不请求模型，也不创建运行目录。"""
    monkeypatch.chdir(tmp_path)
    creation = Mock(side_effect=AssertionError("异常需求单不得进入创作流程"))
    monkeypatch.setattr(cli, "run_creation", creation)
    monkeypatch.setattr(
        sys, "argv", ["main.py", "run", "--brief", str(BRIEFS_DIR / "song_003.json")]
    )

    assert cli.main() == 1

    creation.assert_not_called()
    assert "audience" in capsys.readouterr().err
    assert not (tmp_path / "outputs").exists()
