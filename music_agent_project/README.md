# 音乐创作 Agent

本地命令行项目，实现：

需求单 → DeepSeek 任务计划 → 作词 → 人工歌词审核 →
作曲方案与 mock 音频 → 人工音频审核 → 本地发行物料包及运行报告。

实现主 Agent、作词子 Agent、作曲/音频子 Agent。
终点为本地物料导出，不调用发行平台。

完整演示、异常分支和全部 CLI 参数见 [本地 Demo 与命令手册](docs/本地演示与命令手册.md)。

七项交付物的内容、组织方式和验收入口见 [项目交付说明](项目交付说明.md)。

第一次查看项目，可按目的选择入口：

| 想做什么 | 从哪里开始 |
| --- | --- |
| 直接查看已生成的歌曲物料和运行记录 | [交付示例](项目交付说明.md#五交付示例入口) |
| 无需密钥，验证程序完整流程 | 本文第 8 节，或 [无密钥模拟演示](docs/本地演示与命令手册.md#8-无密钥模拟演示与自动化测试) |
| 使用 DeepSeek 创建自己的项目 | 从下方安装与配置开始，再按第 4 节执行 |

## 1. 安装

在项目根目录打开终端，使用 conda 创建环境：

```powershell
conda env create -f environment.yml
conda activate music_agent
```

已有同名环境时，可更新依赖：

```powershell
conda env update -n music_agent -f environment.yml
```

当前完整测试通过的环境为 Python 3.11.5、
jsonschema 4.26.0、Pillow 12.1.1、pytest 7.4.0。
environment.yml 声明 Python 与依赖版本范围，使用 conda 统一创建运行环境。

## 2. 配置 DeepSeek

PowerShell：

```powershell
$env:DEEPSEEK_API_KEY = "替换为你的密钥"
```

程序从环境变量读取配置，不会自动加载 .env 文件。

上面的 `$env:` 写法适用于 PowerShell。如果使用 CMD 或默认以 CMD 打开的 Anaconda Prompt，请改用：

```bat
set "DEEPSEEK_API_KEY=替换为你的密钥"
set "PYTHONIOENCODING=utf-8"
```

两种写法都只设置当前终端；`python main.py ...` 和测试命令可以在两种终端使用。demo 中的 `$projectId`、`Get-Content` 等辅助命令需在 PowerShell 执行。

可通过 DEEPSEEK_MODEL 设置模型名称；未设置时使用客户端默认值。
计划、歌词和作曲方案需要请求 DeepSeek，可能产生 API 费用。

音频固定使用本地 mock，无需音乐模型密钥或独立服务。

## 3. 本地 mock 启动

直接运行创作流程时，音频阶段自动使用 mock，无需额外启动命令。

也可单独生成一段 3 秒测试音，无需 DeepSeek 密钥：

```powershell
python -c "from music_agent.clients.mock_music import generate_audio; print(generate_audio('outputs/mock_demo.wav', 3))"
```

目标文件已存在时会拒绝覆盖。

mock 音频为 440 Hz 固定测试音，采用 WAV、22050 Hz、
单声道、16 位 PCM。它用于验证文件与流程，
不包含真实演唱，也不呈现作曲方案中的编曲效果。

## 4. 完整 demo

所有命令在项目根目录执行。以下使用题目第二份正常需求单。

### 生成计划和歌词

```powershell
python main.py run --brief fixtures/briefs/song_002.json
python main.py status song_002
```

首次成功后状态为 lyrics_ready。
run 命令会输出运行目录，请查看其中的 lyrics.json 并人工审核。

### 审核歌词，生成作曲方案和音频

确认歌词通过审核后执行：

```powershell
python main.py approve --project song_002 --stage lyrics --reviewer "阿圣"
python main.py resume song_002
python main.py status song_002
```

成功后状态为 audio_ready。
查看运行目录中的 composition-v001.json、
composition-result-v001.json，并试听 audio-v001.wav。

### 审核音频并导出

确认作曲方案和测试音频满足当前流程要求后执行：

```powershell
python main.py approve --project song_002 --stage audio --reviewer "阿圣"
python main.py export --project song_002
python main.py status song_002
```

成功后状态为 package_ready，物料包位于运行目录的 release-package/。

示例审核人应替换为实际操作人员。
两次审核必须在检查对应产物后分别执行。

相同项目编号、相同需求重复 run 会复用已有项目。
已有项目应先查询状态，再执行对应阶段命令。
如需独立演示，可复制需求单并使用新的 project_id。

## 5. 拒绝与恢复

歌词不通过时，保存修改意见：

```powershell
python main.py reject --project song_002 --stage lyrics --reviewer "阿圣" --feedback "增加具体场景，副歌更克制"
python main.py resume song_002
```

程序保留旧稿和拒绝记录，生成新版本并重新等待审核。
当前歌词文件由 state.json 中的 lyrics_file 指定。

创作阶段发生错误后，通过 resume 重试。
程序会校验并复用已完成的检查点，避免覆盖已审核产物。

打包失败或打包过程中断后，重新执行 export。
物料缺失或校验失败时，不能生成 ready 物料包。

同一项目存在多个运行目录时，
status、approve、reject、resume、export 可使用 --run 指定目录名。
例如：

```powershell
python main.py status song_002 --run run-替换为实际目录名
```

## 6. 需求样例

- fixtures/briefs/song_001.json：毕业告别，流行摇滚，210 秒。
- fixtures/briefs/song_002.json：鲲游向天空，国风电子，180 秒。
- fixtures/briefs/song_003.json：异常样例，保留缺受众、时长及平台冲突。

异常需求单会在模型请求前被拒绝。
封面缺失允许先创作，但导出时必须补齐。

## 7. 运行产物

每次新建运行使用 outputs/run-<UUID>/。

按执行阶段保存：

- brief.json、plan.json：需求快照与已校验计划。
- lyrics.json、lyrics-v002.json 等：初稿及修订稿。
- state.json：当前状态、版本、哈希及审核记录。
- model-calls/：模型参数、响应摘要和失败记录。
- composition-v001.json：作曲方案。
- composition-result-v001.json：产物路径、版本、哈希及交接说明。
- audio-v001.wav：mock 音频。
- run_report.json：当前进度、状态历史及模型记录引用。

发行包结构：

```text
release-package/
  manifest.json
  lyrics.md
  composition.json
  audio.wav
  cover.png
  approvals.json
  README.md
```

manifest 记录文件大小与哈希、音频参数、项目名称、
版权声明、产物版本、brief 哈希、审核记录和创建时间。

版权声明说明 AI 辅助创作与 mock 音频的制作方式，
不代表已完成原创性鉴定。

本项目附带 `song_001` 的完整运行示例，包含发行包、运行报告、模型调用记录和作曲交接清单，具体入口见 [交付示例](项目交付说明.md#五交付示例入口)。

## 8. 自动化测试

测试使用模拟模型接口、本地 WAV 和 pytest 临时目录，**无需真实 API 密钥，不产生模型调用费用，也不改动交付示例**。

### 8.1 测试数据从哪里来

| 数据 | 来源 | 检查目标 |
| --- | --- | --- |
| 正常需求 song_001 | [fixtures/briefs/song_001.json](fixtures/briefs/song_001.json)：毕业告别、流行摇滚、210 秒 | 需求校验，以及生成、两次审核、导出与报告的完整模拟流程。 |
| 正常需求 song_002 | [fixtures/briefs/song_002.json](fixtures/briefs/song_002.json)：鲲游向天空、国风电子、180 秒 | 不同风格与目标时长下的流程、音频参数及物料完整性。 |
| 异常需求 song_003 | [fixtures/briefs/song_003.json](fixtures/briefs/song_003.json)：缺受众、时长冲突、主平台冲突 | 在模型请求前拒绝；测试逐项修复前置错误，确认后续冲突仍能被识别。 |
| 模型响应及故障 | 测试文件内的 fixture、Mock 和 monkeypatch | 构造合法计划/歌词/作曲方案，以及非法 JSON、超时、429 和提示注入等场景。 |
| 音频与封面 | 本地 mock 生成真实 WAV；部分场景在临时目录生成小尺寸 PNG | 检查实际格式、参数和哈希，并构造空文件、截断文件或损坏内容。 |
| 状态、审核及物料文件 | 各测试在 tmp_path 下创建 | 验证版本保护、审核绑定、失败恢复、重复执行与报告同步。 |

正常 brief 的完整流程保留原始目标时长；部分边界测试使用 1～2 秒音频，以缩短运行时间。模拟响应、故障数据和临时审核记录由测试代码构造，不要求手动准备额外数据文件。

### 8.2 如何运行

先激活环境，在项目根目录执行：

```powershell
conda activate music_agent
python -m pytest tests -q
```

按需要选择以下命令，**不必全部重复执行**：

| 目的 | 命令 |
| --- | --- |
| 运行最终七项验收测试 | `python -m pytest tests/test_acceptance.py -v` |
| 查看两份正常 brief 的完整模拟流程 | `python -m pytest tests/test_acceptance.py -k normal_brief_complete_workflow -v -s` |
| 检查三份原始需求单 | `python -m pytest tests/test_requirement_briefs.py -v` |
| 验证 CLI 完整流程 | `python -m pytest tests/test_review_gates.py -k cli_complete_workflow -v` |
| 验证打包提交前后中断恢复 | `python -m pytest tests/test_packaging.py -k recovers_after_crash -v` |
| 只列出测试，不执行 | `python -m pytest tests --collect-only -q` |
| 首个失败即停止，显示简短错误 | `python -m pytest tests -x --tb=short` |

`-q` 显示汇总，`-v` 显示每项用例，`-s` 显示程序输出，`-k` 按名称筛选测试。

### 8.3 各测试文件负责什么

| 检查范围 | 测试文件 |
| --- | --- |
| brief 校验与题目原始数据 | `test_brief.py`、`test_requirement_briefs.py` |
| DeepSeek 客户端及模型调用记录 | `test_deepseek.py`、`test_model_records.py` |
| 首次生成、音频失败及提交窗口恢复 | `test_initial_resume.py`、`test_resume.py`、`test_artifact_crash_recovery.py` |
| 同需求复用与冲突输入拦截 | `test_idempotency.py` |
| 歌词拒绝、修订及当前稿件选择 | `test_lyrics_revision.py` |
| mock 参数与实际 WAV | `test_mock_music.py` |
| 审核关卡、提示注入及 CLI | `test_review_gates.py` |
| 物料包完整性、元数据、哈希与打包恢复 | `test_packaging.py` |
| 报告同步、历史保留及写入保护 | `test_reporting.py` |
| 非法作曲方案、音频防覆盖及两份正常 brief 完整流程 | `test_acceptance.py` |

文件均位于 `tests/`。同一测试函数通过参数化检查多组输入，因此测试项数量与文件或函数数量不同。

### 8.4 如何理解结果

已记录的完整运行结果为 **202 项通过**。pytest 汇总中的 `passed` 表示断言符合预期；例如异常 brief 被拒绝、空音频被拦截，都属于测试通过。`failed` 或 `error` 才表示需要检查的测试失败或执行错误。

测试生成的项目与物料写入临时目录，与 `outputs/` 中的交付示例分开。它们用于验证程序行为，不作为正式保留的运行示例。

自动化测试验证结构、流程和文件约束；歌词是否贴合主题、原创要求和实际音乐效果，仍由人工审核判断。

## 9. 使用范围

项目按依赖顺序串行执行创作流程，支持歌词审核拒绝后的修订、已审核版本保护和失败步骤恢复。

音频使用本地 mock，交付终点为可校验的本地发行物料包。歌词内容与音乐效果通过人工审核确认。

## 10. 交付文档

- [项目交付说明](项目交付说明.md)：七项交付内容、文件组织和验收入口。
- [本地 Demo 与命令手册](docs/本地演示与命令手册.md)：完整流程、分支操作和全部 CLI 参数。
- [AI 使用与本人工作说明](docs/AI%20使用说明.md)：本人工作、AI 参与及检查结果。
- [开发日志](docs/Agent%20开发日志.md)：开发过程、问题修正及测试记录。
