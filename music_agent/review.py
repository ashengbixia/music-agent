"""人工审核：检查歌词文件，并记录审核通过结果。"""

import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
import wave
from music_agent.storage import get_lyrics_path, load_state, save_state
def approve_lyrics(run_dir, reviewer):
    """审核通过当前运行目录中的歌词。
    Args:
        run_dir: 本次运行的产物目录。
        reviewer: 实际操作审核的人员姓名。
    Raises:
        ValueError: 审核人为空、状态不允许审核或文件哈希不一致。
        OSError: 文件读取或状态保存失败。
    """
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("审核人不能为空")

    run_dir = Path(run_dir)
    state = load_state(run_dir)

    # 只有待审核的歌词才能执行本次操作。
    if state.get("status") != "lyrics_ready":
        raise ValueError("当前状态不是 lyrics_ready，不能审核歌词")

    version = state.get("lyrics_version")
    if type(version) is not int or version < 1:
        raise ValueError("缺少有效的歌词版本，请使用新版流程生成歌词")

    # 重新计算当前文件的哈希，与生成时保存的哈希比较。
    lyrics_path = get_lyrics_path(run_dir, state)
    current_hash = hashlib.sha256(lyrics_path.read_bytes()).hexdigest()
    if current_hash != state.get("lyrics_sha256"):
        raise ValueError("歌词文件发生变化或缺少哈希，不能审核通过")

    # 审核绑定到具体版本和文件哈希。
    state["lyrics_approval"] = {
        "reviewer": reviewer.strip(),
        "decision": "approved",
        "version": version,
        "sha256": current_hash,
        # 按北京时间保存，明确标注时区。
        "approved_at": datetime.now(
            timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S（北京时间）"),
    }
    state["status"] = "lyrics_approved"

    # 统一保存状态并同步报告，审核命令结束后即可查询最新进度。
    save_state(run_dir, state)

def approve_audio(run_dir, reviewer):
    """检查音频及关联产物，并保存人工审核通过记录。
    Args:
        run_dir: 本次运行的产物目录。
        reviewer: 实际审核人员姓名。
    Raises:
        ValueError: 状态、产物、审核记录或音频格式不合法。
        OSError: 文件读取或状态保存失败。
    """
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("审核人不能为空")

    run_dir = Path(run_dir)
    state = load_state(run_dir)

    if state.get("status") != "audio_ready":
        raise ValueError("当前状态不是 audio_ready，不能审核音频")

    # 再次检查歌词，避免作曲完成后歌词被修改。
    lyrics_approval = state.get("lyrics_approval")
    if not isinstance(lyrics_approval, dict):
        raise ValueError("缺少歌词审核记录")

    lyrics_hash = hashlib.sha256(
        get_lyrics_path(run_dir, state).read_bytes()
    ).hexdigest()
    if (
        lyrics_approval.get("decision") != "approved"
        or lyrics_approval.get("version") != state.get("lyrics_version")
        or lyrics_hash != state.get("lyrics_sha256")
        or lyrics_hash != lyrics_approval.get("sha256")
    ):
        raise ValueError("歌词或歌词审核记录不一致")

    # 审核同时绑定音频与作曲方案，任一文件变化都不能沿用审核。
    composition_version = state.get("composition_version")
    audio_version = state.get("audio_version")
    for version in (composition_version, audio_version):
        if type(version) is not int or version < 1:
            raise ValueError("作曲或音频版本不合法")

    composition_path = run_dir / f"composition-v{composition_version:03d}.json"
    composition_hash = hashlib.sha256(
        composition_path.read_bytes()
    ).hexdigest()
    if composition_hash != state.get("composition_sha256"):
        raise ValueError("作曲方案发生变化")

    audio_info = state.get("audio")
    if not isinstance(audio_info, dict):
        raise ValueError("缺少音频元数据")

    audio_path = run_dir / f"audio-v{audio_version:03d}.wav"
    audio_hash = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    if audio_hash != audio_info.get("sha256"):
        raise ValueError("音频文件发生变化")

    # 完整读取采样数据，避免仅能打开文件头的截断 WAV 通过检查。
    try:
        with wave.open(str(audio_path), "rb") as audio:
            frame_count = audio.getnframes()
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            expected_size = frame_count * channels * audio.getsampwidth()
            data = audio.readframes(frame_count)
    except (wave.Error, EOFError) as error:
        raise ValueError("音频格式无效或文件损坏") from error

    if frame_count <= 0 or len(data) != expected_size:
        raise ValueError("音频为空或采样数据不完整")
    if (
        audio_info.get("format") != "wav"
        or channels != audio_info.get("channels")
        or sample_rate != audio_info.get("sample_rate")
        or frame_count / sample_rate != audio_info.get("duration_seconds")
    ):
        raise ValueError("音频实际参数与记录不一致")

    state["audio_approval"] = {
        "reviewer": reviewer.strip(),
        "decision": "approved",
        "version": audio_version,
        "sha256": audio_hash,
        "composition_version": composition_version,
        "composition_sha256": composition_hash,
        "lyrics_version": state["lyrics_version"],
        "lyrics_sha256": lyrics_hash,
        "approved_at": datetime.now(
            timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S（北京时间）"),
    }
    state["status"] = "audio_approved"
    save_state(run_dir, state)

def reject_lyrics(run_dir, reviewer, feedback):
    """拒绝当前待审核歌词，并保存修改意见。
    Args:
        run_dir: 本次运行的产物目录。
        reviewer: 审核人员姓名。
        feedback: 明确的修改意见。
    Raises:
        ValueError: 输入、状态、版本或文件哈希不合法。
        OSError: 文件读取或状态保存失败。
    """
    for value, label in (
        (reviewer, "审核人"),
        (feedback, "修改意见"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}不能为空")

    run_dir = Path(run_dir)
    state = load_state(run_dir)
    if state.get("status") != "lyrics_ready":
        raise ValueError("只能拒绝当前待审核的歌词")

    version = state.get("lyrics_version")
    if type(version) is not int or version < 1:
        raise ValueError("歌词版本不合法")

    current_hash = hashlib.sha256(
        get_lyrics_path(run_dir, state).read_bytes()
    ).hexdigest()
    if current_hash != state.get("lyrics_sha256"):
        raise ValueError("歌词文件发生变化，无法绑定当前审核")

    history = state.get("review_history", [])
    if not isinstance(history, list):
        raise ValueError("审核历史格式不合法")

    record = {
        "stage": "lyrics",
        "decision": "rejected",
        "reviewer": reviewer.strip(),
        "feedback": feedback.strip(),
        "version": version,
        "sha256": current_hash,
        "reviewed_at": datetime.now(
            timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S（北京时间）"),
    }

    # 保存历史，后续修改歌词不能覆盖本次拒绝记录。
    state["review_history"] = [*history, record]
    state["lyrics_rejection"] = record
    state["status"] = "lyrics_rejected"
    save_state(run_dir, state)
