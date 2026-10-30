"""发行物料包：收集发行信息并检查资料完整性。"""
import json
from pathlib import Path
from music_agent.storage import get_lyrics_path, load_state
import hashlib
import math
from io import BytesIO
from PIL import Image, UnidentifiedImageError
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from music_agent.storage import save_state, write_new_artifact
from music_agent.clients.mock_music import inspect_audio_bytes
# 相对封面路径统一以项目根目录为基准，不受终端工作目录影响。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def check_release_info(snapshot):
    """从已有产物中收集发行信息，返回缺项列表。
    本函数不修改状态，也不判断物料包是否 ready。
    文件哈希、审核版本绑定及格式校验由后续导出流程执行。
    Args:
        snapshot: load_package_snapshot 返回的数据快照。
    Returns:
        二元组：发行信息字典、问题列表。
    Raises:
        ValueError: 当前状态不允许打包或输入 JSON 格式不合法。
        OSError: 文件读取失败。
    """
    state = snapshot["state"]
    documents = snapshot["documents"]
    problems = []

    for filename in ("brief.json", "lyrics.json"):
        if filename not in documents:
            problems.append(f"缺少 {filename}")

    if len(documents) != 2:
        return {}, problems

    brief = documents["brief.json"]
    lyrics = documents["lyrics.json"]
    if brief.get("project_id") != state.get("project_id"):
        problems.append("需求单与当前项目编号不一致")

    info = {
        "project_id": state.get("project_id"),
        "project_name": lyrics.get("project_name"),
        "title": lyrics.get("title"),
        "cover_placeholder_path": brief.get("cover_placeholder_path"),
        "rights_declaration": "",
    }

    for field, label in (
        ("project_name", "创作项目名"),
        ("title", "歌曲标题"),
    ):
        value = info[field]
        if not isinstance(value, str) or not value.strip():
            problems.append(f"缺少{label}")

    cover_value = info["cover_placeholder_path"]
    if not isinstance(cover_value, str) or not cover_value.strip():
        problems.append("缺少封面占位图路径")
    else:
        cover_path = Path(cover_value)
        if not cover_path.is_absolute():
            cover_path = PROJECT_ROOT / cover_path

        if not cover_path.is_file():
            problems.append("封面文件不存在")
        else:
            info["cover_source_path"] = str(cover_path.resolve())

    # 当前实现仅支持本地合成的 mock 音频，不替未知来源编造声明。
    audio_info = state.get("audio")
    if isinstance(audio_info, dict) and audio_info.get("is_mock") is True:
        info["rights_declaration"] = (
            "本项目歌词与作曲方案由 AI 辅助生成，"
            "原创要求遵循歌曲需求单所列约束。"
            "音频为本地程序合成的 mock 测试音，未使用外部音频采样。"
            "本声明记录制作方式，不代表已完成原创性鉴定。"
        )
    else:
        problems.append("缺少可用于整理声明的音频制作来源")

    return info, problems

def validate_approved_assets(snapshot):
    """校验待打包产物是否仍是人工审核通过的版本。
    Args:
        snapshot: 已加载的状态和源文件快照。
    Returns:
        包含各产物路径和实际 SHA-256 的字典。
    Raises:
        ValueError: 状态、版本、哈希或审核记录不一致。
        OSError: 产物读取失败。
    """
    state = snapshot["state"]
    versions = snapshot["versions"]

    audio_info = state.get("audio")
    if not isinstance(audio_info, dict):
        raise ValueError("缺少音频元数据")
    expected_hashes = {
        "lyrics": state.get("lyrics_sha256"),
        "composition": state.get("composition_sha256"),
        "audio": audio_info.get("sha256"),
    }
    # 对已读取的字节计算哈希，不重新打开文件。
    assets = {}
    for stage in ("lyrics", "composition", "audio"):
        path = snapshot["paths"][stage]
        data = snapshot["source_data"].get(stage)
        if data is None:
            raise ValueError(f"缺少 {path.name}")
        if not data:
            raise ValueError(f"{path.name} 是空文件")

        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hashes[stage]:
            raise ValueError(f"{path.name} 与状态中的哈希不一致")

        assets[stage] = {
            "path": path,
            "sha256": actual_hash,
            "data": data,
        }
    # 哈希只能证明字节未变，音频参数仍需与实际 WAV 内容逐项比较。
    _validate_audio_metadata(audio_info, assets["audio"]["data"])

    # 两个审核关卡都必须存在明确的人工通过记录。
    for stage in ("lyrics", "audio"):
        approval = state.get(f"{stage}_approval")
        if not isinstance(approval, dict):
            raise ValueError(f"缺少 {stage} 审核记录")
        if (
            approval.get("decision") != "approved"
            or approval.get("version") != versions[stage]
            or approval.get("sha256") != assets[stage]["sha256"]
        ):
            raise ValueError(f"{stage} 审核与当前产物不一致")

        for field in ("reviewer", "approved_at"):
            value = approval.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{stage} 审核记录缺少 {field}")
    # 音频审核还绑定了它所依据的歌词及作曲方案。
    audio_approval = state["audio_approval"]
    for stage in ("lyrics", "composition"):
        if (
            audio_approval.get(f"{stage}_version") != versions[stage]
            or audio_approval.get(f"{stage}_sha256")
            != assets[stage]["sha256"]
        ):
            raise ValueError(f"音频审核绑定的 {stage} 已发生变化")
    return assets

def prepare_package_files(snapshot):
    """校验输入并准备发行包文件内容，暂不写入磁盘。
    Args:
        snapshot: load_package_snapshot 返回的数据快照。
    Returns:
        文件名到文件字节内容的字典，供导出流程统一写入。
    Raises:
        ValueError: 资料缺失、产物变化或封面不是有效 PNG。
        OSError: 文件读取失败。
    """
    # 一次读取，再把同一份快照传给不同职责的检查函数。

    info, problems = check_release_info(snapshot)
    if problems:
        raise ValueError("发行资料不完整：" + "；".join(problems))

    assets = validate_approved_assets(snapshot)
    state = snapshot["state"]
    source_data = {
        stage: asset["data"]
        for stage, asset in assets.items()
    }

    # 将结构化歌词转为下游可直接阅读的 Markdown。
    lyrics = snapshot["documents"]["lyrics.json"]
    markdown = [f"# {lyrics['title']}", ""]
    for section in lyrics["sections"]:
        markdown.extend([
            f"## {section['name']}",
            "",
            f"情绪：{section['emotion']}",
            "",
        ])
        markdown.extend(line + "  " for line in section["lines"])
        markdown.append("")

    # 文件扩展名不能证明图片有效，需要实际解码检查。
    cover_data = Path(info["cover_source_path"]).read_bytes()
    try:
        with Image.open(BytesIO(cover_data)) as image:
            if image.format != "PNG":
                raise ValueError("封面必须是 PNG 格式")
            image.verify()
        # verify 检查文件结构，再重新打开并加载像素数据。
        with Image.open(BytesIO(cover_data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise ValueError("封面图片损坏或无法识别") from error

    approvals = {
        "project_id": state["project_id"],
        "lyrics_approval": state["lyrics_approval"],
        "audio_approval": state["audio_approval"],
    }

    readme = (
        f"# {info['title']}\n\n"
        f"项目：{info['project_name']}\n\n"
        f"项目编号：{info['project_id']}\n\n"
        "## 文件说明\n\n"
        "- lyrics.md：已审核歌词的 Markdown 副本。\n"
        "- composition.json：已审核的作曲方案。\n"
        "- audio.wav：已审核的 mock 测试音频。\n"
        "- cover.png：需求单指定的占位封面。\n"
        "- approvals.json：歌词及音频审核记录。\n"
        "- manifest.json：物料清单、版本与文件校验值。\n\n"
        "## 制作说明\n\n"
        f"{info['rights_declaration']}\n\n"
        "mock 音频不包含真实演唱，也不实际呈现作曲方案的编曲效果。\n\n"
        "本物料包交给下游发行 Agent，本程序未执行平台发布。\n"
    )

    files = {
        "lyrics.md": ("\n".join(markdown) + "\n").encode("utf-8"),
        "composition.json": source_data["composition"],
        "audio.wav": source_data["audio"],
        "cover.png": cover_data,
        "approvals.json": json.dumps(
            approvals, ensure_ascii=False, indent=2
        ).encode("utf-8"),
        "README.md": readme.encode("utf-8"),
    }
    return files,info

def load_package_snapshot(run_dir, state=None):
    """一次读取打包所需的状态和源文件。
    Args:
        run_dir: 本次运行的产物目录。
        state: 可选的已加载状态，避免导出入口重复读取。
    Returns:
        包含状态、版本、路径、原始字节和解析后文档的字典。
    Raises:
        ValueError: 状态、版本或 JSON 文档不合法。
        OSError: 文件读取失败。
    """
    run_dir = Path(run_dir)
    if state is None:
        state = load_state(run_dir)
    if state.get("status") not in (
        "audio_approved", "package_needs_info", "package_ready"
    ):
        raise ValueError("当前状态不允许导出")

    versions = {}
    for stage in ("lyrics", "composition", "audio"):
        version = state.get(f"{stage}_version")
        if type(version) is not int or version < 1:
            raise ValueError(f"{stage} 版本不合法")
        versions[stage] = version

    paths = {
        "brief": run_dir / "brief.json",
        "lyrics": get_lyrics_path(run_dir, state),
        "composition": run_dir / f"composition-v{versions['composition']:03d}.json",
        "audio": run_dir / f"audio-v{versions['audio']:03d}.wav",
    }

    # 文件缺失留给后续检查报告；其他读取错误直接向上传递。
    source_data = {}
    for name, path in paths.items():
        try:
            source_data[name] = path.read_bytes()
        except FileNotFoundError:
            continue

    documents = {}
    for name in ("brief", "lyrics"):
        if name not in source_data:
            continue
        try:
            document = json.loads(source_data[name])
        except (ValueError, UnicodeError) as error:
            raise ValueError(f"{paths[name].name} 不是有效 JSON") from error
        if not isinstance(document, dict):
            raise ValueError(f"{paths[name].name} 必须是 JSON 对象")
        documents[f"{name}.json"] = document

    return {
        "state": state,
        "versions": versions,
        "paths": paths,
        "source_data": source_data,
        "documents": documents,
    }

def verify_package_files(package_dir, file_records):
    """读取导出文件，检查实际字节数和哈希。
    Args:
        package_dir: 物料包目录。
        file_records: 文件名到预期大小、哈希的字典。
    Returns:
        校验通过的文件字节字典，供进一步解析时复用。
    Raises:
        ValueError: 导出文件与预期不一致。
        OSError: 文件不存在或读取失败。
    """
    contents = {}
    for filename, record in file_records.items():
        data = (package_dir / filename).read_bytes()
        if (
            len(data) != record["size_bytes"]
            or hashlib.sha256(data).hexdigest() != record["sha256"]
        ):
            raise ValueError(f"导出文件校验失败：{filename}")
        contents[filename] = data
    return contents


def _validate_audio_metadata(audio, data=None):
    """校验音频必需参数，并在提供字节时核对实际 WAV 内容。

    Args:
        audio: 源状态或物料清单中的音频元数据。
        data: 可选的已有音频字节，用于格式与参数核验。

    Raises:
        ValueError: 必需参数缺失、类型或值不合法，或与实际音频不符。
    """
    if not isinstance(audio, dict):
        raise ValueError("缺少音频元数据")
    if audio.get("format") != "wav":
        raise ValueError("音频 format 必须为 wav")
    for field in ("sample_rate", "channels"):
        value = audio.get(field)
        # bool 是 int 的子类，不能把 true 当成合法声道数。
        if type(value) is not int or value <= 0:
            raise ValueError(f"音频 {field} 必须是正整数")
    duration = audio.get("duration_seconds")
    if (
        type(duration) not in (int, float)
        or duration <= 0
        or (type(duration) is float and not math.isfinite(duration))
    ):
        raise ValueError("音频 duration_seconds 必须是有限的正数")

    if data is not None:
        actual = inspect_audio_bytes(data)
        for field in ("format", "sample_rate", "channels", "duration_seconds"):
            if audio[field] != actual[field]:
                raise ValueError(f"音频 {field} 与实际 WAV 内容不一致")


def _validate_manifest(manifest):
    """校验物料包清单及审核绑定，不读取文件。"""
    if not isinstance(manifest, dict):
        raise ValueError("已有物料包 manifest 格式错误")
    if manifest.get("status") not in ("ready", "needs_info"):
        raise ValueError("物料包状态不合法")
    for field in ("project_id", "project_name", "title", "rights_declaration", "created_at"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"物料清单缺少 {field}")
    if type(manifest.get("package_version")) is not int or manifest["package_version"] < 1:
        raise ValueError("物料包版本不合法")
    versions = manifest.get("versions")
    if not isinstance(versions, dict) or any(
        type(versions.get(stage)) is not int or versions[stage] < 1
        for stage in ("lyrics", "composition", "audio")
    ):
        raise ValueError("物料清单中的产物版本不合法")

    required = {"lyrics.md", "composition.json", "audio.wav", "cover.png", "approvals.json", "README.md"}
    records = manifest.get("files")
    if not isinstance(records, dict) or set(records) != required:
        raise ValueError("物料清单必须记录全部六个物料文件")

    def is_sha256(value):
        return isinstance(value, str) and len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )

    if not is_sha256(manifest.get("brief_sha256")):
        raise ValueError("物料清单缺少有效的 brief 哈希")
    for name, record in records.items():
        if (
            not isinstance(record, dict)
            or type(record.get("size_bytes")) is not int
            or record["size_bytes"] <= 0
            or not is_sha256(record.get("sha256"))
        ):
            raise ValueError(f"物料清单的 {name} 校验信息不合法")
    source = manifest.get("lyrics_source")
    audio = manifest.get("audio")
    if not isinstance(source, dict) or not is_sha256(source.get("sha256")):
        raise ValueError("物料清单缺少歌词审核源哈希")
    if not isinstance(audio, dict) or audio.get("sha256") != records["audio.wav"]["sha256"]:
        raise ValueError("物料清单的音频哈希不一致")
    _validate_audio_metadata(audio)
    for stage, expected_hash in (
        ("lyrics", source["sha256"]), ("audio", records["audio.wav"]["sha256"])
    ):
        approval = manifest.get(f"{stage}_approval")
        if not isinstance(approval, dict) or (
            approval.get("decision") != "approved"
            or approval.get("version") != versions[stage]
            or approval.get("sha256") != expected_hash
        ):
            raise ValueError(f"物料清单的 {stage} 审核不一致")
        for field in ("reviewer", "approved_at"):
            if not isinstance(approval.get(field), str) or not approval[field].strip():
                raise ValueError(f"物料清单的 {stage} 审核缺少 {field}")
    approval = manifest["audio_approval"]
    if (
        approval.get("lyrics_version") != versions["lyrics"]
        or approval.get("lyrics_sha256") != source["sha256"]
        or approval.get("composition_version") != versions["composition"]
        or approval.get("composition_sha256") != records["composition.json"]["sha256"]
    ):
        raise ValueError("物料清单的音频审核来源不一致")


def _read_verified_package(package_dir):
    """独立验证已有包，不依赖可能已变化的源文件。"""
    with (package_dir / "manifest.json").open(encoding="utf-8") as file:
        manifest = json.load(file)
    _validate_manifest(manifest)
    contents = verify_package_files(package_dir, manifest["files"])
    _validate_audio_metadata(manifest["audio"], contents["audio.wav"])
    approvals = json.loads(contents["approvals.json"])
    if not isinstance(approvals, dict) or any(
        approvals.get(field) != manifest[field]
        for field in ("project_id", "lyrics_approval", "audio_approval")
    ):
        raise ValueError("包内审核记录与 manifest 不一致")
    return manifest


def _write_manifest(package_dir, manifest):
    """原子更新清单状态，保留包内其他物料。"""
    temporary = package_dir / f".manifest-{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump(manifest, file, ensure_ascii=False, indent=2)
        temporary.replace(package_dir / "manifest.json")
    finally:
        temporary.unlink(missing_ok=True)


def _invalidate_broken_package(package_dir, project_id, problems):
    """只撤销损坏包的 ready；源文件变化不影响完整旧包。"""
    if not package_dir.is_dir():
        return
    try:
        _read_verified_package(package_dir)
        return
    except (OSError, ValueError) as error:
        package_problem = str(error)

    path = package_dir / "manifest.json"
    raw = path.read_bytes() if path.is_file() else None
    try:
        manifest = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeError):
        manifest = None
    if not isinstance(manifest, dict):
        # 无法解析的原始清单保留在包外，避免覆盖后丢失排查证据。
        if raw is not None:
            backup = package_dir.parent / f"manifest-invalid-{uuid4().hex}.json"
            write_new_artifact(backup, raw)
        manifest = {"project_id": project_id}
    manifest.update({
        "status": "needs_info",
        "validation_errors": list(dict.fromkeys([*problems, package_problem])),
        "validated_at": datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S（北京时间）"
        ),
    })
    _write_manifest(package_dir, manifest)


def export_package(run_dir):
    """导出首版发行物料包，重复执行时验证并复用已有包。
    Args:
        run_dir: 本次运行的产物目录。
    Returns:
        已完成校验的 release-package 目录。
    Raises:
        ValueError: 资料、审核、源产物或已有物料包不一致。
        OSError: 文件读写或目录操作失败。
    """
    run_dir = Path(run_dir)
    state = load_state(run_dir)
    # 未通过审核的项目仍保持原状态，不能靠 export 改成可打包状态。
    if state.get("status") not in ("audio_approved", "package_needs_info", "package_ready"):
        raise ValueError("当前状态不允许导出")
    target_dir = run_dir / "release-package"

    try:
        snapshot = load_package_snapshot(run_dir, state)
        files, info = prepare_package_files(snapshot)
        # 包内哈希基于实际要写入的字节，而不是源文件路径。
        file_records = {
            name: {
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in files.items()
        }

        # 歌词 JSON 是审核源文件，Markdown 是发行包内的阅读副本。
        manifest = {
            "status": "ready",
            "project_id": info["project_id"],
            "project_name": info["project_name"],
            "title": info["title"],
            "rights_declaration": info["rights_declaration"],
            "package_version": 1,
            "versions": snapshot["versions"],
            "brief_sha256": hashlib.sha256(
                snapshot["source_data"]["brief"]
            ).hexdigest(),
            "lyrics_source": {
                "file": state["lyrics_file"],
                "location": "original_run_directory",
                "sha256": state["lyrics_sha256"],
            },
            "audio": {
                **state["audio"],
                # 包内使用相对文件名，不能沿用原运行目录路径。
                "file": "audio.wav",
            },
            "lyrics_approval": state["lyrics_approval"],
            "audio_approval": state["audio_approval"],
            "files": file_records,
        }

        if target_dir.exists():
            # 同一份输入重复导出时复用，不覆盖已有版本。
            existing = _read_verified_package(target_dir)
            existing_metadata = dict(existing)
            created_at = existing_metadata.pop("created_at", None)
            # 原件修复后允许重新验证，恢复状态，不修改版本及创建时间。
            existing_metadata["status"] = "ready"
            existing_metadata.pop("validation_errors", None)
            existing_metadata.pop("validated_at", None)
            if (
                not isinstance(created_at, str)
                or not created_at.strip()
                or existing_metadata != manifest
            ):
                raise ValueError("已有物料包与当前输入不一致，不能覆盖")

            if existing.get("status") != "ready" or "validation_errors" in existing or "validated_at" in existing:
                _write_manifest(target_dir, {**manifest, "created_at": created_at})
        else:
            # 先写临时目录，校验成功后再以正式目录名交付。
            staging_dir = run_dir / f".release-package-{uuid4().hex}"
            staging_dir.mkdir()

            for filename, data in files.items():
                with (staging_dir / filename).open("xb") as file:
                    file.write(data)

            # 临时包在完整性校验通过之前不写 ready 标记。
            manifest["created_at"] = datetime.now(
                timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%d %H:%M:%S（北京时间）")

            _validate_manifest(manifest)
            _write_manifest(staging_dir, {**manifest, "status": "needs_info"})
            _read_verified_package(staging_dir)
            _write_manifest(staging_dir, manifest)
            staging_dir.rename(target_dir)

    except (OSError, ValueError) as error:
        # 未满足交付条件时，不能留下 package_ready 项目状态。
        state["status"] = "package_needs_info"
        state["package_missing_info"] = [str(error)]
        state["last_error"] = {"stage": "package", "message": str(error)}
        try:
            _invalidate_broken_package(target_dir, state["project_id"], state["package_missing_info"])
        except (OSError, ValueError) as invalidation_error:
            state["package_missing_info"].append(f"撤销损坏包状态失败：{invalidation_error}")
        save_state(run_dir, state)
        raise

    state["status"] = "package_ready"
    state["package_version"] = 1
    state["package_directory"] = str(target_dir)
    state["package_missing_info"] = []
    if isinstance(state.get("last_error"), dict) and state["last_error"].get("stage") == "package":
        state.pop("last_error")
    save_state(run_dir, state)
    return target_dir
