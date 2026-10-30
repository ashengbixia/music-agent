"""打包缺项、快照异常、包内篡改和重复导出验收测试。"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from music_agent import packaging, workflow
from music_agent.planner import STAGES
from music_agent.review import approve_audio, approve_lyrics
from music_agent.storage import load_state, save_state


@pytest.fixture
def approved_run(tmp_path, monkeypatch):
    """创建真实本地产物及审核记录，所有模型调用均替换。"""
    monkeypatch.chdir(tmp_path)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (8, 8), "blue").save(cover)
    brief = {
        "project_id": "package_test", "theme": "毕业告别", "genre": "流行",
        "mood": "温暖", "duration_seconds": 1, "audience": "毕业生",
        "lyrics_language": "中文", "platform": ["测试平台"],
        "primary_platform": "测试平台", "allow_sampling": False,
        "cover_placeholder_path": str(cover),
    }
    monkeypatch.setattr(workflow, "generate_plan", Mock(return_value={
        "project_id": brief["project_id"],
        "steps": [{"stage": stage, "description": "测试任务"} for stage in STAGES],
    }))
    monkeypatch.setattr(workflow, "generate_lyrics", Mock(return_value={
        "project_name": "告别企划", "title": "再见",
        "sections": [{"name": "主歌", "emotion": "温暖", "lines": ["测试歌词"]}],
    }))
    monkeypatch.setattr(workflow, "generate_composition", Mock(return_value={
        "bpm": 100, "key": "C",
        "sections": [{"name": "主歌", "duration_seconds": 1, "arrangement": "钢琴"}],
        "handoff": "mock 音频",
    }))
    run = workflow.run_creation(brief)
    approve_lyrics(run, "测试审核人")
    workflow.resume_creation(run)
    approve_audio(run, "测试审核人")
    return run


def read_manifest(package):
    """读取实际导出清单。"""
    return json.loads((package / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("problem", [
    "invalid_version", "invalid_brief_json", "invalid_lyrics_json",
    "missing_lyrics", "missing_audio", "missing_cover", "invalid_cover",
    "missing_lyrics_approval", "missing_audio_approval", "wrong_approval_version",
    "unknown_audio_source", "changed_composition",
])
def test_source_problems_record_needs_info(approved_run, problem):
    """快照加载和资料检查错误都必须记录缺项状态，不能生成 ready 包。"""
    run = approved_run
    state = load_state(run)
    if problem == "invalid_version":
        state["composition_version"] = 0
    elif problem == "invalid_brief_json":
        (run / "brief.json").write_bytes(b"{")
    elif problem == "invalid_lyrics_json":
        (run / state["lyrics_file"]).write_bytes(b"{")
    elif problem == "missing_lyrics":
        (run / state["lyrics_file"]).unlink()
    elif problem == "missing_audio":
        (run / "audio-v001.wav").unlink()
    elif problem in {"missing_cover", "invalid_cover"}:
        cover = run.resolve().parents[1] / "cover.png"
        if problem == "missing_cover":
            cover.unlink()
        else:
            cover.write_bytes(b"not a PNG")
    elif problem == "missing_lyrics_approval":
        state.pop("lyrics_approval")
    elif problem == "missing_audio_approval":
        state.pop("audio_approval")
    elif problem == "wrong_approval_version":
        state["audio_approval"]["version"] = 2
    elif problem == "unknown_audio_source":
        state["audio"].pop("is_mock")
    elif problem == "changed_composition":
        path = run / state["composition_file"]
        path.write_bytes(path.read_bytes() + b"\n")
    save_state(run, state)

    with pytest.raises((OSError, ValueError)):
        packaging.export_package(run)
    state = load_state(run)
    assert state["status"] == "package_needs_info"
    assert state["package_missing_info"]
    assert state["last_error"]["stage"] == "package"
    assert not (run / "release-package").exists()
    report = json.loads((run / "run_report.json").read_text(encoding="utf-8"))
    assert report["current_status"] == "package_needs_info"


def test_repeated_export_reuses_same_version_and_files(approved_run):
    """重复导出不改创建时间、版本、清单或物料字节。"""
    package = packaging.export_package(approved_run)
    before = {path.name: path.read_bytes() for path in package.iterdir()}
    assert packaging.export_package(approved_run) == package
    assert {path.name: path.read_bytes() for path in package.iterdir()} == before
    assert load_state(approved_run)["package_version"] == 1
    assert not list(approved_run.glob(".release-package-*"))


@pytest.mark.parametrize("name", ["lyrics.md", "composition.json", "audio.wav", "cover.png", "approvals.json", "README.md"])
@pytest.mark.parametrize("missing", [False, True])
def test_broken_existing_package_loses_ready(approved_run, name, missing):
    """任一包内文件损坏或丢失，项目及 manifest 都不能继续 ready。"""
    package = packaging.export_package(approved_run)
    before = {path.name: path.read_bytes() for path in package.iterdir()}
    target = package / name
    if missing:
        target.unlink()
    else:
        target.write_bytes(before[name] + b"\n")
    with pytest.raises((OSError, ValueError)):
        packaging.export_package(approved_run)
    assert load_state(approved_run)["status"] == "package_needs_info"
    assert read_manifest(package)["status"] == "needs_info"
    assert read_manifest(package)["validation_errors"]
    for other, content in before.items():
        if other not in {name, "manifest.json"}:
            assert (package / other).read_bytes() == content
    if not missing:
        assert target.read_bytes() == before[name] + b"\n"


@pytest.mark.parametrize("damage", ["invalid_json", "array", "missing", "invalid_status"])
def test_unreadable_manifest_preserves_evidence_and_marks_needs_info(approved_run, damage):
    """无法读取清单时保留原始内容，生成明确的缺项标记。"""
    package = packaging.export_package(approved_run)
    path = package / "manifest.json"
    original = None
    if damage == "missing":
        path.unlink()
    else:
        if damage == "invalid_status":
            manifest = read_manifest(package)
            manifest["status"] = []
            original = json.dumps(manifest).encode("utf-8")
        else:
            original = b"{" if damage == "invalid_json" else b"[]"
        path.write_bytes(original)
    with pytest.raises((OSError, ValueError)):
        packaging.export_package(approved_run)
    assert read_manifest(package)["status"] == "needs_info"
    assert load_state(approved_run)["status"] == "package_needs_info"
    backups = list(approved_run.glob("manifest-invalid-*.json"))
    assert len(backups) == (1 if damage in {"invalid_json", "array"} else 0)
    if backups:
        assert backups[0].read_bytes() == original


@pytest.mark.parametrize("field", ["rights_declaration", "lyrics_approval", "audio_approval"])
def test_required_manifest_information_missing_cannot_be_ready(approved_run, field):
    """清单缺声明或审核记录时，不能仅凭物料哈希正确就通过。"""
    package = packaging.export_package(approved_run)
    manifest = read_manifest(package)
    manifest.pop(field)
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        packaging.export_package(approved_run)
    assert read_manifest(package)["status"] == "needs_info"


def test_repaired_package_can_be_revalidated_without_new_version(approved_run):
    """还原被损坏的物料后可重新验证，保留原版本及创建时间。"""
    package = packaging.export_package(approved_run)
    original_manifest = read_manifest(package)
    path = package / "audio.wav"
    original_audio = path.read_bytes()
    path.write_bytes(b"broken")
    with pytest.raises(ValueError):
        packaging.export_package(approved_run)
    path.write_bytes(original_audio)
    assert packaging.export_package(approved_run) == package
    assert read_manifest(package) == original_manifest
    state = load_state(approved_run)
    assert state["status"] == "package_ready"
    assert "last_error" not in state
    report = json.loads((approved_run / "run_report.json").read_text(encoding="utf-8"))
    assert any(event["progress"]["status"] == "package_needs_info" for event in report["history"])


def test_source_failure_does_not_invalidate_complete_old_package(approved_run):
    """源文件异常与旧包自身损坏要区分，完整旧包保持原样。"""
    package = packaging.export_package(approved_run)
    before = {path.name: path.read_bytes() for path in package.iterdir()}
    (approved_run / "brief.json").write_bytes(b"{")
    with pytest.raises(ValueError):
        packaging.export_package(approved_run)
    assert load_state(approved_run)["status"] == "package_needs_info"
    assert {path.name: path.read_bytes() for path in package.iterdir()} == before
    assert read_manifest(package)["status"] == "ready"


def test_unapproved_project_state_is_not_promoted(approved_run):
    """未审核项目不能通过 export 被改成可打包状态。"""
    state = load_state(approved_run)
    state["status"] = "audio_ready"
    save_state(approved_run, state)
    with pytest.raises(ValueError, match="当前状态不允许导出"):
        packaging.export_package(approved_run)
    assert load_state(approved_run)["status"] == "audio_ready"
    assert not (approved_run / "release-package").exists()


def test_blank_generated_declaration_does_not_produce_ready_package(approved_run, monkeypatch):
    """即使资料收集函数漏报空声明，最终清单校验仍须阻止 ready。"""
    real_check = packaging.check_release_info

    def blank_declaration(snapshot):
        info, problems = real_check(snapshot)
        info["rights_declaration"] = ""
        return info, problems

    monkeypatch.setattr(packaging, "check_release_info", blank_declaration)
    with pytest.raises(ValueError, match="rights_declaration"):
        packaging.export_package(approved_run)
    assert load_state(approved_run)["status"] == "package_needs_info"
    assert not (approved_run / "release-package").exists()


def test_staging_verification_failure_never_publishes_ready(approved_run, monkeypatch):
    """落盘校验失败时只保留未完成临时包，不交付 ready 目录。"""
    monkeypatch.setattr(packaging, "verify_package_files", Mock(side_effect=ValueError("模拟物料校验失败")))
    with pytest.raises(ValueError, match="模拟物料校验失败"):
        packaging.export_package(approved_run)
    assert not (approved_run / "release-package").exists()
    staging = list(approved_run.glob(".release-package-*"))
    assert len(staging) == 1
    assert read_manifest(staging[0])["status"] == "needs_info"
    assert load_state(approved_run)["status"] == "package_needs_info"


def test_export_reads_state_only_once(approved_run, monkeypatch):
    """错误处理改动不能重新引入入口和快照重复读取状态。"""
    reader = Mock(wraps=packaging.load_state)
    monkeypatch.setattr(packaging, "load_state", reader)
    packaging.export_package(approved_run)
    assert reader.call_count == 1


@pytest.mark.parametrize("location", ["state", "manifest"])
@pytest.mark.parametrize("field", ["format", "duration_seconds", "sample_rate", "channels"])
def test_missing_audio_metadata_cannot_be_ready(approved_run, location, field):
    """源状态或已有清单缺少音频必需字段时，均须阻止 ready。"""
    if location == "state":
        state = load_state(approved_run)
        state["audio"].pop(field)
        save_state(approved_run, state)
    else:
        package = packaging.export_package(approved_run)
        manifest = read_manifest(package)
        manifest["audio"].pop(field)
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        packaging.export_package(approved_run)

    assert load_state(approved_run)["status"] == "package_needs_info"
    package = approved_run / "release-package"
    if location == "manifest":
        assert read_manifest(package)["status"] == "needs_info"
    else:
        assert not package.exists()


@pytest.mark.parametrize("location", ["state", "manifest"])
@pytest.mark.parametrize("field,value", [
    ("format", "mp3"),
    ("sample_rate", True),
    ("sample_rate", "22050"),
    ("sample_rate", 0),
    ("channels", True),
    ("channels", -1),
    ("duration_seconds", True),
    ("duration_seconds", "1"),
    ("duration_seconds", 0),
    ("duration_seconds", float("nan")),
    ("duration_seconds", float("inf")),
    ("sample_rate", 44100),
    ("channels", 2),
    ("duration_seconds", 2),
])
def test_invalid_or_inaccurate_audio_metadata_rejected(approved_run, location, field, value):
    """不仅检查字段存在，还应拒绝非法值及与实际 WAV 不符的参数。"""
    if location == "state":
        state = load_state(approved_run)
        state["audio"][field] = value
        save_state(approved_run, state)
    else:
        package = packaging.export_package(approved_run)
        manifest = read_manifest(package)
        manifest["audio"][field] = value
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        packaging.export_package(approved_run)

    assert load_state(approved_run)["status"] == "package_needs_info"
    package = approved_run / "release-package"
    if location == "manifest":
        assert read_manifest(package)["status"] == "needs_info"
    else:
        assert not package.exists()


def test_export_reads_source_audio_once(approved_run, monkeypatch):
    """音频格式检查复用快照字节，不能重新读取源 WAV。"""
    audio_path = (approved_run / "audio-v001.wav").resolve()
    real_read = Path.read_bytes
    reads = []

    def track_read(path):
        """记录源音频读取次数，其他文件按正常逻辑读取。"""
        if path.resolve() == audio_path:
            reads.append(path)
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", track_read)
    packaging.export_package(approved_run)

    assert len(reads) == 1


class SimulatedPackageCrash(BaseException):
    """模拟进程突然中断，绕过业务异常处理。"""


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_export_recovers_after_crash(approved_run, monkeypatch, point):
    """打包提交前后中断，重新 export 均应恢复且不覆盖已提交文件。"""
    run_dir = approved_run
    package = run_dir / "release-package"
    original_state = (run_dir / "state.json").read_bytes()
    original_report = (run_dir / "run_report.json").read_bytes()
    real_rename = Path.rename

    def interrupt_commit(source, target):
        """在正式目录提交前或提交后模拟中断。"""
        if source.name.startswith(".release-package-"):
            if point == "after_commit":
                real_rename(source, target)
            raise SimulatedPackageCrash()
        return real_rename(source, target)

    monkeypatch.setattr(Path, "rename", interrupt_commit)

    with pytest.raises(SimulatedPackageCrash):
        packaging.export_package(run_dir)

    # 尚未保存最终状态，不得提前报告打包成功。
    assert (run_dir / "state.json").read_bytes() == original_state
    assert (run_dir / "run_report.json").read_bytes() == original_report
    assert package.exists() == (point == "after_commit")

    committed_files = None
    if package.exists():
        committed_files = {
            path.name: path.read_bytes()
            for path in package.iterdir()
        }

    # 恢复正常提交，重新执行 export。
    monkeypatch.setattr(Path, "rename", real_rename)
    assert packaging.export_package(run_dir) == package

    state = load_state(run_dir)
    report = json.loads(
        (run_dir / "run_report.json").read_text(encoding="utf-8")
    )

    # 独立核对包内哈希及审核绑定，不能只看状态名称。
    manifest = packaging._read_verified_package(package)
    assert manifest["status"] == "ready"
    assert manifest["package_version"] == 1
    assert state["package_version"] == 1
    assert state["status"] == report["current_status"] == "package_ready"

    # 已提交的包应直接复用，包括创建时间和全部文件字节。
    if committed_files is not None:
        assert {
            path.name: path.read_bytes()
            for path in package.iterdir()
        } == committed_files

    # 恢复打包不能重新调用创作模型。
    workflow.generate_plan.assert_called_once()
    workflow.generate_lyrics.assert_called_once()
    workflow.generate_composition.assert_called_once()
