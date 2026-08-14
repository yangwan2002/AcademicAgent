# v2 意图卡片集

学术论文写作 agent v2 的产品规格。每张卡定义一个意图，包含 Router 判据、SOP、Interview、工具白名单、Diff Gate 规则、意图升级路径。

**约定**：所有意图作用于 working copy（`.paper_workspace/<session_id>/current.<ext>`）；源文件永不动，直到用户明确 `save`。`convert_format` 例外，产出新文件。

## 意图集合概览（5 个）

| # | 意图 | 说明 |
|---|---|---|
| 1 | `polish` | 语言润色（不动事实/数字/引用/结构） |
| 2 | `rewrite` | 内容改写（含搜文献子流程） |
| 3 | `add_section` | 新增章节（含访谈+搜文献+生成子流程） |
| 4 | `convert_format` | 跨格式转换（产出新文件） |
| 5 | `format_docx` | 格式排版操作（Layer 1 结构化工具 + 逃生舱） |

**跨意图共享设施**：
- Facts 系统（`facts.json`）：渐进式积累声明/数字/术语/文献，替代 v1 的 artifact
- 共享子 SOP：搜文献 + 核验 + 引用（供 rewrite / add_section 引用）
- ~~共享工具：`fix_bibliography`~~ —— **已废弃**（backlog B-2）。文献区同步改由
  `literature/bibliography_sync.py:apply_bibliography` 在 rewrite / add_section
  commit 时自动承担（与正文同一次写入，`:undo` 原子）；用户原稿的悬空引用改由载入时
  只读诊断报告（`literature/dangling_refs.py`）。下方「共享设施 C」整节同此。

---

## 卡 1 · `polish`

**目标**：语言润色，不改事实/数字/引用/结构。

### 1. Router 判据

- **关键词**：
  - 中文：润色 / 改简洁 / 通顺 / 语法 / 语言 / 精炼 / 别扭 / 通读语言 / 读起来
  - 英文：polish / refine / rewrite for clarity / smooth
- **上下文信号**：句中含具体章节名 / [ref] 引用位置。
- **冲突消解**：
  - 同时命中 `rewrite`（"重写" / "补论证" / "换叙述"）→ 走 `rewrite`
  - 同时命中 `format_docx`（"字体" / "行距" / "样式"）→ 走 `format_docx`
  - 命中 polish 但章节名不存在 → 拒绝并列出实际章节

**分派子模式**（Router 内部）：
- 文件为 `.docx` / `.tex` 且未提"重排" → **inplace 实现**（保 OOXML / LaTeX 结构）
- 其他 → **结构化实现**（走 section 表示 + 重导出）

### 2. SOP

a. [代码] 从用户话抽出 section 引用（章节名 / id）
b. [代码] 若未指明章节 → Interview a1
c. [代码] 读取目标章节原文
d. [Interview] 见第 3 节
e. [LLM 单次] 输入 { 章节原文, interview 答案, 保真硬约束 prompt } → 返回新正文
f. [代码] 计算 diff
g. [代码] 跑 Diff Gate（见第 5 节）
h. [代码] 未过 → 回灌违反规则给 LLM retry（≤2 次）
i. [代码] 通过 → 写入 working copy + git commit
j. [代码] 展示 diff 摘要给用户

### 3. Interview

- **必问（SOP 硬编码）**：
  - `a1`（仅当章节未指明）："润色哪一章？" [列出章节 id + 标题选项]
- **LLM 允许追问 ≤ 1 条**：
  - 触发：用户话里带模糊限定词（"简洁点" / "顺一下"）
  - 格式：必须带"为什么问"的一句话理由
  - 示例："你说'简洁点'——是想砍多少？(a) 只删冗余 (b) 缩短 30% 左右 (c) 缩短一半。我问是为了避免把关键论据也删掉。"
- **含糊处理**：追问一次后仍含糊 → 用最保守默认（"只删冗余"），从不硬拒绝。

### 4. 工具白名单

| 工具 | 用途 |
|---|---|
| `list_sections()` | 返回章节 id + 标题 |
| `read_section(anchor)` | 返回章节正文 |
| `polish_section(anchor, goal)` | 单次 LLM 生成 + gate |
| `show_diff()` | 展示 working vs original |

★ 不暴露的工具：`add_section` / `add_refs` / `convert_document` / `rewrite_section` / `format_*`。

### 5. Diff Gate（代码硬校验）

**通过条件（全部满足）**：
- ✓ 章节数量不变
- ✓ Markdown / LaTeX 标题层级不变
- ✓ 所有 [ref-id] 引用集合逐字相同
- ✓ 所有数字（`\d+(\.\d+)?`）集合逐字相同
- ✓ 所有公式（`$..$`, `$$..$$`, `\begin{equation}` 等）逐字保留
- ✓ 总字符数变化 ≤ 30%
- ✓ 无占位符（TODO / 待补充 / xxx / `[待填]`）

**Retry 策略**：
- 失败 → 回灌违反规则条目 + 原文 + LLM 上次输出："你违反了 X 规则，重新生成"
- 最多 retry 2 次

**最终失败**：
- 任务失败，如实告诉用户："我 3 次尝试都没能只改语言不动数字/引用，可能这段实际需要 rewrite 模式。要切换吗？" → 升级到 `rewrite`（用户确认）

### 6. 意图升级 / 降级路径

- Polish gate 连续失败 → 询问用户升级到 `rewrite`（**用户明确同意才切；不自动切**）
- 用户中途说"算了我自己改" → 保留 working copy 未落盘状态，任务结束
- Router 阶段发现是格式类需求 → 拒绝并推荐 `format_docx`

---

## 卡 2 · `rewrite`

**目标**：改写目标章节的内容（含允许换/加引用、补论证、调结构），仅限单章范围。

### 1. Router 判据

- **关键词**：
  - 中文：改写 / 重写 / 补论证 / 换叙述 / 重新组织 / 改结构 / 展开 / 详细写 / 改一下逻辑 / 补一段 / 加一段
  - 英文：rewrite / restructure / expand / elaborate / add discussion
- **上下文信号**：
  - 句中含明确"改动幅度大于语言"的动词
  - 或包含"补充/新增论据/新增段落"
  - 或含"加 N 篇文献到 X 章节"（管理引用被融入本意图）
- **冲突消解**：
  - 同时命中 `polish`（"通顺"/"精炼"）→ 走 `polish`（保守优先）
  - 命中 `add_section`（"新增章节"/"补一章"/目标章节不存在）→ 走 `add_section`
  - 目标章节 = 参考文献章节 → 拒绝（防编造文献；改文献格式走 `format_docx`）

### 2. SOP

a. [代码] 从用户话抽出目标章节
b. [代码] 未指明章节 → Interview a1
c. [代码] 读取原文
d. [Interview] 必问 + 允许追问（见第 3 节）
e. [代码] 判断是否需要搜文献（用户提"加文献"/interview 追问出"需要新论据"）→ 若是则调用**共享子 SOP：搜文献 + 核验 + 引用**（见文末）
f. [代码] 拉取本章节可用 refs（verified 集合 = 原有 + 本轮新增）
g. [代码] 拉取 `facts.json`（known_numbers / claims / known_terms）供 grounding
h. [LLM 单次] 输入 { 原文, 用户意图, verified refs 列表, facts 摘要, 保真约束 prompt } → 返回新正文
i. [代码] 计算 diff
j. [代码] 跑 Diff Gate（见第 5 节）
k. [代码] 未过 → 回灌违反规则 retry ≤ 2 次
l. [代码] 通过 → 写入 working copy + git commit
m. [代码] 更新 `facts.json`（本次 rewrite 中经用户确认的新数字并入 `known_numbers`）
n. [代码] 展示 diff 摘要给用户，标出"新增了 N 处引用 / M 处新数字"

### 3. Interview

- **必问（SOP 硬编码）**：
  - `a1`（章节未指明）："改哪一章？"
  - `a2`（永远必问）："你希望改动的方向是什么？"
    - (a) 补充论证 / 加论据
    - (b) 调整段落顺序 / 结构
    - (c) 更换叙述角度（如从方法优先→问题优先）
    - (d) 其他，请说明
- **LLM 允许追问 ≤ 2 条**：
  - 场景：用户方向选了但缺关键信息
  - 选 (a) 补充论证 → 追问："想补什么论据？"
    - (i) 数学分析 (ii) 更多实验对比 (iii) 引用现有工作 (iv) 具体例子
    - 若选 (iii)：进一步问关键词，随后触发**搜文献子 SOP**
  - 选 (c) 换叙述角度 → 追问："改成什么角度？举例。"
  - 若原文中出现关键 claim 而 `facts.claims` 里没有 → 追问："这一段的核心 claim 是 X，对吗？确认后我会记住供后续复用。"
- **含糊处理**：追问一次后仍含糊 → 任务失败（rewrite 不宽松）。诚实告诉用户："我需要更明确的方向才能改写，否则容易改成你不想要的。你可以：(1) 明确一下方向再说 (2) 试试 polish 意图"

### 4. 工具白名单

| 工具 | 用途 |
|---|---|
| `list_sections()` | 返回章节 id + 标题 |
| `read_section(anchor)` | 返回章节正文 |
| `list_available_refs()` | 返回本章可引用的 verified refs |
| `search_refs(query, limit)` | **共享子 SOP** 内部使用；返回候选文献 |
| `verify_refs(candidates)` | **共享子 SOP** 内部使用；核验并入库 |
| `rewrite_section(anchor, direction, details)` | 单次 LLM 生成 + gate |
| `confirm_new_number(value, context)` | Gate 触发；用户确认新出现的数字来源 |
| `show_diff()` | 展示 working vs original |

★ 不暴露：`add_section` / `convert_document` / `format_*` / `polish_section`。

### 5. Diff Gate（代码硬校验）

**引用格式说明**：引用语法由 working copy 的文件扩展名决定：
- `.tex` → `\cite{bibkey}`；`bibkey` 必须在 `facts.known_refs[*].bibkey` 集合中
- `.docx` / `.md` → `[id]`；`id` 必须在 `facts.known_refs[*].id` 集合中
- Gate 不硬编码某种语法，只校验"引用都能解析到 known_refs 的某条记录"

**通过条件（全部满足）**：
- ✓ 只有目标 section 被改动，其他 section 逐字不变
- ✓ 所有引用（无论 `\cite{bibkey}` 还是 `[id]`）都能解析到 `known_refs` 的某条记录（不新增未核验）
- ✓ 无占位符（TODO / 待补充 / xxx / 具体请补充 / [待填]）
- ✓ **新出现的数字必须满足以下之一**：
  - 在 `facts.known_numbers` 里且未被删除
  - 经 `confirm_new_number` 用户确认
  - 否则视为编造 → 拒绝
- ✓ 段落数在 [原-2, 原+5] 之间（防过度膨胀）
- ✓ 章节标题未改（改标题另做 `rename_section` 工具，不在 rewrite 内）

**Retry 策略**：
- 失败 → 回灌违反规则条目 + 原文 + LLM 上次输出 + 具体违规位置
- 最多 retry 2 次

**最终失败处理**：
- 诚实告诉用户具体违反了哪几条
- 分类建议：
  - "编造了未核验引用" → 已尝试触发搜文献子 SOP 但未找到 / 用户拒绝了新引用 → 建议改叙述避开该 claim
  - "编造了数字" → 用 `confirm_new_number` 让用户确认，或用户提供 `facts.json` 里没有的数字需先在 chat 中说明来源
  - "改动扩到别的 section" → 提示拆成多次 rewrite

### 6. 意图升级 / 降级路径

- Interview 追问后仍无明确方向 → 建议降级 `polish`
- 目标章节不存在 → 提示切换 `add_section`
- 改动范围明显超出单章 → 拒绝，让用户逐章操作
- 用户说"顺便把 X 也改了" → 拒绝（严守单章边界）
- 用户说"帮我修一下 bibliography 缺条目" → 触发共享工具 `fix_bibliography`，与本意图无关

---

## 跨意图共享设施 A · Facts 系统

**动机**：v1 的 `ResearchArtifact` 要求用户启动前提供严格 YAML+CSV（20+ 必填字段，等于先写完论文再来 chat），门槛过高。v2 改为**渐进式积累**——从源文件自动抽 + 对话中收集 + 用户环内校验。

### 数据结构

`.paper_workspace/<sid>/facts.json`（所有字段可选）：

```json
{
  "known_refs": [                // 已核验文献（Layer 1 抽取 + rewrite 中新增）
    {"id": "1", "bibkey": "vaswani2017attention", "title": "...",
     "authors": [...], "year": 2023, "verified": true}
  ],
  "known_numbers": [             // 已确认数字（含来源）
    {"value": "0.87", "source": "table_3.row_2", "confirmed": true}
  ],
  "known_terms": ["GNN", "attention"],  // 术语规范写法（供 polish/rewrite 统一用词）
  "claims": {                    // Interview 累积的核心声明
    "novelty": "首次证明 X 方法在 Y 场景下比 Z 好",
    "gap": "现有工作忽略了 W 的影响",
    "contributions": ["...", "..."],
    "baselines": ["ResNet", "Transformer"],
    "section_structures": {       // 用户历史给过的章节结构偏好（供 add_section 复用）
      "introduction": "背景 → gap → 我们做了 → 贡献列表",
      "method": "notation → problem statement → approach → analysis"
    }
  }
}
```

`bibkey` vs `id` 说明：
- `.tex` 里正文用 `\cite{bibkey}` → gate 校验 bibkey 集合
- `.docx` / `.md` 里正文用 `[id]` → gate 校验 id 集合
- 两者在 `known_refs` 里一一对应，导入/搜索/`convert_format` 时同步填充

### 三层积累机制

**Layer 1 · 从源文件自动抽取（零门槛，session 起始时一次）**
- 扫描 bibliography → 初始 `known_refs`
- 扫描表格/图注 → 数字入 `known_numbers`（`confirmed=true`，来源标注为原文位置）
- 词频统计 → 高频术语入 `known_terms`
- 用户什么都不用做

**Layer 2 · Interview 时收集（低门槛，每次意图执行时）**
- Rewrite / add_section 的 Interview 里，SOP 主动问"这段的核心 claim 是什么"、"novelty 一句话"等
- 用户回答存进 `claims`
- 下次相关意图执行时，SOP 优先从 `claims` 取，避免重复问
- 例：写 introduction 时问过的 novelty，写 conclusion 时直接复用

**Layer 3 · 用户环内校验（Diff Gate 触发）**
- Rewrite/add_section 的 gate 检测到"新出现的数字"→ 调 `confirm_new_number(value, context)` 工具
- 用户回答：
  - "对，来自我的 table 3" → 存进 `known_numbers`（`confirmed=true, source="user_input"`）
  - "不对，删掉" → gate 拒绝，触发 retry
  - "改成 0.85" → 用户直接给正确值，gate 通过

### 与 v1 artifact 的替换关系

| v1 概念 | v2 对应 |
|---|---|
| `ResearchArtifact.method.overview` | 首次 rewrite/add method 章节时 Interview 收集，存 `claims.method_summary` |
| `ResearchArtifact.contributions[]` | 写引言/conclusion 时 Interview 收集，存 `claims.contributions` |
| `ResearchArtifact.experiments[].results_data.rows` | Layer 1 从表格抽取 → `known_numbers` |
| `ResearchArtifact.novelty_claims` | `claims.novelty`（Interview 收集） |
| `ResearchArtifact.method.pseudocode` | 不做（若用户想让 agent 生成伪代码，属于 rewrite 的 target） |
| `evidence_ids` / `artifact_hash` 强绑定 | 不做（改用 diff gate 的 known_numbers 检查） |

### 模块估算

- `SessionFacts`（读写 facts.json）：~100 行
- `AutoExtractor`（源文件抽 Layer 1）：~150 行
- Interview 的 `save_claim` 钩子：~30 行
- Diff gate 中 `check_new_numbers` 规则 + `confirm_new_number` 工具：~50 行

**新增合计 ~350 行，删除 v1 `research_artifact.py`（492）+ `artifact_loader.py`（261）+ 各处 `ws.artifact` 引用 ≈ 800 行。净负 450 行**。

---

## 跨意图共享设施 B · 子 SOP：搜文献 + 核验 + 引用

**动机**：rewrite / add_section 都需要"顺手加文献"的能力，抽成共享子 SOP 避免重复。

### 触发条件

任一意图的 SOP 中判定：
- 用户明确要求"加 N 篇 XX 主题的文献"
- Interview 追问出"需要新论据"并选了"引用现有工作"
- LLM 生成时若引用了未核验 id → gate 拒绝 → 提示"要不要触发搜文献子 SOP 补进来"

### 子 SOP 步骤

a. [代码/Interview] 收集参数：
   - `query`（关键词，中文自动转英文；用户没给则从上下文推断并让用户确认）
   - `limit`（默认 5；用户说"3 篇"则用 3）
   - `target_section`（哪一章要用，供 gate 判定是否引用了这批新 refs）

b. [代码] 调 `search_refs(query, limit)`：
   - 走 retrieval provider（OpenAlex / arXiv / api）
   - 返回候选文献元数据（title / authors / year / doi / abstract）

c. [代码] 调 `verify_refs(candidates)`：
   - 每条走 `CitationVerifier`
   - 可核验的入库，不可核验的丢弃并如实告诉用户"请求 N 条，核验通过 M 条"

d. [代码] 分配编号：
   - `id`：续接现有 `known_refs.id` 最大值 + 1（供 `.docx`/`.md` 用 `[id]`）
   - `bibkey`：从元数据生成稳定 key（如 `firstauthor+year+firsttitleword`；冲突则加后缀）（供 `.tex` 用 `\cite{bibkey}`）
   - 一条 known_refs 同时持有两者，供不同 working ext 使用

e. [代码] 写入 `facts.known_refs`（去重：优先按 doi，其次按 title+year）

f. [代码] 返回给上层 SOP：新加入的 `[id]` 列表 + 每条简介，供 LLM 生成正文时使用

### 退化用法（用户只想加文献不改正文）

**该场景在 v2 中已删除**——加文献必伴随"要在正文引用"这个前置目的，不做独立场景。

### 失败处理

- 检索限流 / 网络错误：告诉用户，不重试轰炸
- 0 条可核验：诚实告诉"未找到可核验的相关文献"，让上层意图决定继续还是放弃
- 用户说"别搜了直接写"：立即停止子 SOP，上层意图继续（可能触发 gate 拒绝未核验引用；此时会走 rewrite gate 失败路径）

### 与 v1 的差异

- v1 有独立的 `add_references` / `verify_existing_references` 工具 + 独立的 CHANGE_CITATION 护栏通道
- v2 把它们合并成一个子 SOP，作为 rewrite/add_section 的内部步骤，不再是"独立意图"

---

## 跨意图共享设施 C · `fix_bibliography` 工具 —— ⚠️ 已废弃（B-2）

> **本节保留为设计史，不再描述现行系统。**
>
> 实现（`tools/bibliography.py`）与 `Intent.FIX_BIBLIOGRAPHY` 已删除。理由见
> `docs/backlog.md` B-2：它只支持 .tex、只写题名（authors/year/DOI 全丢），且
> 单独一次 `write_working` 会让 `:undo` 留半截状态。
>
> 现行形态分两半：
> - **写入侧**：`apply_bibliography` 在 commit 时按正文实际引用重建文献区，走
>   `citation_styles` 渲染完整著录，与正文同一次写入。
> - **诊断侧**：`diagnose_dangling` 在载入时只读报告悬空引用（下面「触发方式」
>   第二条的自动扫描就是它，但**只报告不修**——缺元数据时补不了）。
>
> 下面的「操作步骤」c/d 两步（问用户 / 搜文献补元数据）**没有实现**，也不打算按
> 这个形态实现：交互式补文献属搜文献链路。

**动机**：用户手写的初稿常见问题——正文中有 `[3]` 或 `\cite{smith2020}` 引用，但 bibliography 段缺失对应条目。这不是 rewrite / add_section 的范畴，是**结构性修复**。

### 触发方式

- 用户明确说"帮我补 bibliography 缺的条目"
- 用户导入 draft 后，Layer 1 抽取阶段自动扫描：正文引用集合 ⊃ bibliography 集合 → 提示用户"你的正文引用了 X 条 refs 但 bibliography 只有 Y 条，要不要修复？"
- 任何意图内部若发现正文有引用锚点但 known_refs 缺条目 → 提示切到 `fix_bibliography`

### 操作步骤

a. [代码] 扫描 working copy 正文，抽出所有引用锚点集合（`[id]` 或 `\cite{bibkey}`）
b. [代码] 与 `facts.known_refs` 对比，找出缺失条目
c. [代码/交互] 对每条缺失：
   - 若在 v1 residual 里有元数据（title/authors）→ 直接调 `verify_refs` 补核验
   - 若只有引用锚点没有元数据 → 问用户"[3] 对应什么文献？给个 title 或 doi"
   - 若用户不知道 → 触发 `search_refs`（用锚点附近的正文关键词做 query）猜测
d. [代码] 用户确认候选 → 补入 `known_refs`
e. [代码] 重新渲染 bibliography 段并写回 working copy
f. [代码] git commit

### 与其他意图的关系

- **不是意图**，是**共享工具**——不需要 Router 判定，可被任意意图内部调用
- 但用户直接说"修 bibliography"时，Router 把它当 `fix_bibliography` 分派（作为一个特殊的入口，不进任何意图的完整 SOP）

---


## 卡 3 · `add_section`

**目标**：为论文新增一个当前不存在的章节（引言 / 方法 / Limitations / 任意用户命名的章节），包含"访谈收集章节意图 + 结构 + 可选搜文献 + 生成正文 + gate + 落盘"完整闭环。

**立场声明**：v2 不预设论文宏观结构（不做 IMRaD 模板、不做 minimal 模板）。**章节内部结构由 Interview 每次收集一次**，答案存 `facts.claims.section_structures[section_id]` 供后续复用。论文整体结构完全由用户导入的文件 + 主动 add_section 决定。

### 1. Router 判据

- **关键词**：
  - 中文：补写 / 新增 / 加一章 / 补 / 缺 / 补齐 / 写一章 / 写个引言 / 需要引言
  - 英文：add / write a new section / missing / need an introduction
- **上下文信号**：
  - 用户提到的章节名在现有 `list_sections()` 中**不存在**（若存在应走 rewrite）
  - 或用户明确说"新增" / "补齐"
- **冲突消解**：
  - 命中 add_section 但目标章节已存在 → 提示切换 `rewrite`
  - 命中"补齐所有缺失章节" / "帮我看看缺哪些" → 进入 **批处理模式**（见第 2 节）
  - 目标是"参考文献 / bibliography"章节 → 转 `fix_bibliography` 共享工具（不走 add_section）

### 2. SOP

**单章模式**（默认）：

a. [代码] 从用户话抽出目标章节名 + 可能的位置暗示
b. [代码] 若已存在同名章节 → 拒绝并建议 rewrite
c. [Interview] 见第 3 节（3 问 + 可能追问）
d. [代码] 判断本章节类型是否通常需要引用（引言/相关工作/讨论 → 是；纯方法/纯实验 → 否）
   - 若"是"且用户 Interview 中未明确说不用文献 → 追问"要不要搜相关文献支撑？"
   - 用户同意 → 调用**搜文献子 SOP**（共享设施 B）
e. [代码] 若用户在 Interview a2 选了 (c) 模仿式（指参考论文）→ 抽取参考论文对应章节的小节骨架
f. [代码] 拉取 `facts.claims`（section_intents / novelty / gap / contributions / baselines 等）作为 grounding
g. [LLM 单次] 输入 { 章节类型, 章节目的, 内部结构骨架, verified refs 列表, facts 摘要, 保真约束 prompt } → 生成完整章节正文
h. [代码] 计算 diff（对应"在插入位置新增一整段内容"）
i. [代码] 跑 Diff Gate（见第 5 节）
j. [代码] 未过 → 回灌违反规则 retry ≤ 2 次
k. [代码] 通过 → 写入 working copy + git commit
l. [代码] 更新 `facts.claims`：
   - `section_intents[section_id]` = Interview a1 答案
   - `section_structures[section_id]` = Interview a2 答案
   - 若本章 Interview 触发用户回答了 novelty / gap / contributions 等关键 claim → 一并入库
m. [代码] 展示 diff 摘要 + 新章节预览前 300 字 + 引用编号列表

**批处理模式**（用户说"补齐缺失章节"）：

a'. [代码] 扫描现有章节 = list_sections()
b'. [代码] 展示常见学术章节参考清单（Introduction / Related Work / Discussion / Conclusion 等），标出用户已有的、缺的
    诚实措辞："这些是常见章节做参考，不是必须。你想补哪些？（多选 / 说'都不用' / 补其他自定义章节）"
c'. [代码] 用户选定 N 个章节 → 逐个进入单章模式（不并行）
d'. [代码] 每个章节 SOP 走完 → 展示 diff → 询问"继续下一章 / 停在这里"
    - 用户说"继续" → 进下一章
    - 用户说"停" → 保留已完成的章节，任务结束
    - 用户说"上一章方向不对，改一下" → 触发 rewrite 对上一章（新意图，走 rewrite SOP）

### 3. Interview（3 问 + 可选追问）

**必问（SOP 硬编码）**：

- `a1`：**"这一章的类型 / 目的？"**（一句话）
  - 示例回答：
    - "引言，讲清楚为什么这个问题重要 + 我们的贡献"
    - "方法章节，介绍我们的 XX 算法"
    - "Limitations，讨论方法的适用边界"
  - 存 `facts.claims.section_intents[section_id]`

- `a2`：**"这一章内部怎么组织？"** 三种输入方式任选：
  - (a) **描述式**：一句话说骨架
    - 示例："背景 → gap → 我们做了 → 贡献列表"
  - (b) **列表式**：给 3-5 个小节标题
    - 示例："1. Setup 2. Main Theorem 3. Proof Sketch 4. Discussion"
  - (c) **模仿式**：指个已发表论文的对应章节做参考
    - 示例："按 [/path/to/neurips2023_xxx.pdf] 的 introduction 结构写"
    - 触发代码抽取参考论文该章节的小节骨架
  - **`facts.claims.section_structures[section_type]` 沿用提示**：若用户历史有过该类型章节的结构偏好 → Interview 提示"上次你的 introduction 用了 [结构 X]，本次沿用吗？(y/n/改一下)"

- `a3`：**"插入位置？"**
  - "全文开头 / 全文末尾 / 某章之前 / 某章之后"
  - Router 若从用户原话已推断出位置 → 此问变为确认（"我理解插在 X 之后，对吗？"）

**LLM 允许追问 ≤ 2 条**：

- 若 a1 答案模糊（如"就是个引言啊"）→ 追问："能不能一句话说清这一章想传达什么？比如'为什么这个问题重要 + 我们的贡献'"
- 若 a2 选了 (a) 描述式但描述过于抽象（如"就正常引言那样"）→ 追问："能不能列 3-4 个具体点？或者你有参考的论文吗？"
- 若章节类型是"引言 / 相关工作 / 讨论"且用户未提引用 → 追问："这一章通常需要引用相关文献。要不要现在搜？（可选：搜 X 篇 / 用已有的 / 不引用）"
- 若 `facts.claims` 里已有 novelty / gap 但当前章节需要 → **不追问**，直接从 claims 取（第 N 次 chat 越用越省心）

**含糊处理**：

- 追问一次后 a1 / a2 仍含糊 → **任务失败**（add_section 是重操作，不宽松）
- 诚实告诉用户："我需要更明确的章节目的和结构才能写好，否则容易写成 generic 骨架。要不要：(1) 想清楚再重来 (2) 参考一篇已发表论文的对应章节"

### 4. 工具白名单

| 工具 | 用途 |
|---|---|
| `list_sections()` | 返回章节 id + 标题（含位置） |
| `check_section_exists(name)` | Router 判定用；返回是否已存在 |
| `extract_structure_from(path, section_name)` | 从参考论文抽小节骨架（Interview a2 (c) 时用） |
| `search_refs(query, limit)` | 共享子 SOP 内部使用 |
| `verify_refs(candidates)` | 共享子 SOP 内部使用 |
| `add_section(title, position, direction, structure, cited_refs)` | 单次 LLM 生成 + gate + 落盘 |
| `confirm_new_number(value, context)` | Gate 触发；用户确认新数字 |
| `show_diff()` | 展示新增章节的 diff |

★ 不暴露：`rewrite_section` / `polish_section` / `convert_document` / `format_*`。

### 5. Diff Gate（代码硬校验）

**引用格式说明**：与卡 2 一致——`.tex` 用 `\cite{bibkey}`，`.docx`/`.md` 用 `[id]`；gate 只校验"引用能解析到 `known_refs`"。

**通过条件（全部满足）**：

- ✓ 只新增了目标章节，其他章节逐字不变
- ✓ 新章节的插入位置符合 Interview a3 指定
- ✓ 章节标题与 Interview 里的章节名一致
- ✓ 新章节的小节结构与 Interview a2 提供的骨架一致（若 a2 是列表式或模仿式，可硬校验小节标题；描述式则用宽松的 semantic 匹配或跳过）
- ✓ 所有引用都能解析到 `known_refs`（不新增未核验）
- ✓ 无占位符（TODO / 待补充 / xxx / 具体请补充 / [待填]）
- ✓ **新数字必须满足**：在 `facts.known_numbers` 或经 `confirm_new_number` 用户确认
- ✓ 章节长度合理（500-3000 字之间；引言/结论偏短，方法/相关工作可长）

**Retry 策略**：

- 失败 → 回灌违反规则条目 + 用户 Interview 答案 + LLM 上次输出 + 具体违规位置
- 最多 retry 2 次

**最终失败处理**：

- 诚实告诉用户具体违反了哪几条
- 分类建议：
  - "引用了未核验文献" → 提示再跑一次搜文献子 SOP 或换叙述避开该 claim
  - "编造了数字" → 提示用户在 `confirm_new_number` 里确认，或从 `facts.claims` 里补充实验设置
  - "小节骨架与 Interview 答案不符" → 通常是 LLM 理解偏了，用更结构化的输入（列表式或模仿式）重来
  - "章节太空洞（多处占位符）" → 说明 Interview 答案信息不足，建议用户补充 novelty / gap 等 claim 后重跑

### 6. 意图升级 / 降级路径

- 用户指定的章节已存在 → 拒绝并推荐 `rewrite`
- 目标是 bibliography → 转 `fix_bibliography`
- 用户 Interview 里其实说的是"改现有章节" → 建议切 `rewrite`
- 用户批处理中间说"算了不补了" → 保留已落盘章节，任务结束
- 用户批处理中间说"上一章方向不对" → 提示切 `rewrite` 修上一章，本意图暂停或结束
- 生成后用户说"我想细化第 3 小节" → 建议切 `rewrite` 对本章做局部改动

---

## 卡 4 · `convert_format`

**目标**：把用户论文从一种格式转成另一种（`.tex ↔ .docx ↔ .md`），保留公式、章节结构、表格、引用；可选双栏 / 三线表等常见排版预设。

**立场声明**：本意图**不修改论文内容**——只做格式变换。产出是**新文件**，不是 working copy 的 diff。转换本质是"确定性工具（pandoc + python-docx / LaTeX 后处理）"，LLM 只在**没有明确目标格式**时用于意图澄清，不参与内容生成。

### 1. Router 判据

- **关键词**：
  - 中文：转 / 转成 / 变成 / 换成 / 转格式
  - 英文：convert / to docx / to latex / to word / as tex
  - 目标格式关键词：`docx` / `word` / `latex` / `.tex` / `markdown` / `.md`
  - 排版预设关键词：`双栏` / `two-column` / `三线表` / `IEEE 格式` / `ACM 模板`
- **上下文信号**：
  - 用户提到源文件（或 working copy 已存在）+ 目标格式
  - 源格式与目标格式不同（相同则拒绝并提示"用 `format_docx` 或 `inplace_polish`"）
- **冲突消解**：
  - 命中 convert_format 但源=目标 → 拒绝，建议改走 `format_docx`
  - 命中 convert_format 但用户想改内容（"改一下再转"）→ 建议先跑 rewrite/polish 落盘 working，再走 convert
  - 与 `format_docx` 撞（"转成 docx 并把字体改成宋体"）→ 先跑 convert，再跑 format_docx，分两步告诉用户

### 2. SOP

a. [代码] 从用户话抽出 { source_ext（若未指定则用 working ext）, target_ext, 排版预设列表 }
b. [代码] 若 target_ext 未指明 → 追问用户（选项：docx / latex / markdown）
c. [代码] 检查 pandoc 可用性 → 不可用则**任务失败**并诚实告知（不做 LLM 降级"翻译"，那会破坏公式）
d. [代码] 检查目标格式与源的兼容性：
   - `.tex → .docx`：pandoc 直转，公式转 OMML；bibliography 若为 `.bib` 单独处理
   - `.docx → .tex`：pandoc 直转，OMML 转 `$...$`；bibliography 若有 refs → 生成 `.bib` 或内联 `\bibitem`
   - `.md → *`：pandoc 直转
   - 其他跨格式：走 pandoc 默认策略
e. [代码] 执行 pandoc 转换 → 得到目标文件
f. [代码] 应用排版预设（若用户指定）：
   - 双栏（docx）→ python-docx 设 section columns
   - 三线表 → 遍历表格套用样式
   - IEEE / ACM 模板 → 依 venue skill（v3 才做，v2 阶段仅支持 raw pandoc + 双栏 + 三线表）
g. [代码] 转换后校验（见第 5 节）
h. [代码] 通过 → 写入 `output/<stem>.<target_ext>`（新文件，不覆盖 working copy）
i. [代码] 展示："已生成 X.docx。原 working copy 未变。要不要把 working 切换成新文件？"
   - 用户"是" → 新文件成为下轮 working copy（session 切换绑定文件）
   - 用户"否" → 保留新文件，working 保持不变（用户可随时手动 open）

### 3. Interview（0-1 问）

**必问**：无（意图信号强时零 Interview）

**LLM 允许追问 ≤ 1 条**（仅在信号不足时）：

- 若 target_ext 未指明 → 追问："转成什么格式？" 选项：docx / latex / markdown
- 若用户提到"排版" / "样式"但未具体化 → 追问："要不要加常见预设？(a) 双栏 (b) 三线表 (c) 都要 (d) 都不要"
- 若 working copy 有未落盘的 rewrite/polish 改动 → 追问："当前 working 有未 save 的改动，用改动版转还是用源文件转？"

**含糊处理**：追问一次后仍不明确 → 使用最保守默认（不加任何排版预设，只做纯格式转换），不硬拒绝。

### 4. 工具白名单

| 工具 | 用途 |
|---|---|
| `check_pandoc()` | 探测 pandoc 可用性 |
| `convert_document(source, target_ext, presets)` | 主要工具：pandoc 直转 + 排版预设应用 |
| `list_typography_presets()` | 返回 v2 支持的预设列表 |
| `switch_working_to(new_file)` | 用户同意后把新文件设为 working copy |

★ 不暴露：任何内容改动工具（`polish_section` / `rewrite_section` / `add_section`）。

### 5. 转换后校验（代码硬校验，非 diff gate）

**通过条件**：

- ✓ pandoc 退出码 = 0
- ✓ 章节数量：转后 = 转前（用统一的章节抽取器数）
- ✓ 引用锚点数：转后 = 转前（`\cite{X}` 数 = `[id]` 数）
- ✓ 表格数、图数：转后 = 转前
- ✓ 公式数：转后 = 转前（`$...$` / OMML `<m:oMath>` 数量匹配）
- ✓ 输出文件可打开（`.docx` 用 python-docx 试开；`.tex` 用简单语法扫描）

**Retry 策略**：无（转换是确定性的，失败即失败，不 retry）。

**失败处理**：

- 章节/引用/公式数不匹配 → 诚实告知"pandoc 转换有信息损失，可能是 X 未被识别"，附上 pandoc stderr
- 缺 pandoc → 提示用户装 pandoc 或走替代路径（若 `.tex ↔ .docx` 且用户装了 Word，可提示手动"打开另存为"）
- 文件损坏 → 保留原 working copy 不变，删除损坏产物

### 6. 意图升级 / 降级路径

- 源 = 目标 → 拒绝并建议 `format_docx` 或 `inplace_polish`
- 用户想"顺便改内容" → 拒绝合并，建议先 rewrite/polish 再 convert
- 转换后用户对样式不满意 → 建议切 `format_docx` 微调
- 转换失败且用户明确要 LLM"翻译"式转换 → 拒绝（那会破坏公式与引用，学术论文场景不做）

---

## 卡 5 · `format_docx`

**目标**：对 `.docx`（或 `.tex`）文件做排版格式操作——字体、字号、行距、缩进、对齐、样式、分栏、表格样式、页眉页脚、页码等。用户说什么改什么，能确定性做的就硬编码工具做；做不了的**如实告知边界**，或经用户明确同意后走 `run_python` 逃生舱。

**立场声明**：本意图**不修改正文内容**（一个字都不动）。所有操作只改**格式属性**（OOXML 里的 rPr / pPr / sectPr / tblPr 等；tex 里的 preamble / 命令参数）。Diff Gate 的核心是**正文 hash 前后一致**——只要正文变了就一定拒绝。

### 1. Router 判据

- **关键词**：
  - 中文：字体 / 字号 / 几号字 / 行距 / 首行缩进 / 对齐 / 居中 / 左对齐 / 一级标题 / 二级标题 / 样式 / 双栏 / 单栏 / 三线表 / 页码 / 页眉 / 页脚 / 加粗 / 斜体
  - 英文：font / size / spacing / indent / align / heading / style / column / bold / italic / page number
- **上下文信号**：
  - 用户提到的是"外观 / 排版属性"，不是"内容 / 论证 / 引用"
  - Working copy 是 `.docx` / `.tex`（`.md` 一般不谈排版，转到 docx 后再谈）
- **冲突消解**：
  - 命中 format_docx 但用户话里同时含"改一下这段的表述" → 拒绝合并，先 polish/rewrite 再 format
  - 命中 format_docx 但目标是"从 tex 变 docx" → 走 `convert_format`（顺带排版预设）
  - 命中 format_docx 但 working 是 `.md` → 提示"markdown 没有排版属性，要不要先 convert 到 docx"

### 2. SOP

a. [代码] 从用户话抽出 { 目标属性, 属性值, 作用范围 }（可能多组，一次可批量下达）
b. [代码] 每组识别 → 分派到 Layer 1 / Layer 2 / Layer 3：
   - **Layer 1**（结构化工具直做）：所有工具在预置目录（见第 4 节）内 → 直接执行
   - **Layer 2**（组合宏）：识别为预置组合 → 展开为多个 Layer 1 调用
   - **Layer 3**（逃生舱）：Layer 1/2 都覆盖不了 → 见 c 步
c. [Layer 3 逃生舱交互]：
   - 明确告知用户"这个需求超出我的确定性工具范围，我可以尝试用 `run_python + python-docx` 脚本实现，但不保证正确"
   - 用户选：(a) 试一下 (b) 放弃这条 (c) 换个近似操作
   - 用户选 (a) → 生成 python 脚本（LLM 单次）→ **副本沙箱执行**（不动原 working）→ 展示结果 → 用户 y/n 是否采纳
   - 用户选 (b) → 跳过此条，继续处理其他条
d. [Interview] 见第 3 节（多数场景零 Interview；仅"作用范围模糊"时追问）
e. [代码] 备份 working（git commit 当前状态作 undo 点）
f. [代码] 按顺序执行所有工具调用（每一步是原子 python-docx / tex 操作）
g. [代码] 每步后跑 Diff Gate（第 5 节）；任一步违反 → 立即回滚 + 报错，不继续
h. [代码] 全部通过 → git commit 落盘
i. [代码] 展示"已执行 N 条格式操作，均通过结构无损校验"，附操作清单

### 3. Interview（0-1 问，通常零问）

**必问**：无

**LLM 允许追问 ≤ 1 条**（仅当作用范围模糊）：

- 用户说"字体改成宋体" 但没说范围 → 追问："作用范围？(a) 全文 (b) 正文（不含标题）(c) 某某章节 (d) 某段"
- 用户说"标题居中" 但没说哪级 → 追问："哪级标题？(a) 一级 (b) 二级 (c) 全部标题"
- 用户说"改成 IEEE 格式" → 追问："IEEE 有多个模板，你的目标会议是？（v2 只支持 raw + 双栏 + 三线表，真正的 IEEE 模板 v3 才做）"

**含糊处理**：追问一次后仍模糊 → 用最保守默认（如"作用范围"默认全文正文），不硬拒绝。

### 4. 工具白名单（Layer 1 目录级归纳）

**字符级（run / span）属性**：
- `set_font_family(scope, family)`  # 宋体 / Times New Roman / Arial ...
- `set_font_size(scope, size)`      # "四号" / 14 / "小四"
- `set_bold(scope, on)` / `set_italic(scope, on)` / `set_underline(scope, on)`
- `set_font_color(scope, color)`

**段落级（paragraph / pPr）属性**：
- `set_alignment(scope, align)`     # left / center / right / justify
- `set_line_spacing(scope, value)`  # 1.0 / 1.5 / 2.0 / "single" / "double"
- `set_first_line_indent(scope, value)`
- `set_paragraph_spacing(scope, before, after)`
- `apply_paragraph_style(paragraph_ref, style_name)`  # "Heading 1" / "Normal" ...

**结构级（section / secPr）属性**：
- `set_columns(scope, n)`           # 1 / 2 / 3
- `set_page_size(scope, size)`      # A4 / Letter
- `set_margins(scope, top, bottom, left, right)`
- `set_page_orientation(scope, orient)`  # portrait / landscape

**表格 / 图**：
- `apply_table_style(table_ref, style_name)`  # "三线表" / "Grid" ...
- `set_table_column_widths(table_ref, widths)`
- `set_figure_alignment(figure_ref, align)`

**页眉 / 页脚 / 页码**：
- `set_header(scope, text)` / `set_footer(scope, text)`
- `insert_page_number(position, format)`

**Layer 2 组合宏**：
- `apply_typography_preset(name)`  # "two_column_paper" / "single_column_thesis"
- `format_references_style(style)`  # "IEEE" / "APA" / "GB/T 7714"（v2 仅骨架，逐步补）

**Layer 3 逃生舱**：
- `run_python_script(script_text)`  # 沙箱执行，只能操作 working 的副本；用户明确同意才触发

★ 不暴露：任何**内容改动工具**（`rewrite_section` / `add_section` / `polish_section`）——本意图严格只碰格式。

### 5. Diff Gate（结构无损校验）

**核心规则：正文文本 hash 前后必须完全一致。**

**通过条件（全部满足）**：

- ✓ 正文文本 hash 前后一致：
  - 从 working 提取所有可见字符（去掉格式标记）→ SHA-256
  - 操作前后 hash 相同（**这是硬红线**）
- ✓ 章节数、段落数、表格数、图数不变
- ✓ 所有引用锚点（`\cite{}` / `[id]`）逐字保留
- ✓ 公式（OMML / `$...$`）逐字保留
- ✓ 只有被用户授权的属性字段发生了变化（这是"审计跟踪"——每个工具调用声明"我要改哪些字段"，gate 验证实际改动子集 = 声明字段）
- ✓ 文件结构完整（`.docx` 用 python-docx 试开；`.tex` 用简单语法扫描）

**Retry 策略**：无。格式操作是确定性的，一步失败就是失败，不 retry。

**失败处理**：

- 正文 hash 变了 → **立即回滚到本次意图开始前的 git commit**，报错"这个工具意外改到了正文内容，已回滚全部改动"
- 属性字段越界（改到了声明外的字段）→ 同上回滚
- 文件损坏 → 同上回滚
- Layer 3 脚本失败 → 只回滚该脚本的影响，其他 Layer 1/2 操作保留

### 6. 意图升级 / 降级路径

- 用户想改内容 → 拒绝合并，建议 polish/rewrite/add_section
- 用户想跨格式转换 → 建议 `convert_format`（可含双栏预设）
- Layer 3 脚本失败且用户仍想做 → 诚实告知"这个需求超出 v2 能力，可能需要手动操作"
- Working 是 `.tex` 但用户诉求本质是 docx 排版（如"三线表"在 tex 里语义不同）→ 提示"先转 docx 再改"

---

## Step 3 · 模块清单（从卡片反推）

### 层次结构

```
Layer A · 基础设施
──────────────────────────────────────────────────
A1. FileBackedSession      Session 生命周期 + working copy + git
A2. SectionIndex           解析 working 生成章节锚点索引（cache）
A3. SessionFacts           facts.json 读写（known_refs / claims / etc.）
A4. AutoExtractor          从源文件抽 Layer 1 facts（refs/numbers/terms）
A5. Providers              LLM / retrieval / verifier（保留 v1 大部分）

Layer B · Agent 骨架
──────────────────────────────────────────────────
B1. Router                 意图分派（规则 + 极少 LLM 兜底）
B2. Interview              SOP 状态机 + 追问 + 含糊处理
B3. Generator              单次 LLM 调用，按意图构造 messages
B4. DiffGate               按意图分派的规则集（5 个 gate 实现）
B5. ContextBuilder         每意图的 messages 构造函数（纯函数）
B6. IntentRegistry         每意图暴露哪些工具（工具白名单硬编码）

Layer C · 工具集（按意图归类）
──────────────────────────────────────────────────
C1. 只读工具               list_sections / read_section / show_diff / ...
C2. 内容改动工具           polish_section / rewrite_section / add_section
C3. 引用管理工具           search_refs / verify_refs / confirm_new_number
C4. 格式转换工具           check_pandoc / convert_document
C5. 格式排版工具           Layer 1 硬编码 20+ 个（set_font_size 等）
C6. 逃生舱                 run_python_script（沙箱）
C7. 修复工具               ~~fix_bibliography~~（已删，B-2）

Layer D · CLI / Entry
──────────────────────────────────────────────────
D1. Chat REPL              scripts/chat.py（简化版）
D2. Runtime Config         env → Config（简化到 <100 行）
```

### 依赖关系

```
Chat REPL (D1)
  └─→ Router (B1)
       └─→ IntentRegistry (B6)
            └─→ (为每个意图)
                 Interview (B2) → ContextBuilder (B5) → Generator (B3)
                                                          → DiffGate (B4)
                                                          → Tools (C1-C7)

所有层共同依赖 Layer A：FileBackedSession + SectionIndex + SessionFacts
Providers (A5) 被 Generator / Verifier / Search 依赖
```

### v2 模块 vs v1 代码对应

| v2 模块 | 复用 v1 | 新写 |
|---|---|---|
| A1. FileBackedSession | 少量 workspace/store 思路 | 大部分新写 |
| A2. SectionIndex | 少量 ingestion 章节切分 | 新写 |
| A3. SessionFacts | 无 | 全新 |
| A4. AutoExtractor | ingestion / parsing 部分函数 | 新写 |
| A5. Providers | **保留** `providers/`, `observability/`, `tools/citation`, `tools/literature_tool` | 少量精简 |
| B1. Router | 少量 `agent_platform/routing.py` 思路 | 大部分新写（多规则、少 LLM） |
| B2. Interview | `AskUserTool`, `clarification.py` 部分 | 主体新写（状态机） |
| B3. Generator | 无（v1 分散在 WritingAgent + TaskAgent） | 全新，纯函数 |
| B4. DiffGate | 少量 `GuardrailGate` 思路 | 全部新写 |
| B5. ContextBuilder | 无 | 全新 |
| B6. IntentRegistry | `ToolRegistry` | 扩展"意图分片" |
| C1-C7 | v1 工具部分复用 | 大部分薄适配到新 harness |
| D1. Chat REPL | `chat.py` 骨架 | 精简 |
| D2. Runtime Config | `config.py` 少量 | 大幅精简 |

### 行数估算

| 模块 | 行数 |
|---|---|
| A1. FileBackedSession | ~250 |
| A2. SectionIndex | ~200 |
| A3. SessionFacts | ~100 |
| A4. AutoExtractor | ~150 |
| A5. Providers（复用 + 精简） | ~3500 |
| B1. Router | ~200 |
| B2. Interview | ~400 |
| B3. Generator | ~300 |
| B4. DiffGate（5 个实现 + 通用） | ~600 |
| B5. ContextBuilder（6 个函数） | ~300 |
| B6. IntentRegistry | ~100 |
| C1-C7 工具（20+ 个） | ~1500 |
| D1. Chat REPL | ~200 |
| D2. Runtime Config | ~100 |
| Ingestion / Export（保留） | ~3800 |
| **合计** | **~11700** |

对比 v1 ~33000 行 → **-64%**。

---

## Step 4 · Milestone 表

### 设计原则

1. **垂直穿越**：每周做一个完整端到端穿越，不做水平堆栈（Layer 全建再接工具）
2. **由简到繁**：意图按复杂度排序（polish → convert → rewrite → add_section → format_docx）
3. **可见验收**：每周验收 = 用户可见行为，不接受"单测覆盖率"这类内部指标
4. **v1 冷冻**：v1 代码不动一直可跑，v2 独立目录（`src/paper_agent_v2/`）；Week 6 切换主入口 + 删 v1

### Week 1 · Polish 端到端穿越

**目标**：能 chat 一句"润色某章"，走完 Router → Interview → LLM 生成 → DiffGate → 落盘 → show diff 全流程。

**要建的模块**（最小版）：
- A1 FileBackedSession（最小版：绑一个文件 + copy 到 working，暂时无 git，用简单备份）
- A2 SectionIndex（最小版：只支持 `.tex` 的 `\section{...}` 抽取）
- A3 SessionFacts（最小版：只有 `known_refs` 一个字段，从 tex 的 `\bibitem` 抽取）
- A4 AutoExtractor（极简：只抽 refs）
- B1 Router（只识别 polish 意图 + 其他归 unclear）
- B2 Interview（只支持 polish 的 1 问）
- B3 Generator（只有 polish 一种 ContextBuilder）
- B4 DiffGate（只实现 polish 的 gate 规则）
- B5 ContextBuilder（1 个函数）
- B6 IntentRegistry（硬编码 polish 的工具白名单）
- C1 只读工具（list_sections / read_section / show_diff）
- C2 polish_section 工具
- D1 Chat REPL（极简版，先支持一轮对话，不做流式）
- D2 Runtime Config（极简版）

**验收**：
```
$ python scripts/chat_v2.py my_paper.tex
> 润色 introduction 章节

（agent 追问："改到什么程度？"）
（agent 展示 diff，只改语言、数字/引用没动）
```

**行数目标**：~1500 行新增
**风险**：跑不通 → stop and rethink

---

### Week 2 · Convert Format + FileSession 完善

**目标**：加第二个意图（convert）；补全 FileBackedSession（git + `.docx` / `.md` 支持）。

**新增模块**：
- A1 FileBackedSession 补 git（commit / revert）
- A2 SectionIndex 支持 `.docx`（python-docx）+ `.md`
- B1 Router 扩展识别 convert_format
- B5 ContextBuilder 加 convert 分支（零 LLM 生成）
- B4 DiffGate 加 convert 转换后校验（章节/引用/公式数一致）
- C4 格式转换工具（check_pandoc + convert_document + list_typography_presets）

**验收**：
```
> 把这份 tex 转成 docx，加双栏
（agent 直接跑 pandoc + python-docx 双栏预设）
（agent 展示："已生成 my_paper.docx，公式/章节/引用数量校验通过"）
```

**行数目标**：~1000 行新增
**为什么放第 2**：零 LLM 生成、零 Interview 分支，独立验证 FileBackedSession + Router 分派

---

### Week 3 · Rewrite + 搜文献子 SOP

**目标**：加第三个意图，第一次跑通"搜文献 + 核验 + 引用"共享子 SOP。

**新增模块**：
- A3 SessionFacts 补 `known_numbers` / `claims` 字段
- A4 AutoExtractor 补数字抽取（从表格）+ 术语抽取
- B1 Router 扩展 rewrite
- B2 Interview 加 rewrite 多问 + 追问
- B4 DiffGate 加 rewrite gate（含"新数字必须 confirm"）
- B5 ContextBuilder 加 rewrite 分支
- C2 rewrite_section 工具
- C3 引用管理工具（search_refs / verify_refs / confirm_new_number）
- 共享设施 B（搜文献子 SOP）首次实现

**验收**：
```
> 补一下方法章节的分析论证，加 2 篇最近的 GNN 综述引用
（agent 追问方向；触发搜文献子 SOP，找到 2 篇 verified）
（gate 通过：所有引用都在 known_refs 里）
```

**行数目标**：~1500 行新增
**难点**：搜文献子 SOP 的错误处理路径（限流 / 0 结果 / 用户中途叫停）

---

### Week 4 · Add Section + Facts 系统完整化

**目标**：加第四个意图，第一次跨 chat 复用 `facts.claims`。

**新增模块**：
- B1 Router 扩展 add_section（单章 + 批处理）
- B2 Interview 扩展 add_section 3 问 + 三种输入方式
- B4 DiffGate 加 add_section gate
- B5 ContextBuilder 加 add_section 分支
- C2 add_section 工具
- extract_structure_from（从参考论文抽小节骨架）
- Facts 跨意图复用逻辑（`section_intents` / `section_structures` 沿用提示）
- 共享设施 C（fix_bibliography 工具）—— 后已废弃，见 B-2

**验收**：
```
第一次 chat: > 帮我补写引言
（agent 问：目的/结构/位置；生成落盘；存 claims）

第二次 chat: > 再补一个 conclusion
（agent 直接从 facts 取 novelty，不再问；生成落盘）
```

**行数目标**：~1500 行新增
**难点**：批处理中断/继续/回退；Interview 沿用提示语要自然

---

### Week 5 · Format Docx（Layer 1 + 逃生舱）

**目标**：加第五个意图，最复杂的一周。

**新增模块**：
- B1 Router 扩展 format_docx
- B2 Interview 加可选追问
- B4 DiffGate 加"正文 hash 前后一致"核心规则 + 授权字段审计
- B5 ContextBuilder 加 format_docx 分支（LLM 只映射工具调用）
- C5 格式排版工具（20+ Layer 1 硬编码工具）
- C6 逃生舱（`run_python_script` 沙箱 + 用户确认）
- Layer 2 组合宏骨架（`apply_typography_preset`）

**验收**：
```
> 全文改成宋体四号，1.5 倍行距，一级标题居中，某表加三线格式
（agent 识别 4 条操作，分派 Layer 1，逐条执行）
（正文 hash 前后一致，通过）
```

**行数目标**：~1500 行新增
**难点**：`.docx` 正文提取的边界（headers/footers/表格 cell/脚注/批注）

---

### Week 6 · 切换主入口 + 删 v1 + 收尾

**目标**：v2 成为默认入口；删掉 v1；产出 changelog。

**动作**：
- 切换 `scripts/chat.py` 主入口 → v2
- 删掉 v1：`orchestrator.py` + `app.py` + 8 个 pipeline Agent + `agents/writing_agent.py` + `research_artifact.py` + `artifact_loader.py` + `agent_platform/` 大部分
- 更新 README
- 一致性检查 & 术语归一

**验收**：
```
- 仓库 python 行数 ≤ 12000
- 所有 5 个意图端到端 demo 全过
- scripts/chat.py 默认走 v2
- 新用户凭 README 5 分钟内跑通一个意图
```

**行数目标**：净删 ~20000 行

---

### 总览

| Week | 主题 | 新增 | 累计 v2 行数 | 里程碑 |
|---|---|---|---|---|
| 1 | Polish 端到端穿越 | ~1500 | 1500 | 骨架验证 |
| 2 | Convert + FileSession | ~1000 | 2500 | Router 分派可靠 |
| 3 | Rewrite + 搜文献 | ~1500 | 4000 | 子 SOP 跑通 |
| 4 | Add Section + Facts | ~1500 | 5500 | 跨 chat 记忆 |
| 5 | Format Docx | ~1500 | 7000 | 硬编码工具集 |
| 6 | 切换 + 删 v1 | 净删 20000+ | ~12000 | 上线 |

**总投入**：6 周 × 15-20h/周 ≈ 90-120 小时。

---

## 待办

- [x] 卡 1-5 · 5 张意图卡
- [x] 共享设施 A · Facts 系统
- [x] 共享设施 B · 搜文献子 SOP
- [x] 共享设施 C · fix_bibliography（后废弃，B-2）
- [x] Step 3 · 模块清单
- [x] Step 4 · Milestone 表

**规格完成 —— 可以进入实现阶段。**
