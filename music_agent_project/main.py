"""音乐创作 Agent 的命令行入口。

负责解析命令、调用业务模块及保存计划，不包含需求单和模型校验逻辑。
运行示例：python main.py run --brief brief.json
"""

import argparse
import sys
from urllib.error import HTTPError, URLError

from music_agent.brief import load_brief
from music_agent.workflow import run_creation, resume_creation
from music_agent.storage import find_run_directory, load_state
from music_agent.review import approve_lyrics, approve_audio, reject_lyrics
from music_agent.packaging import export_package
def main():
    """读取需求单、生成任务计划并保存到本地。
    Returns:
        成功时返回退出码 0，读取、生成、校验或保存失败时返回 1。
        命令行参数错误由 argparse 处理，并以退出码 2 结束程序。
    """
    # 创建命令行参数解析器，description 是帮助信息中的程序说明。
    parser = argparse.ArgumentParser(description="音乐创作 Agent")
    # 创建子命令集合；子命令名称存到 args.command，且必须输入子命令。
    commands = parser.add_subparsers(dest="command", required=True)
    # 注册 run 子命令，并为它创建独立的参数解析器。
    run = commands.add_parser("run", help="读取歌曲需求单并生成任务计划")
    run.add_argument("--brief", required=True, help="JSON 需求单路径")
    # 注册人工审核命令，目前仅支持歌词审核。
    approve = commands.add_parser("approve", help="人工审核通过")
    approve.add_argument("--project", required=True, help="项目编号")
    approve.add_argument("--stage", required=True, choices=["lyrics", "audio"])
    approve.add_argument("--reviewer", required=True, help="审核人姓名")
    approve.add_argument("--run", help="可选的运行目录名，例如 run-xxxx")

    # 注册状态查询命令，优先通过项目编号查找。
    status = commands.add_parser("status", help="查询项目状态")
    status.add_argument("project_id", help="项目编号")
    status.add_argument("--run", help="可选的运行目录名")
    # 注册 resume 子命令，用于继续执行项目流程。
    resume = commands.add_parser("resume", help="继续项目流程")
    resume.add_argument("project_id", help="项目编号")
    resume.add_argument("--run", help="可选的运行目录名")
    #注册 export 子命令，导出本地发行物料包
    export = commands.add_parser("export", help="导出本地发行物料包")
    export.add_argument("--project", required=True, help="项目编号")
    export.add_argument("--run", help="可选的运行目录名")
    #注册 reject 子命令，拒绝审核并记录修改意见
    reject = commands.add_parser("reject", help="拒绝审核并记录修改意见")
    reject.add_argument("--project", required=True, help="项目编号")
    reject.add_argument("--stage", required=True, choices=["lyrics"])
    reject.add_argument("--reviewer", required=True, help="审核人姓名")
    reject.add_argument("--feedback", required=True, help="修改意见")
    reject.add_argument("--run", help="可选的运行目录名")
    # 解析用户实际输入的命令，并检查子命令、必填参数是否合法。
    args = parser.parse_args()
    # 查询完成后直接返回，不进入审核或生成流程。
    if args.command == "status":
        try:
            run_dir = find_run_directory(args.project_id, args.run)
            state = load_state(run_dir)
        except (OSError, ValueError) as error:
            print(f"查询失败：{error}", file=sys.stderr)
            return 1
        print(f"运行目录：{run_dir}")
        print(f"项目：{state.get('project_id')}")
        print(f"状态：{state.get('status')}")
        print(f"歌词版本：{state.get('lyrics_version', '未记录')}")
        approval = state.get("lyrics_approval")
        if isinstance(approval, dict):
            print(f"审核人：{approval.get('reviewer')}")
            print(f"审核时间：{approval.get('approved_at')}")
        return 0
    # 根据指定阶段审核歌词或音频，处理完成后结束本次命令。
    if args.command == "approve":
        try:
            run_dir = find_run_directory(args.project, args.run)
            # 根据人工指定的阶段调用对应审核函数。
            if args.stage == "lyrics":
                approve_lyrics(run_dir, args.reviewer)
            else:
                approve_audio(run_dir, args.reviewer)
        except (OSError, ValueError) as error:
            print(f"审核失败：{error}", file=sys.stderr)
            return 1
        print(f"{args.stage} 审核通过：{run_dir}")
        return 0
    if args.command == "resume":
        try:
            run_dir = find_run_directory(args.project_id, args.run)
            resume_creation(run_dir)
        except (OSError, ValueError) as error:
            print(f"继续执行失败：{error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "export":
        try:
            run_dir = find_run_directory(args.project, args.run)
            package_dir = export_package(run_dir)
        except (OSError, ValueError) as error:
            print(f"导出失败：{error}", file=sys.stderr)
            return 1

        print(f"发行物料包已生成：{package_dir.resolve()}")
        return 0
    if args.command == "reject":
        try:
            run_dir = find_run_directory(args.project, args.run)
            reject_lyrics(run_dir, args.reviewer, args.feedback)
        except (OSError, ValueError) as error:
            print(f"审核拒绝操作失败：{error}", file=sys.stderr)
            return 1
        print("歌词已拒绝，修改意见已保存。")
        return 0


    try:
        brief = load_brief(args.brief)
    except (OSError, UnicodeError, ValueError) as error:
        # 错误输出到标准错误流，并返回失败退出码，便于终端或脚本识别。
        print(f"读取失败：{error}", file=sys.stderr)
        return 1

    print("需求单基础校验通过")
    print(f"项目：{brief['project_id']}")
    print(f"主题：{brief['theme']}")
    print(f"风格：{brief['genre']}")
    print(f"情绪：{brief['mood']}")
    print(f"时长：{brief['duration_seconds']} 秒")
    try:
        run_dir = run_creation(brief)
        print(f"产物目录：{run_dir.resolve()}")
    except HTTPError as error:
        print(f"DeepSeek 请求失败：HTTP {error.code}", file=sys.stderr)
        return 1
    except (OSError, URLError, ValueError) as error:
        print(f"创作流程失败：{error}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
