# paper-agent

学术论文写作 Agent。以**意图驱动 + 写入前校验**为核心：把「润色这一节」「补一节相关工作」
这类自然语言请求解析成显式操作计划，执行前让你确认，写回前用 gate 拦住不该发生的改动。

面向的是**已有初稿**的场景 —— 读入你的 `.docx` / `.md` / `.tex`，在原文结构上改，
而不是从零生成一篇。

## 现状

- 297 个 Python 文件 / 约 6.7 万行 / **1941 个测试**（不含 `tests/eval`，本机约 3 分钟）
- CI：ruff + pytest（3.10 / 3.12）+ 无可选依赖时的可导入性检查
- **尚未发布**：见下方「已知限制」。真机链路跑通了，但部分能力仍在 flag 后面

## 装

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows；Linux/macOS 用 bin/activate
pip install -r requirements.lock                 # 钉死版本，可复现
pip install -e . --no-deps
```

`pyproject.toml` 的 `dependencies` 故意为空 —— `src/` 里所有第三方导入都是函数内惰性导入，
所以核心包零依赖就能 import（CI 有 job 守着这条承诺）。**但要真跑起来必须装 extras**，
上面的 `requirements.lock` 就是「实测全绿那一组版本」的完整闭包。

只想跑测试、不接真实模型：

```bash
pip install -e ".[all,dev]" && pytest tests/ -q --ignore=tests/eval
```

## 配

最少两个变量：选一个 provider，给它 key。

```bash
# .env（放仓库根目录，自动加载）
PAPER_LLM=openai              # openai | anthropic | qwen | deepseek | moonshot | zhipu | ctrip | mock
OPENAI_API_KEY=sk-...         # 变量名由 provider 决定，见下表
```

| `PAPER_LLM` | 需要的 key 变量 | 默认模型 |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| `qwen` | `DASHSCOPE_API_KEY` | `qwen-plus` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `moonshot` | `MOONSHOT_API_KEY` | `moonshot-v1-8k` |
| `zhipu` | `ZHIPU_API_KEY` | `glm-4-plus` |
| `ctrip` | `CTRIP_LLM_API_KEY` | `Qwen3.5-397B-A17B` |
| `anthropic` | `ANTHROPIC_API_KEY` | — |
| `mock` | 不需要 | 不调真实模型，测试用 |

自建 / 兼容端点：给 `PAPER_BASE_URL` + `PAPER_LLM_MODEL` 即可，无需改代码。
用 `PAPER_LLM_MODEL` 覆盖默认模型；`PAPER_KEY_ENV` 覆盖 key 的变量名。

常用可选项（完整清单见 `src/paper_agent/config.py`，共 60+ 个）：

```bash
PAPER_INTENT_PLANNING_MODE=off   # off | shadow | active（默认 off，走关键词路由）
PAPER_TRACE=true                 # 落 runs/<run_id>/trace.json
PAPER_LANGFUSE=true              # 额外上传 Langfuse，需 PAPER_TRACE 同开
                                 # 另需 LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY
                                 # 缺任一把 key 会静默退化为只落本地
PAPER_RETRIEVAL=openalex         # 文献检索源，默认 mock
PAPER_REVIEWER_LLM_MODEL=...     # 判相关性的模型，可与写作模型不同
```

## 跑

```bash
python scripts/chat.py 你的论文.docx
```

进 REPL 后直接说需求（「润色 4.2 节的语言」「在方法后面补一节实验设计」）。
系统会先给出操作计划让你确认,再执行。命令：

| 命令 | 作用 |
|---|---|
| `:sections` | 列出章节 |
| `:ask <问题>` | 只读问答，模型自己决定查什么，不改文档 |
| `:refs` | 按 DOI 查 Crossref，回填参考文献的刊名/卷/期/页（会改文档） |
| `:stats` | 本次会话的 Turn 归属统计（新栈 / shadow / 静默降级） |
| `:undo` | 撤销上一次修改 |
| `:save` | 落盘到源文件（**不 save 就不动原文件**）。docx 的引用存成 Word 交叉引用域，编号可在 Word 里 F9 更新；`--plain` 则存纯文本 `[n]` |
| `:export [路径]` | 另存一份交叉引用版到别的路径，源文件不动（如投稿版） |
| `:quit` | 退出，不自动保存 |

改动先落在 `.paper_workspace/<hash>/current.docx`（每步一个 git commit），
`:save` 才写回你的源文件。所以误操作的代价是可控的。

> **Windows 提示**：管道输入或重定向输出时加 `PYTHONIOENCODING=utf-8`，
> 否则非 ASCII 字符在 GBK 终端下会出问题（stdin 侧尚未在代码里兜住）。

## 架构

```
scripts/chat.py            REPL 入口
  └─ coordinator.py        一轮对话的编排
      ├─ intents/router    请求 → 意图（polish / rewrite / add_section / …）
      ├─ tasking/          意图 → 操作计划（planning_loop 可多轮自修）
      ├─ generate/         组装上下文 → 调模型产出候选文本
      ├─ gates/            写回前校验：diff / 格式 / 引用配额
      ├─ session/          文档投影（docx/md/tex → 带 # 标题的统一文本）
      ├─ literature/       文献检索、相关性判定、文献区重建
      └─ observability/    trace 落 runs/，可选导出 Langfuse
```

两个设计要点值得单独说：

**文档投影层**（`session/docx_document.py`）把三种格式都投影成 `#` 前缀的统一文本，
上层意图只跟这层打交道。docx 的 Word 交叉引用域在载入时摊平、保存时升回域，
所以编号引用不会在往返中退化成死文本。

**gate 是写回前的硬门槛**，不是事后检查。`diff_gate` 拦住润色时数字/引用/公式被改动，
`apply_text` 拦住写回会丢标题的文本 —— 后者是真实事故的产物：曾有一次润色让章节标题
吞掉正文首句、正文末句吞掉下一节标题，整节凭空消失。

## 意图

`polish`（只改语言）、`rewrite`（可改内容与论证）、`add_section`、`add_section_batch`、
`convert_format`、`format_docx`，加一个 `FALLBACK` 兜底 —— 写意图都不认领的请求交给
只读 ReAct 循环回答，而不是硬塞进某个写操作。

## 已知限制

- **`add_section_batch` 在 `active` 规划模式下不可达**（`operation_analyzer` 未支持），
  见 `docs/backlog.md` B-1 / B-3
- **默认 flag 与开发配置不一致**：代码默认 `PAPER_INTENT_PLANNING_MODE=off`，
  开发机 `.env` 用的是 `active`。发布态该用哪个尚未定
- **文献检索的中文配额常填不满**（`zh>=5` 一类约束），这是数据源覆盖问题而非 bug
- **发布门槛自锁**：原定 ≥200 条人工标注才允许默认开启 flag，实际标注 0/40，
  门槛需重定，见 `docs/backlog.md` D-7
- 锁文件无哈希校验（本机无 `uv` / `pip-compile`），防版本漂移但不防上游重打包

## 开发

```bash
pytest tests/ -q --ignore=tests/eval   # 1941 个，约 3 分钟
ruff check .                           # CI 同一套规则
```

`tests/eval/` 下是要打真实 API 的 dogfood / A-B 脚本，不进 CI，需手动跑。

`docs/backlog.md` 记录跨 feature 的问题池与决策留痕（含已修问题的根因分析），
`.kiro/specs/*` 是单个 feature 的 requirements / design / tasks。改动前值得先翻 backlog ——
很多「看起来该这么修」的地方,里面已经记了为什么不那么修。
