"""模型输出的 JSON Schema 定义。"""

LYRICS_SCHEMA = {
    "type": "object",
    "required": ["project_name", "title", "sections"],
    "additionalProperties": False,
    "properties": {
        # 创作项目名称，与项目编号、歌曲标题分别保存。
        "project_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40,
        },
        "title": {"type": "string", "minLength": 1},
        # 数组顺序就是歌曲的段落顺序，每段携带情绪标记。
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "emotion", "lines"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "emotion": {"type": "string", "minLength": 1},
                    "lines": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    },
}

# 作曲方案必须包含节奏、调性，以及按演唱顺序排列的段落编曲。
COMPOSITION_SCHEMA = {
    "type": "object",
    "required": ["bpm", "key", "sections", "handoff"],
    "additionalProperties": False,
    "properties": {
        "bpm": {"type": "integer", "minimum": 30, "maximum": 300},
        "key": {"type": "string", "minLength": 1},
        "handoff": {"type": "string", "minLength": 1},
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "duration_seconds", "arrangement"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "duration_seconds": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "arrangement": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}