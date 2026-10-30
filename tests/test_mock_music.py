"""Mock 模型参数与实际 WAV 一致性测试。"""

import hashlib
import wave

import pytest

from music_agent.clients.mock_music import (
    MOCK_MODEL_NAME,
    generate_audio,
    inspect_audio,
)


def test_generated_parameters_match_actual_wav(tmp_path):
    """记录的生成参数必须与文件实际参数一致，检查器不推断制作来源。"""
    path = tmp_path / "audio.wav"
    info = generate_audio(path, 1)
    parameters = info["model_parameters"]
    assert info["model"] == MOCK_MODEL_NAME
    assert info["is_mock"] is True
    with wave.open(str(path), "rb") as audio:
        assert audio.getframerate() == parameters["sample_rate"]
        assert audio.getnchannels() == parameters["channels"]
        assert audio.getsampwidth() == parameters["sample_width_bytes"]
        assert audio.getnframes() / audio.getframerate() == parameters["duration_seconds"]
    assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    inspected = inspect_audio(path, 1)
    assert inspected["sha256"] == info["sha256"]
    assert not {"model", "model_parameters", "is_mock"}.intersection(inspected)


@pytest.mark.parametrize("duration", [True, 0, -1, "1"])
def test_invalid_duration_does_not_create_artifact(tmp_path, duration):
    """参数不合法时，应在创建文件前拒绝。"""
    path = tmp_path / "audio.wav"
    with pytest.raises(ValueError, match="时长必须是正整数"):
        generate_audio(path, duration)
    assert not path.exists()
