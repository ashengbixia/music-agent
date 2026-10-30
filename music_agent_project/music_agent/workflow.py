"""音乐创作流程：生成计划、生成歌词，并停在人工审核前。"""

import json
from pathlib import Path
from uuid import uuid4
import hashlib

from music_agent.agents.lyricist import generate_lyrics, validate_lyrics
from music_agent.agents.composer import (
    build_composition_handoff,
    generate_composition,
    validate_composition,
)
from music_agent.planner import generate_plan, validate_plan
from music_agent.clients.mock_music import (
    MOCK_MODEL_NAME,
    generate_audio,
    get_mock_model_parameters,
    inspect_audio,
)
from music_agent.brief import load_brief
from music_agent.storage import get_lyrics_path, load_state, save_state
from music_agent.storage import calculate_brief_fingerprint, find_existing_run
from music_agent.storage import write_new_artifact
from music_agent.storage import make_model_response_recorder
def run_creation(brief):
    """创建初始检查点并执行首次生成，同需求项目直接复用。

    Args:
        brief: 已校验的歌曲需求单。

    Returns:
        本次或已有项目的运行目录。

    Raises:
        ValueError: 项目冲突或生成结果不合法。
        OSError: 网络请求或文件读写失败。
    """
    fingerprint = calculate_brief_fingerprint(brief)
    existing_run = find_existing_run(brief["project_id"], fingerprint)
    if existing_run is not None:
        print(f"发现相同需求，复用已有项目：{existing_run}")
        return existing_run

    run_dir = Path("outputs") / f"run-{uuid4().hex}"
    run_dir.mkdir(parents=True)
    brief_data = json.dumps(brief, ensure_ascii=False, indent=2).encode("utf-8")
    write_new_artifact(run_dir / "brief.json", brief_data)

    # 在第一次模型请求之前登记项目，失败后也能通过编号找到它。
    state = {
        "project_id": brief["project_id"],
        "brief_fingerprint": fingerprint,
        "status": "new",
    }
    save_state(run_dir, state)
    continue_initial_creation(run_dir, state)
    return run_dir


def continue_initial_creation(run_dir, state):
    """执行或恢复计划、歌词生成，并保存各阶段检查点。

    Args:
        run_dir: 本次运行的产物目录。
        state: 已加载的 new 状态记录。

    Raises:
        ValueError: 输入、检查点或产物不合法。
        OSError: 网络请求或文件读写失败。
    """
    run_dir = Path(run_dir)
    if state.get("status") != "new":
        raise ValueError("初始生成只能从 new 状态继续")

    stage = "brief"
    try:
        brief = load_brief(run_dir / "brief.json")
        if (
            brief["project_id"] != state.get("project_id")
            or calculate_brief_fingerprint(brief) != state.get("brief_fingerprint")
        ):
            raise ValueError("需求单与初始检查点不一致")

        documents = {}
        # 回调按顺序执行，作词回调执行时计划已加载并通过校验。
        steps = (
            (
                "plan", "plan.json",
                lambda: generate_plan(
                    brief,
                    on_response=make_model_response_recorder(
                        run_dir, state["project_id"], "plan"
                    ),
                ),
                lambda document: validate_plan(document, brief["project_id"]),
            ),
            (
                "lyrics", "lyrics.json",
                lambda: generate_lyrics(
                    brief,
                    next(
                        step["description"]
                        for step in documents["plan"]["steps"]
                        if step["stage"] == "lyrics"
                    ),
                    on_response=make_model_response_recorder(
                        run_dir, state["project_id"], "lyrics"
                    ),
                ),
                lambda document: validate_lyrics(document, brief),
            ),
        )
        for stage, filename, generate, validate in steps:
            path = run_dir / filename
            recorded_hash = state.get(f"{stage}_sha256")
            if path.exists():
                # 文件完整落盘但状态未登记时，也允许校验后恢复。
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if recorded_hash is not None and recorded_hash != digest:
                    raise ValueError(f"{filename} 与检查点哈希不一致")
                document = json.loads(data)
                validate(document)
                print(f"已校验并复用 {filename}。", flush=True)
            else:
                if recorded_hash is not None:
                    raise ValueError(f"检查点中的 {filename} 已丢失")
                print(f"正在生成 {stage}……", flush=True)
                # 生成函数内部已经校验，此处不重复校验。
                document = generate()
                data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
                digest = hashlib.sha256(data).hexdigest()
                write_new_artifact(path, data)

            documents[stage] = document
            state.update({f"{stage}_file": filename, f"{stage}_sha256": digest})
            if stage == "lyrics":
                state["lyrics_version"] = 1
                state["status"] = "lyrics_ready"
            state.pop("last_error", None)
            save_state(run_dir, state)

    except (OSError, ValueError) as error:
        state["last_error"] = {"stage": stage, "message": str(error)}
        save_state(run_dir, state)
        raise
    print("歌词已生成，等待人工审核。")


def _load_or_generate_checkpoint(path, generate, validate, expected_hash=None, adopt_existing=False):
    """恢复已登记的 JSON 产物，或生成并完整提交新产物。

    Args:
        path: 固定版本的产物路径。
        generate: 已包含业务校验的生成函数。
        validate: 恢复已有文件时使用的业务校验函数。
        expected_hash: 已登记的产物哈希，尚未提交检查点时为 None。
        adopt_existing: 是否已通过生成任务记录确认未登记文件的来源。

    Returns:
        二元组：产物字典、实际文件 SHA-256。

    Raises:
        ValueError: 文件未登记、内容变化、丢失或业务校验失败。
        OSError: 请求或文件读写失败。
    """
    if path.exists():
        if expected_hash is None and not adopt_existing:
            raise ValueError(f"未登记的产物已存在：{path.name}")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if expected_hash is not None and digest != expected_hash:
            raise ValueError(f"{path.name} 与检查点不一致")
        document = json.loads(data)
        validate(document)
        print(f"已校验并复用 {path.name}。", flush=True)
        return document, digest
    if expected_hash is not None:
        raise ValueError(f"检查点中的 {path.name} 已丢失")

    document = generate()
    data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    write_new_artifact(path, data)
    return document, hashlib.sha256(data).hexdigest()


def revise_rejected_lyrics(run_dir, state):
    """根据拒绝意见生成新版本，保留旧文件并重新等待审核。ban
    Args:
        run_dir: 本次运行的产物目录。
        state: 已加载的当前状态。
    Raises:
        ValueError: 状态、拒绝记录或源文件不一致。
        OSError: 网络请求或文件读写失败。
    """
    run_dir = Path(run_dir)
    if state.get("status") != "lyrics_rejected":
        raise ValueError("当前歌词未处于拒绝状态")

    # 当前只处理进入作曲前的歌词修改，不改动已审核通过的产物。
    if (
        state.get("lyrics_approval")
        or state.get("composition_version")
        or state.get("audio_approval")
    ):
        raise ValueError("已有通过审核或下游产物，不能使用此修订流程")

    version = state.get("lyrics_version")
    rejection = state.get("lyrics_rejection")
    if type(version) is not int or version < 1:
        raise ValueError("歌词版本不合法")
    if not isinstance(rejection, dict):
        raise ValueError("缺少歌词拒绝记录")

    old_path = get_lyrics_path(run_dir, state)
    old_data = old_path.read_bytes()
    old_hash = hashlib.sha256(old_data).hexdigest()
    if (
        rejection.get("decision") != "rejected"
        or rejection.get("version") != version
        or rejection.get("sha256") != old_hash
        or state.get("lyrics_sha256") != old_hash
    ):
        raise ValueError("歌词与拒绝记录不一致")

    feedback = rejection.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValueError("拒绝记录缺少修改意见")

    brief = load_brief(run_dir / "brief.json")
    if brief["project_id"] != state.get("project_id"):
        raise ValueError("需求单与当前项目不一致")
    with (run_dir / "plan.json").open(encoding="utf-8") as file:
        plan = json.load(file)
    validate_plan(plan, brief["project_id"])
    task = next(step for step in plan["steps"] if step["stage"] == "lyrics")

    new_version = version + 1
    new_path = run_dir / f"lyrics-v{new_version:03d}.json"
    revision_inputs = {
        "brief": brief,
        "task_description": task["description"],
        "feedback": feedback.strip(),
        "parent": {"file": old_path.name, "version": version, "sha256": old_hash},
    }
    inputs_hash = hashlib.sha256(json.dumps(
        revision_inputs, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()
    expected_job = {"file": new_path.name, "version": new_version, "inputs_sha256": inputs_hash}
    recorded_job = state.get("lyrics_revision_job")
    if recorded_job is not None and recorded_job != expected_job:
        raise ValueError("歌词修订任务与当前输入不一致")
    if new_path.exists() and recorded_job is None:
        raise ValueError("目标歌词版本已存在，停止操作以避免覆盖")
    if recorded_job is None:
        # 先绑定源稿、修改意见及目标版本，再发起付费生成。
        state["lyrics_revision_job"] = expected_job
        save_state(run_dir, state)

    previous_lyrics = json.loads(old_data)
    try:
        print(f"正在生成或恢复歌词 v{new_version}……", flush=True)
        _, digest = _load_or_generate_checkpoint(
            new_path,
            lambda: generate_lyrics(
                brief, task["description"], previous_lyrics=previous_lyrics, feedback=feedback,
                on_response=make_model_response_recorder(
                    run_dir, state["project_id"], "lyrics", new_version
                ),
            ),
            lambda document: validate_lyrics(document, brief, previous_lyrics),
            adopt_existing=True,
        )
        # 新文件提交后才切换指针；旧稿、拒绝历史均保留。
        completed = {
            **state,
            "status": "lyrics_ready",
            "lyrics_file": new_path.name,
            "lyrics_version": new_version,
            "lyrics_sha256": digest,
            "lyrics_parent": {"file": old_path.name, "version": version, "sha256": old_hash},
        }
        completed.pop("lyrics_revision_job", None)
        completed.pop("last_error", None)
        save_state(run_dir, completed)
    except (OSError, ValueError) as error:
        state["last_error"] = {"stage": "lyrics_revision", "message": str(error)}
        save_state(run_dir, state)
        raise
    print("新版歌词已生成，等待重新审核。")


def resume_creation(run_dir):
    """根据初始生成、歌词拒绝或通过状态继续执行。
    Args:
        run_dir: 本次运行的产物目录。
    Raises:
        ValueError: 状态、审核记录、文件或输入不符合要求。
        OSError: 网络请求或文件读写失败。
    """
    run_dir = Path(run_dir)
    state = load_state(run_dir)

    if state.get("status") == "new":
        continue_initial_creation(run_dir, state)
        return

    if state.get("status") == "lyrics_rejected":
        revise_rejected_lyrics(run_dir, state)
        return

    if state.get("status") != "lyrics_approved":
        raise ValueError("当前仅支持从 new、lyrics_rejected 或 lyrics_approved 状态继续")

    # 同时检查审核结论、版本和哈希，不能只相信状态名称。
    approval = state.get("lyrics_approval")
    if not isinstance(approval, dict):
        raise ValueError("缺少歌词审核记录")
    if approval.get("decision") != "approved":
        raise ValueError("歌词审核未通过")

    version = state.get("lyrics_version")
    if type(version) is not int or version < 1:
        raise ValueError("歌词版本不合法")
    if approval.get("version") != version:
        raise ValueError("审核记录与当前歌词版本不一致")

    lyrics_bytes = get_lyrics_path(run_dir, state).read_bytes()
    lyrics_hash = hashlib.sha256(lyrics_bytes).hexdigest()
    if (
        lyrics_hash != state.get("lyrics_sha256")
        or lyrics_hash != approval.get("sha256")
    ):
        raise ValueError("歌词已发生变化，需要重新审核")

    lyrics = json.loads(lyrics_bytes)
    brief = load_brief(run_dir / "brief.json")
    if brief["project_id"] != state.get("project_id"):
        raise ValueError("需求单与当前项目不一致")

    with (run_dir / "plan.json").open(encoding="utf-8") as file:
        plan = json.load(file)
    validate_plan(plan, brief["project_id"])
    audio_task = next(
        step for step in plan["steps"] if step["stage"] == "audio"
    )

    composition_path = run_dir / "composition-v001.json"
    audio_path = run_dir / "audio-v001.wav"


    # 检查点同时绑定作曲输入，避免复用其他需求或歌词对应的方案。
    composition_inputs = {
        "brief": brief,
        "lyrics_sha256": lyrics_hash,
        "task_description": audio_task["description"],
    }
    inputs_hash = hashlib.sha256(
        json.dumps(
            composition_inputs,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    expected_composition_job = {
        "file": composition_path.name, "version": 1, "inputs_sha256": inputs_hash,
    }
    try:
        job = state.get("composition_job")
        if job is not None and job != expected_composition_job:
            raise ValueError("作曲生成任务与当前输入不一致")
        recorded_hash = state.get("composition_sha256")
        if recorded_hash is not None:
            # 已登记的旧检查点仍可复用，不补造历史任务记录。
            if (
                state.get("composition_version") != 1
                or state.get("composition_file") != composition_path.name
                or inputs_hash != state.get("composition_inputs_sha256")
            ):
                raise ValueError("已有作曲方案与检查点不一致，不能复用")
        else:
            if composition_path.exists() and job is None:
                raise ValueError("已有作曲方案缺少生成任务记录，不能接管")
            if job is None:
                state["composition_job"] = expected_composition_job
                save_state(run_dir, state)

        print("正在生成或恢复作曲方案……", flush=True)
        composition, digest = _load_or_generate_checkpoint(
            composition_path,
            lambda: generate_composition(
                brief, lyrics, audio_task["description"],
                on_response=make_model_response_recorder(
                    run_dir, state["project_id"], "composition"
                ),
            ),
            lambda document: validate_composition(document, brief, lyrics),
            expected_hash=recorded_hash,
            adopt_existing=state.get("composition_job") == expected_composition_job,
        )
        state.update({
            "composition_file": composition_path.name,
            "composition_version": 1,
            "composition_sha256": digest,
            "composition_inputs_sha256": inputs_hash,
        })
        save_state(run_dir, state)
    except (OSError, ValueError) as error:
        state["last_error"] = {"stage": "composition", "message": str(error)}
        save_state(run_dir, state)
        raise

    # 即使状态异常，也不允许恢复逻辑改动已经审核过的音频。
    if state.get("audio_approval"):
        raise ValueError("已有音频审核记录，不能执行未审核音频恢复")

    # 生成前登记任务，将目标文件绑定到当前输入及作曲方案。
    expected_job = {
        "file": audio_path.name,
        "producer": "mock_music",
        "model": MOCK_MODEL_NAME,
        "model_parameters": get_mock_model_parameters(brief["duration_seconds"]),
        "composition_sha256": state["composition_sha256"],
        "inputs_sha256": inputs_hash,
    }
    recorded_job = state.get("audio_job")

    if recorded_job is not None and recorded_job != expected_job:
        raise ValueError("音频生成记录与当前输入不一致")

    # 不接管无法确认来源的旧文件。
    if audio_path.exists() and recorded_job is None:
        raise ValueError("已有音频缺少生成记录，不能自动复用或替换")

    if recorded_job is None:
        state["audio_job"] = expected_job
        save_state(run_dir, state)

    try:
        audio_info = None

        if audio_path.exists():
            try:
                audio_info = inspect_audio(
                    audio_path, brief["duration_seconds"]
                )
            except ValueError as error:
                # 只处理当前任务的未审核文件，保留故障原件用于排查。
                failed_path = audio_path.with_name(
                    f"{audio_path.stem}.failed-{uuid4().hex}.wav"
                )
                audio_path.rename(failed_path)
                print(
                    f"音频检查失败：{error}；"
                    f"原文件已保留为 {failed_path.name}",
                    flush=True,
                )
            else:
                # 来源由生成任务记录确认，不由文件格式推断。
                audio_info.update({
                    "is_mock": True,
                    "model": recorded_job["model"],
                    "model_parameters": recorded_job["model_parameters"],
                })
                print("已校验并复用完整音频。", flush=True)

        if audio_info is None:
            print("正在生成 mock 测试音频……", flush=True)
            audio_info = generate_audio(
                audio_path, brief["duration_seconds"]
            )

    except (OSError, ValueError) as error:
        state["last_error"] = {
            "stage": "audio",
            "message": str(error),
        }
        save_state(run_dir, state)
        raise
    # 先构造交接快照，成功保存交接文件后再推进项目状态。
    handoff_state = {**state, "audio_version": 1, "audio": audio_info}
    handoff = build_composition_handoff(handoff_state, composition)
    handoff_data = json.dumps(
        handoff, ensure_ascii=False, indent=2
    ).encode("utf-8")
    handoff_path = run_dir / (
        f"composition-result-v{state['composition_version']:03d}.json"
    )

    # 交接文件写完、状态未保存就中断时，复用内容一致的文件。
    if handoff_path.exists():
        if handoff_path.read_bytes() != handoff_data:
            raise ValueError("已有作曲交接文件与当前产物不一致")
    else:
        write_new_artifact(handoff_path, handoff_data)

    state.update({
        "status": "audio_ready",
        "audio_version": 1,
        "audio": audio_info,
        "composition_result_file": handoff_path.name,
        "composition_result_sha256": hashlib.sha256(handoff_data).hexdigest(),
    })
    state.pop("last_error", None)
    save_state(run_dir, state)
    print("作曲方案、测试音频和交接清单已生成，等待人工审核。")
