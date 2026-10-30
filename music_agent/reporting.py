"""根据已保存的状态生成运行报告，不调用模型或重复读取产物。"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def write_run_report(run_dir, state):
    """更新项目运行报告，保留状态变化及失败历史。

    Args:
        run_dir: 本次运行的产物目录。
        state: 已成功保存的状态快照。

    Raises:
        ValueError: 已有报告格式或项目编号不合法。
        OSError: 报告读取、写入或替换失败。
    """
    run_dir = Path(run_dir)
    report_path = run_dir / "run_report.json"
    now = datetime.now(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S（北京时间）"
    )

    history = []
    if report_path.exists():
        with report_path.open(encoding="utf-8") as file:
            previous = json.load(file)
        if (
            not isinstance(previous, dict)
            or previous.get("project_id") != state["project_id"]
            or not isinstance(previous.get("history"), list)
        ):
            raise ValueError("已有运行报告格式或项目编号不合法")
        history = previous["history"]
        if not all(
            isinstance(event, dict)
            and isinstance(event.get("progress"), dict)
            and isinstance(event.get("recorded_at"), str)
            for event in history
        ):
            raise ValueError("已有运行报告历史格式不合法")

    # 使用调用方的状态快照，不再读取 state.json 或音频等产物。
    progress = {
        "status": state["status"],
        "versions": {
            stage: state.get(f"{stage}_version")
            for stage in ("lyrics", "composition", "audio", "package")
        },
        "checkpoints": {
            stage: {
                "file": state.get(f"{stage}_file"),
                "sha256": state.get(f"{stage}_sha256"),
            }
            for stage in ("plan", "lyrics", "composition")
        },
        "audio": state.get("audio"),
        "composition_result_file": state.get("composition_result_file"),
        "composition_result_sha256": state.get("composition_result_sha256"),
        "last_error": state.get("last_error"),
        "lyrics_rejection": state.get("lyrics_rejection"),
        "lyrics_approval": state.get("lyrics_approval"),
        "audio_approval": state.get("audio_approval"),
        "package_directory": state.get("package_directory"),
        "package_missing_info": state.get("package_missing_info", []),
    }
    # 重复保存相同进度不重复追加事件，但仍更新模型记录引用。
    if not history or history[-1]["progress"] != progress:
        history.append({"recorded_at": now, "progress": progress})

    records = sorted(
        f"model-calls/{path.name}"
        for path in (run_dir / "model-calls").glob("*.json")
    )
    report = {
        "project_id": state["project_id"],
        "updated_at": now,
        "current_status": state["status"],
        "latest_progress": progress,
        "history": history,
        # 这里是记录数，不是包含重试在内的 HTTP 请求次数。
        "model_call_record_count": len(records),
        "model_call_records": records,
        "note": "本报告记录流程状态，不替代产物及发行包的完整性校验。",
    }

    temporary = run_dir / f".run-report-{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
        temporary.replace(report_path)
    finally:
        temporary.unlink(missing_ok=True)
