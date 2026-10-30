"""本地产物目录的查找与访问。"""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from music_agent.reporting import write_run_report
def get_lyrics_path(run_dir, state):
    """获取状态记录指向的当前歌词文件，不读取文件内容。

    Args:
        run_dir: 本次运行的产物目录。
        state: 已加载的状态字典。

    Returns:
        当前歌词文件路径。

    Raises:
        ValueError: 歌词文件名缺失或包含目录路径。
    """
    filename = state.get("lyrics_file")
    if (
        not isinstance(filename, str)
        or not filename.strip()
        or Path(filename).is_absolute()
        or Path(filename).name != filename
    ):
        raise ValueError("lyrics_file 必须是运行目录内的文件名")

    return Path(run_dir) / filename


def find_run_directory(project_id, run_id=None):
    """查找项目对应的运行目录。
    Args:
        project_id: 项目编号。
        run_id: 可选的运行目录名，用于区分同一项目的多次运行。
    Returns:
        唯一匹配的运行目录。
    Raises:
        ValueError: 状态格式错误、未找到项目或存在多个匹配。
        OSError: 状态文件读取失败。
    """
    matches = []
    for state_path in Path("outputs").glob("run-*/state.json"):
        if run_id and state_path.parent.name != run_id:
            continue
        with state_path.open(encoding="utf-8") as file:
            state = json.load(file)
        if not isinstance(state, dict):
            raise ValueError(f"状态文件格式错误：{state_path}")
        if state.get("project_id") == project_id:
            matches.append(state_path.parent)

    if not matches:
        raise ValueError("未找到对应项目的运行记录")
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise ValueError(f"找到多次运行，请用 --run 指定其中一个：{names}")

    return matches[0]

def load_state(run_dir):
    """读取运行状态。
    Args:
        run_dir: 本次运行的产物目录。
    Returns:
        状态信息字典。
    Raises:
        ValueError: JSON 或状态结构不合法。
        OSError: 文件读取失败。
    """
    state_path = Path(run_dir) / "state.json"
    with state_path.open(encoding="utf-8") as file:
        state = json.load(file)
    if not isinstance(state, dict):
        raise ValueError("状态文件必须是 JSON 对象")
    return state
def save_state(run_dir, state):
    """原子替换状态文件，再用同一份数据更新运行报告。
    Args:
        run_dir: 本次运行的产物目录。
        state: 要保存的状态字典。
    Raises:
        ValueError: 已有运行报告格式不合法。
        OSError: 状态或报告文件写入、替换失败。
    """
    run_dir = Path(run_dir)
    temporary_path = run_dir / f"state-{uuid4().hex}.tmp"
    try:
        with temporary_path.open("x", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        temporary_path.replace(run_dir / "state.json")
        # 状态成功保存后再更新报告，报告不反过来依赖存储模块。
        write_run_report(run_dir, state)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_new_artifact(path, data):
    """完整写入新产物，不覆盖已有文件。

    Args:
        path: 目标文件路径。
        data: 要保存的字节内容。

    Raises:
        FileExistsError: 目标已经存在。
        OSError: 写入或改名失败。
    """
    path = Path(path)
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(data)
        if path.exists():
            raise FileExistsError(f"目标文件已存在：{path}")
        # 当前 Windows 环境下，rename 不会覆盖已存在的目标文件。
        temporary.rename(path)
    finally:
        temporary.unlink(missing_ok=True)


def make_model_response_recorder(run_dir, project_id, stage, version=1):
    """创建模型记录回调，每次响应或请求失败独立保存，绝不覆盖历史。

    Args:
        run_dir: 本次运行的产物目录。
        project_id: 项目编号。
        stage: plan、lyrics 或 composition。
        version: 本次尝试生成的产物版本。

    Returns:
        接收调用记录字典并将其保存的回调函数。

    Raises:
        ValueError: 阶段或版本不合法。
    """
    if stage not in {"plan", "lyrics", "composition"}:
        raise ValueError("模型调用记录阶段不合法")
    if type(version) is not int or version < 1:
        raise ValueError("模型调用记录版本不合法")
    records_dir = Path(run_dir) / "model-calls"

    def record_response(record):
        """收到回调时才创建目录，复用已有产物不会生成空记录。"""
        trace_id = uuid4().hex
        document = {
            "trace_id": trace_id,
            "project_id": project_id,
            "stage": stage,
            "artifact_version": version,
            "response": record,
        }
        data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        records_dir.mkdir(parents=True, exist_ok=True)
        path = records_dir / f"{stage}-v{version:03d}-{trace_id}.json"
        write_new_artifact(path, data)

    return record_response


def calculate_brief_fingerprint(brief):
    """计算需求内容指纹，忽略 JSON 排版和字段顺序。

    Args:
        brief: 已解析的需求单字典。

    Returns:
        规范化 JSON 内容的 SHA-256。
    """
    data = json.dumps(
        brief,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def find_existing_run(project_id, fingerprint):
    """查找可以复用的同需求运行记录。

    Args:
        project_id: 项目编号。
        fingerprint: 当前需求内容指纹。

    Returns:
        唯一可复用的目录；没有已有项目时返回 None。

    Raises:
        ValueError: 项目存在冲突、缺失快照或有多个运行记录。
        OSError: 文件读取失败。
    """
    matches = []
    for state_path in Path("outputs").glob("run-*/state.json"):
        state = load_state(state_path.parent)
        if state.get("project_id") != project_id:
            continue

        brief_path = state_path.parent / "brief.json"
        if not brief_path.is_file():
            raise ValueError("已有项目缺少需求单快照，无法判断是否可复用")
        with brief_path.open(encoding="utf-8-sig") as file:
            saved_brief = json.load(file)
        if not isinstance(saved_brief, dict):
            raise ValueError("已有项目的需求单快照格式不合法")

        saved_fingerprint = calculate_brief_fingerprint(saved_brief)
        # 兼容旧项目；新项目若保存过指纹，则必须与快照一致。
        recorded = state.get("brief_fingerprint")
        if recorded is not None and recorded != saved_fingerprint:
            raise ValueError("已有项目的需求单快照发生变化")
        if saved_fingerprint != fingerprint:
            raise ValueError(
                f"项目 {project_id} 已存在，但需求内容不同，不能直接复用"
            )
        matches.append(state_path.parent)

    if len(matches) > 1:
        raise ValueError("同一项目存在多个运行记录，请先明确保留的项目")
    return matches[0] if matches else None
