"""读取歌曲需求单并校验基础字段。"""
import json
import re
from pathlib import Path


def load_brief(path):
    """读取 JSON 需求单并校验基础字段。
    Args:
        path: JSON 需求单的文件路径，支持字符串或 Path 对象。
    Returns:
        通过基础字段校验的需求单字典；保留其他字段供后续流程使用。
    Raises:
        OSError: 文件不存在、无法访问或读取失败。
        UnicodeError: 文件不是有效的 UTF-8 文本。
        ValueError: JSON 格式错误、顶层不是对象或基础字段不合法。
    """
    # 兼容带 BOM 的 UTF-8 文件，避免部分 Windows 编辑器保存的文件解析失败。
    with Path(path).open(encoding="utf-8-sig") as file:
        brief = json.load(file)

    if not isinstance(brief, dict):
        raise ValueError("需求单必须是一个 JSON 对象")

    for field in (
            "project_id", "theme", "genre", "mood",
            "audience", "lyrics_language", "primary_platform",
    ):
        value = brief.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"字段 {field} 必须是非空文本")

    duration = brief.get("duration_seconds")
    # bool 是 int 的子类，因此使用精确类型判断，拒绝将 true 当作 1 秒。
    if type(duration) is not int or duration <= 0:
        raise ValueError("duration_seconds 必须是正整数（秒）")

    # 两种时长同时提供时，必须一致；不能让模型自行选择一个。
    if "duration_text" in brief:
        text_duration = parse_duration_text(brief["duration_text"])
        if text_duration != duration:
            raise ValueError(
                f"时长冲突：duration_seconds={duration}，"
                f"duration_text 换算后为 {text_duration} 秒"
            )

    platforms = brief.get("platform")
    if (
        not isinstance(platforms, list)
        or not platforms
        or not all(
            isinstance(item, str) and item.strip()
            for item in platforms
        )
    ):
        raise ValueError("platform 必须是非空的平台名称列表")

    if brief["primary_platform"] not in platforms:
        raise ValueError("primary_platform 必须包含在 platform 列表中")

    # 采样许可不能使用字符串“false”或整数0代替布尔值。
    if type(brief.get("allow_sampling")) is not bool:
        raise ValueError("allow_sampling 必须是布尔值 true 或 false")

    for field in ("references", "constraints"):
        if field in brief:
            values = brief[field]
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item.strip()
                for item in values
            ):
                raise ValueError(f"{field} 必须是非空文本组成的列表")

    # 封面缺失不阻止创作，由打包阶段记录为 needs_info。
    return brief

def parse_duration_text(text):
    """将中文时长转换为秒。
    Args:
        text: 例如“3分30秒”“3分钟”或“210秒”。
    Returns:
        正整数秒数。
    Raises:
        ValueError: 时长不是支持的文本格式或不大于零。
    """
    if not isinstance(text, str):
        raise ValueError("duration_text 必须是文本")
    match = re.fullmatch(
        r"(?:(\d+)分(?:钟)?)?(?:(\d+)秒)?",
        text.strip(),
    )
    if not match or not any(match.groups()):
        raise ValueError("时长文本应类似 3分30秒、3分钟或210秒")
    minutes, seconds = match.groups()
    total = int(minutes or 0) * 60 + int(seconds or 0)
    if total <= 0:
        raise ValueError("时长必须大于零")

    return total