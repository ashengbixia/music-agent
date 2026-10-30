"""Mock 音乐模型：生成 WAV 测试音频并返回文件元数据。"""

import hashlib
import math
import struct
import wave
from pathlib import Path
from io import BytesIO

MOCK_MODEL_NAME = "local-sine-mock"


def get_mock_model_parameters(duration_seconds):
    """返回本地 mock 音频生成参数。

    Args:
        duration_seconds: 正整数秒数。

    Returns:
        本次生成使用的参数字典。

    Raises:
        ValueError: 时长不是正整数。
    """
    if type(duration_seconds) is not int or duration_seconds <= 0:
        raise ValueError("音频时长必须是正整数")

    return {
        "duration_seconds": duration_seconds,
        "sample_rate": 22050,
        "channels": 1,
        "sample_width_bytes": 2,
        "frequency_hz": 440,
        "amplitude": 2000,
    }


def generate_audio(output_path, duration_seconds):
    """生成指定时长的单声道测试音频。
    Args:
        output_path: 输出 WAV 文件路径。
        duration_seconds: 音频时长，单位为秒，必须为正整数。
    Returns:
        包含文件元数据、哈希、模型名称及实际生成参数的字典。
    Raises:
        ValueError: 时长不是正整数。
        FileExistsError: 输出文件已经存在。
        OSError: 目录创建或音频读写失败。
        wave.Error: WAV 文件处理失败。
    """
    parameters = get_mock_model_parameters(duration_seconds)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sample_rate = parameters["sample_rate"]
    frequency = parameters["frequency_hz"]
    amplitude = parameters["amplitude"]

    # 构造一秒的低音量正弦测试音。
    # WAV 的 16 位 PCM 采样使用小端有符号整数。
    samples = [
        int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
        for i in range(sample_rate)
    ]
    one_second = struct.pack(f"<{sample_rate}h", *samples)

    # 独占创建文件，保护已有音频；逐秒写入以限制内存占用。
    with output_path.open("xb") as file:
        with wave.open(file, "wb") as audio:
            audio.setnchannels(parameters["channels"])
            audio.setsampwidth(parameters["sample_width_bytes"])
            audio.setframerate(sample_rate)

            for _ in range(parameters["duration_seconds"]):
                audio.writeframes(one_second)

    # 生成完成后使用公共检查函数，统一检查格式、完整性和时长。
    info = inspect_audio(output_path, parameters["duration_seconds"])
    info.update({
        "is_mock": True,
        "model": MOCK_MODEL_NAME,
        "model_parameters": parameters,
    })
    return info

def inspect_audio(audio_path, expected_duration=None):
    """检查 WAV 是否完整，并提取实际元数据。
    Args:
        audio_path: 待检查的音频路径。
        expected_duration: 可选的预期时长，单位为秒。
    Returns:
        音频路径、格式、采样率、声道、时长和哈希。
    Raises:
        ValueError: 文件为空、格式无效、数据不完整或时长不符。
        OSError: 文件读取失败。
    """
    audio_path = Path(audio_path)

    # 一次读取文件，解析与哈希计算共用同一份字节。
    data = audio_path.read_bytes()
    return {"file": str(audio_path), **inspect_audio_bytes(data, expected_duration)}


def inspect_audio_bytes(data, expected_duration=None):
    """从已有 WAV 字节中检查完整性并提取参数，不再读取文件。

    Args:
        data: 已读取的音频文件字节。
        expected_duration: 可选的预期时长，单位为秒。

    Returns:
        格式、采样率、声道、时长和 SHA-256，不包含文件路径。

    Raises:
        ValueError: 文件为空、格式无效、数据不完整或时长不符。
    """
    if not data:
        raise ValueError("音频文件为空")

    try:
        with wave.open(BytesIO(data), "rb") as audio:
            frame_count = audio.getnframes()
            sample_rate = audio.getframerate()
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            frames = audio.readframes(frame_count)
    except (wave.Error, EOFError) as error:
        raise ValueError("音频格式无效或文件损坏") from error

    if frame_count <= 0 or sample_rate <= 0:
        raise ValueError("音频没有有效采样数据")

    expected_size = frame_count * channels * sample_width
    if len(frames) != expected_size:
        raise ValueError("音频采样数据不完整")

    duration = frame_count / sample_rate
    if expected_duration is not None and duration != expected_duration:
        raise ValueError(
            f"音频时长不符：预期 {expected_duration} 秒，实际 {duration} 秒"
        )

    # 检查文件不能证明其制作来源，因此这里不设置 is_mock。
    return {
        "format": "wav",
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_seconds": duration,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
