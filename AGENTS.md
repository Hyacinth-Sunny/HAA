# AGENTS.md — HAA (Hyacinth Automated Analyzer)

自动科研工具：研究简报 → 论文前体（P1）→ 实验执行（P2）→ LaTeX 论文（P3）。Python 3.12 · litellm · SQLite · FastAPI/Jinja2/HTMX。当前版本 v1.0.6（批次已至 rev5，逐版见开发日志）。

**★用语铁律（用户 2026-10-07 指示，逐字）**：无论是在回答还是在你编写的、具有告知书性质的文件中，都应尽量避免出现任何的自造词、生僻概念、没有或隐含语境的比喻，及简略性过强的表述，如果一定要用这些词语及表述来阐述新概念，或者这些词语及表述需要用来代替某些拖沓冗长的词汇或句子，请一定要在回答的最前面，用通俗易懂的语言逐条阐述这些词的具体含义。

最新冒烟 smoke9（2026-08-28，rev4 修复首验）：3/3 前体 PUBLISHED、纯运行 7h22m 零阻断（②流式续体 0.7125 accept·correctness 0.7 历史最高、③见证账本 0.7125 accept·fidelity 0.95·英文 55K、①批量切口 0.6925 reject degraded）。

**大修进行中（2026-10-07 起）**：权威文档为 `MDs/HAA大修计划书01~06`；已入 main 的批次见开发日志"大修批次"条目（批次0 基线检修、批次1 M0 Harness 核心，基线 509）。大修期间本文件与开发日志同步更新。

## 仓库布局（2026-10-06 重组）

- 除 README.md 外的历史 MD 文档统一归档在 `MDs/`（交接文档/说明书/开发日志/计划书/冒烟报告等）。**MDs 内容可能过旧**，与代码现状冲突时以代码 + 测试为准；`MDs/AGENTS.md` 仅是归档副本，**根目录本文件才是权威版**
- `ref/` = 其他自动科研工作流的参考资料（`ref/AutoSci/`、`ref/DSH/deepseek-harness`），供后期修订 HAA 时取用
- 代码最后活动 2026-08-28（rev5 收尾），其后至 2026-10-06 仓库无代码改动；开发日志与代码同步停在 rev5

## 必读文档（按序，均在 MDs/ 下）

1. `MDs/交接文档-ZCode.md` — 接手第一入口：现状快照、冒烟三步、值守红线、检修流程（注意：其快照停在 smoke8/v1.0.5/416 基线，之后的脉络以 `MDs/HAA开发日志.md` 为准）
2. `MDs/HAA项目说明书.md` — 系统全貌（宏状态机/P1 十二关/四路 lens）；由 `/about` 页渲染，版本号三处同步（说明书 / about 页 / footer）
3. `MDs/HAA开发日志.md` — 逐版本脉络（最新条目 v1.0.6-rev5）；每批次工作后追加条目

## 常用命令

```bash
PY=/home/hyacinth-sunny/anaconda3/envs/haa/bin/python   # 唯一正确的 python（勿用 base/系统）
$PY -m pytest                # 全量测试（当前基线 509；大修批次1 起）
$PY data/e2e_check.py        # 端到端体检三层（pytest + Web 路由 + 简报管线），退出码 0=全绿
./run_server.sh              # Web :8420（uvicorn api.server:app）
```

- CLI：`$PY -m haa.cli.main project create|start|resume-p1|approve|advance ...`
- 冒烟：`HAA_CONFIG=config/hatm-v2.yaml` + `DEEPSEEK_API_KEY`/`TAVILY_API_KEY`；**GLM 通道（glm-flash.yaml）仅作显式批准下的备选**——智谱按量 API 未开通（2026-10-07 用户澄清），当前所有 GLM API 只能走 Coding Plan 积分额度，与开发用量账目隔离原则冲突，未经用户显式批准不得用于冒烟/开发。**key 现场来源（2026-10-07 核实）**：DEEPSEEK=`data/smoke10_deepseek.env`（gitignored，600）；TAVILY=`~/.config/systemd/user/openclaw-gateway.service`
- 提示词调试：`$PY -c "from haa.prompts import render_prompt; ..."` 直接渲染查看

## 检修流程（硬性惯例）

1. **改前**先跑 `$PY data/e2e_check.py` 确认全绿；红项先归因再动手
2. 定位用最小复现：读具体报错/日志行（不猜）；Web 问题用 TestClient 单测复现（参考 `tests/test_server*.py`）
3. 小步修；**每修一个 bug 补一个锚定测试**（防回归惯例）
4. **改后**再跑 e2e_check 全绿收工，新增测试计入新基线
5. `MDs/HAA开发日志.md` 追加批次条目

改任何 `haa/`、`api/`、`prompts/`、`frontend/`、`config/` 之后必跑 e2e_check。

## 目录结构

```
haa/            核心包：pipeline.py(P1状态机) · project_controller.py(宏状态机) · stages/ · llm/(client,agent_loop,tools) · brief_schema/compiler/io · memory · artifacts · models/ · p2/ · p3/ · cli/
prompts/        各阶段 Jinja2 提示词；review/=四路物理致盲 lens（correctness/quality/industry/fidelity）
config/         default.yaml · hatm-v2.yaml（冒烟配置）· glm-flash.yaml
data/           haa.db · campaigns/<cid>/(artifacts/+knowledge/) · 研究简报模板.md · e2e_check.py
frontend/       templates/ · static/
api/server.py   FastAPI 全部路由
MDs/            历史 MD 文档归档（可能过旧，以代码+测试为准）
ref/            其他自动科研工作流参考资料（AutoSci · DSH/deepseek-harness）
```

## 红线与关键约束

- 一切改动前跑测试、改后跑全量；基线掉绿先修再走
- **不主动杀运行中的进程**（除非用户明说）；长跑冒烟挂相位感知保姆（只复活不杀）
- 简报是**死格式 md**（模板 `data/研究简报模板.md`）；改简报后必须同步 `data/knowledge/hatm-research-brief-full.md`（cat 头两行 + 原文）
- 简报铁律块（约束逐字+排除方向+知识清单）由 `BaseStage._brief_block` 注入全部十个阶段 prompt
- **证据文化**：质量判断必须给可指认落点（行号/判决原文/表格），先证据后结论
- **工具层**（16 个）：v1.0.6-rev2 起 grep 入列（知识文件先检索后窄读）；**rev3 起 fetch_paper_fulltext 入列**（全文六级发现链：本地库 opt-in→OpenAlex→arXiv→Unpaywall→PMC→S2，配置 `tools.fulltext.*`，本地库模式需用户显式开 enabled+dir）；工具调用统计查 `GET /api/tool-stats?campaign_id=`（stage_tool_limits 调参数据源）；出站 URL 仅 http/https 且拒内网地址（含 DNS 解析后 IP 边界校验）；真实链路冒烟用 `data/probe_fulltext.py`
- 与用户交流用中文；关键结论用对照表
- 读日志顺序：启动 banner（配置核对）→ `babysitter:` 行 → `truncated` 行（上下文帽不够）
- DeepSeek 欠费以 non-retryable 错误杀进程——先 `resume-p1` 再挂保姆

## 遗留问题（优先级序）

1. **WRITE 语言/体量锚定**（smoke9 ①bg=0/method 11K 中文成文，同 run ③却英文全规格 55K——语言与体量都需 prompt 收紧；P0）
2. **EXP 瘦身三件套**（smoke9 EXP 循环 207min/21.4M token，单次 FEASIBILITY 输入峰值 798K——上下文滚雪球，帽不是瓶颈工作模式才是；P0）
3. fidelity lens 放过"缺失型"偏离（smoke8-c2 单侧面候选；修 `prompts/review/fidelity.md`：简报要求全链路/联合时缺失即重大偏离）
4. 论文形式化深度（rev5 G 节挂账：透明性五规则、简报双层条款、DESIGN 定义清单义务、WRITE 可消化性标准；16 包笔记需形式化重写）
5. cost 记账半盲（tokens 可见、$ 仍 0，deepseek 流式不回 usage）
6. 均分制双刃（低分 correctness 可被拉回过审）
7. 用户待确认简报条款："任务一使命显式化"（8-29 后）
8. **大修 M0 兼容挂账两件（用户 2026-10-07 指定，工具部分开发完毕后必须清账）**：①工具桥接退役——`haa/harness/tools_bridge.py` 与 `haa/llm/tools.py` 旧注册表在 16 工具逐个迁入 `haa/harness/tools/` 后删除；②提示词"楼层化"投稿——各 stage 的 Jinja2 模板与 `_brief_block` 字符串拼接改为向 `haa/harness/prompt_sections.py` 楼层注册表投稿（楼层 200+，简报铁律块归楼层 0），`haa/prompts.py` 静态渲染通道随之退役

8. 语言润色模块（OpenClaw smoke10 检视建议不增设；用户 2026-10-08 定：**属 P3 部分，记账**——P3 成稿阶段若 quality 失分以措辞为主，在 P3 成稿前接入，REFINE 专注论证）
9. DeepSeek 价目已入配置（2026-10-08 实取，v4-pro 高峰价保守折美元 1.30/3.80）；**模型二选一**：deepseek-v4-pro（现用）或 deepseek-flash（V4.1-Flash，并发 2500、价约 1/3.4，备查折价 0.30/1.15）

完整 v1.1 议程见 `MDs/HAA项目说明书.md` 第七节。
