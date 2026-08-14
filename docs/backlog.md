# Backlog — 待办与决策留痕

> 本文档记录**已确认的问题**与**已作出的决策**，供后续追溯。
> 每条带证据位置，便于验证结论是否仍然成立。
> 与 `.kiro/specs/*` 的区别：specs 是单个 feature 的 requirements/design/tasks；
> 本文档是跨 feature 的问题池与决策记录。

- 建立时间：2026-08-01
- 建立背景：一次完整代码评审 + active 模式装配冒烟
- 当时状态：228 个 py 文件 / ~3.7 万行 / 1057 测试全绿（170s）/ 十个 feature flag 默认全关

---

## 一、阻塞项（翻 `intent_planning_mode=active` 之前必须处理）

### B-1 · active 模式下两个意图不可达

**证据**：`scripts/_wiring_check.py` 实测输出

```
Router 全集:      add_section, add_section_batch, convert_format,
                  fix_bibliography, format_docx, polish, rewrite
analyzer 支持:    add_section, convert_format, format_docx, polish, rewrite
active 下不可达:  add_section_batch, fix_bibliography
```

**链条**：`operation_analyzer._SUPPORTED` 只含 5 个意图；`_HANDLERS` 同样 5 个；
而 `chat.py:_handle_planned_turn` 在 active 下无条件 `return True`，
导致 `chat.py` 里 `if route.intent is Intent.FIX_BIBLIOGRAPHY:` 等 legacy 分支永不可达。

**状态**：`fix_bibliography` 已删除（B-2 已完成）。剩 `add_section_batch` 一个不可达，
方案见 B-3（脱糖，不是修接线）。所以 B-1 现在只剩一半。

---

### B-2 · `fix_bibliography` 应从意图降级为写入路径的内部不变量 ✅ 已完成

**结论**：这个意图要**删除**，能力保留但换位置。

> **完成记录（2026-08-06）**
>
> 实际删除时发现依据 2「产出是残废的」和「正确形态」表格第一行**已经被另一套实现
> 解决了**：`literature/bibliography_sync.py` 的 `apply_bibliography` 早已在
> `_commit_add_section` / `_commit_rewrite` 里跑（`intents/add_section.py:194`），
> 按正文实际引用重建整个文献区、走 `citation_styles` 渲染完整著录、且与正文**同一次**
> `write_working`（`:undo` 原子性 —— 比本条原方案设想的更完整）。
>
> 所以本条真正的结论不是「能力要搬到 commit」，而是**能力早就在 commit 了，这个意图
> 是重复且更差的实现**：只支持 tex、只写题名、单独一次写入会让 `:undo` 留半截状态
> （正文引用还在而文献区已撤销）。
>
> 下面「待处理的设计细节」那个 `splice` 契约冲突同样已由「同一次写入」解决，不需要
> 再扩 commit 语义。
>
> 落地内容：
> - 删 `Intent.FIX_BIBLIOGRAPHY`、`router._BIB_KW`、`tools/bibliography.py`、
>   `chat.py` 的分支、`tasking/rule_analysis.py` 的映射
> - 新增 `literature/dangling_refs.py`（只读诊断）+ `chat.py:_note_dangling_refs`，
>   接在 `_note_crossref_import` **之后** —— docx 的交叉引用域要先摊平，否则投影层
>   看不见域内文字，那些引用会被误判成「正文没引用」
> - `ask_context._OP_LABELS` 里的 `fix_bibliography` **保留**：老项目 git 历史里仍有
>   以它为 message 的 commit，删了那些历史条目会显示成原始英文
>
> 实测副作用：「参考文献」不再是路由关键词后，原被 `_BIB_KW` 劫持的祈使句现在落
> `UNCLEAR`，**不是依据 3 预期的 `rewrite`** —— 「多引几篇」这类措辞不在任何关键词
> 表里。`UNCLEAR` 只打印功能列表、不改文档，是安全的一侧；要让它落 rewrite 需单独
> 扩 `_REWRITE_KW`，已记为 B-8。

**三条依据**：

1. **触发时机错了。** "正文引用了但参考文献缺条目"对用户是 bug，不是任务。
   让用户发现 bug 再主动请求修复 = 把系统责任推给用户。
   （`:fixbib` 命令方案同样不成立，只是给意图改了个名字。）

2. **产出是残废的。** `tools/bibliography.py`：
   ```python
   new_items.append(f"\\bibitem{{{key}}} {title}.")
   ```
   只写题名。而 `workspace/models.py:ReferenceEntry` 手里有
   `authors` / `year` / `source_id`(DOI/arXiv) 全都没用。
   后果：修复后 LaTeX 编译警告消失、参考文献列表仍不合格 —— **显性问题变隐性问题**。

3. **它在 off 模式下正在抢流量。** `intents/router.py:route()` 第一个检查、无条件返回：
   ```python
   if any(kw in low for kw in _BIB_KW):   # ["bibliography","参考文献","文献列表","缺条目","bibitem"]
       return RouteResult(intent=Intent.FIX_BIBLIOGRAPHY)
   ```
   "帮我在相关工作里多引几篇**参考文献**" → 被劫为 fix_bibliography，本应是 rewrite。

**正确形态**（责任在系统）：

| 场景 | 位置 | 用户参与 |
|---|---|---|
| 系统写入的引用 | `_commit_add_section` / rewrite commit 时同步写 `\bibitem` | 零 |
| 用户原稿的悬空引用 | 载入时诊断报告一次 | 只需决定要不要补 |

**可行性依据**：`gates/diff_gate.py` 的 `AddSectionGate` / `RewriteGate` 已强制
```python
unknown = _cite_tokens(candidate, self.ext) - known
```
所以系统写入的每个 cite key 必然在 `facts.known_refs` 里，**元数据齐全**，
commit 时写条目是确定性查表操作，无需 LLM、无需询问。
推论：`fix_bibliography` 的"无法自动修复"分支在正常写入路径下**不可达**。

**待处理的设计细节**：往 `thebibliography` 插条目是**章节之外的改动**，
会打破 `splice` 的"其他章节逐字不变"字面契约。需明确扩展 commit 语义为
"正文 + 引用条目原子提交"，并让它出现在 diff 里，不能静默修改。

**动作**：
- [x] 删 `Intent.FIX_BIBLIOGRAPHY` 与 `router._BIB_KW` 分支（顺带解决抢流量）
- [x] 载入时悬空引用诊断（只读，零风险，可先做）
      → `literature/dangling_refs.py`，tex / md / docx 三种都做。**保守规则**：文献区
      识别不出来时保持沉默，不把所有引用判成悬空 —— 误报的代价比漏报高（用户会学会
      忽略这个提示）。docx 额外标 `low_confidence`（Word 编号列表/域形态，投影不一定
      认得出条目）。诊断认**用户的原生形态**（`thebibliography` / `## 参考文献`），
      不能复用 `_citation_tokens_in_body` 那套系统自己的界定标记 —— 用户原稿没有那些
      标记，整个文献区会被当成正文，条目自己被抽成引用，悬空判定恒为空。
- [x] ~~commit 时同步写 `\bibitem`，补全 authors/year/DOI~~
      → **本就已完成**，由 `apply_bibliography` 承担（见上方完成记录）。

**遗留（不属于本条）**：真实账本（2 个项目共 51 条，去重后 48 条）实测
`title`/`authors`/`year`/DOI 覆盖率 **100%**，但 `container_title` 45.8% /
`volume` 43.8% / `pages` 50.0% —— 约一半条目凑不出完整期刊著录，
`render_bibliography` 走 `report.incomplete`（「已简化著录」）。

**成因不是抽取器缺字段，是存量数据。** 按 `reference_type` 分组一看就分层：

```
reference_type 为空   n=22   container_title=0    volume=0   pages=0
article              n=20   container_title=20   volume=20  pages=19
conference-paper     n= 6   container_title=2    volume=1   pages=5
```

`providers/retrieval/openalex.py:225-252` 已经在取 `primary_location.source.display_name`
（刊名）、`biblio.volume/issue/first_page`，注释里写明了「原先未取 ⇒ 覆盖率恒为 0」。
所以取字段的代码是对的：**新抽取的 `article` 条目覆盖率 100%（20/20）**，
而那 22 条连 `reference_type` 都为空 —— 是补字段**之前**入库的旧记录，
集中在 `proj_1780f6e14f27`（36 条里 25 条无 type，container 仅 8）；
`proj_75eb4322afff`（15 条，全部有 type，container 14）则基本齐全。

因此真正的动作有两个，且**不是**原先写的「从 DOI 反查刊物元数据」：

1. **存量回填**：这 22 条 `work_identifiers` 里 DOI 齐全（100%），拿 DOI 重新打一次
   OpenAlex/Crossref 就能补上，不需要新通路。属一次性数据迁移。
2. **`conference-paper` 口径**：它的 container 本就该是会议名，但 `conference_name`
   覆盖率 0%、`container_title` 只 2/6。会议论文的著录字段映射需要单独看
   （OpenAlex 对会议的 `source` 语义与期刊不同），这才是需要设计的部分。

`article_number` 覆盖率 0% 同理待查（电子期刊用它替代页码）。归属 F-6。

**已修（同批）**：docx 初稿的参考文献抽取 + 「未核验」如实告知，见 B-9。

---

### B-9 · docx 初稿的文献不入 facts；抽取被误当成核验 —— 已修

**问题 1：docx 抽到 0 条。** `chat.py` 的 `_TEXT_EXT`（= `SECTIONABLE_EXTS`，
含 docx）会调 `extract_refs`，但 `extractor.py` 里只有 `is_tex(ext)` 和
`ext in ("md","markdown")` 两个分支 —— docx 都不匹配，静默返回空。实测同一份内容
docx 抽 0 条、md 抽 2 条，而 docx 投影文本本身就是 `[1] Title` 这种行，形态和 md 一致。

修法：md 分支加上 `docx`。但不能直接复用 —— docx 投影是**一段一行**，正文段落若以
`[3]` 开头就会被 `_MD_REF_LINE`（行首约束）当成条目。故抽取限定在参考文献区内
（复用 `doc_stats.split_bibliography`），切不出区则退回全文以保持旧行为。

**问题 2：抽取被当成核验。** `extract_refs` 产出的 `KnownRef.verified` 恒为 False，
且这个字段**没有任何消费者**（全仓只有两处 `.verified`，都是检索链路的
`outcome.verified` 计数，与它无关）。于是系统既不核验用户原稿的文献，也不说自己
没核验；而载入时那句「抽取到 N 条文献引用」很容易被读成「这 N 条已验过」。

修法（第一版）：不为 `verified` 造消费者，而是在那句话后面补一行
`_note_refs_unverified` 声明「这些未查证」。

> **第二版（2026-08-12）：改成真去核验，声明已删。**
>
> 只声明「我没查」价值有限 —— 用户想知道的是「这条到底存不存在」。新增
> `literature/draft_refs.py`：解析题名 → Crossref `query.bibliographic` 反查 →
> 比题名相似度，四态 MATCHED / MISMATCH / NOT_IN_SOURCE / UNCHECKED。
> `_note_refs_unverified` 与其 4 个测试一并删除。
>
> 三个实测才发现的要点：
> - **Crossref 模糊检索几乎总有返回**。编造的题名会拿回一篇不相关的真论文，所以
>   「有候选」不能判问题，必须看题名相似度（≥0.75 通过，[0.55,0.75) 才提示不符）。
>   最初按「有候选就是元数据不符」写，一连真源就露馅。
> - **「Crossref 查不到」绝不表述为「疑似虚假」**。中文期刊、书籍、学位论文大量
>   不在 Crossref；误报一多，用户学会无视提示，连真造假也一起漏掉。
>   `PROVIDER_ERROR` 同理走 UNCHECKED —— 把网络故障说成文献造假是最坏的错。
> - **成本要交代**。单条约 2s，36 条初稿 ≈ 73s，故打印预估耗时、支持 Ctrl-C 跳过，
>   且中断时已验部分照样落盘（`draft_refs_cache.json`，键为归一化题名）。
>   UNCHECKED 不入缓存，否则一次网络故障会被永久固化成结论。

**同批修的另两处**：

- **自动编号列表抽 0 条**：文献表用 Word 的 `List Number` 样式时，编号由 `numPr`
  渲染、**不在文本流里**，投影只剩 `Zhang X. Visual SLAM. 2021.`，而 `_MD_REF_LINE`
  要求行首有 `[n]`。加位置回退：按段落顺序补 1..n（Word 自动编号默认也是 1 起顺排，
  故与用户看到的一致）。只在显式编号**一条都没抽到**时才走，避免同篇稿子里编号
  来源不一致。
- **Crossref 301 重定向**（`http_util.get_with_retry`）：httpx 默认不跟随，被取代的
  DOI（`10.55417/fr.2023001` → `10.1109/tfr.2024.3492160`）回 301 → 被归到非 404
  的失败分支 → `RetrievalUnavailable`。于是**记录明明在库里**却报「源不可用」，
  回填静默留空。由「51 条里唯独 1 条回填失败」暴露。已固定 `follow_redirects=True`，
  所有走该函数的源都受益。

**顺带**：用真实 Crossref 回填了两个存量项目的著录字段（29 条），有刊名的从
22/51 升到 51/51。改前已备份 `facts.json`。

### B-3 · `add_section_batch` 不是"add_section 的线性叠加"

**执行层面确实是**：`plan_executor` 原生有序串行、失败即停、plan 级 baseline。
用户点名章节的情况（"补充引言、相关工作、结论三章"）**今天在 active 下已经能跑**，
因为 `_ANALYZE_TOOL` 的切分规则规定"作用目标不同必须切分" → 3 个 add_section。零代码。

**但理解层面不是**。三条依据：

1. 触发词全是"用户没点名"的说法：
   `_ADD_BATCH_KW = ["补齐","缺哪些","缺失章节","补全章节","还缺"]`
   这个意图的本质是**读文档状态 → 对比模板 → 提议缺口 → 展开**。

2. 规划栈有一条刻意维持的性质：**操作是消息的纯函数**。
   `analyze(text, llm)` 签名里没有文档状态；grep 确认 `current_sections`
   只出现在 `plan_validator.validate_plan()` 和 `planning_loop` 调它的两处，
   **只用于校验投影，从不参与生成**。

3. 硬限制：`_COMMON_SECTIONS` 有 6 项，`planning_max_operations` 默认 5
   → `budget.check_operations(6)` 抛 `BudgetExceeded("too_many_operations")`
   → `PlanningExit(REJECTED)`。空论文上"补齐所有章节"直接被预算拒。

**建议方案：文本层脱糖**（不动规划栈）

```
消息 → 窄判定命中批量措辞 → 读快照算缺失 → 展示 ✓/— 对照表 → 用户勾选
                            → 改写成显式文本 "新增 引言、结论 两章" → 正常 analyze
```

好处：`analyze` 保持纯函数；复用已有 N×add_section 执行路径；不加 handler；
不碰 `plan_builder` / `plan_validator`。将来"把所有章节都润色一遍"等同类请求可复用。

**注意**：窄判定与 `is_fast_path` 的风险等级不同 —— 它只决定"是否先展示一张表"，
误判代价是多显示一张表；`is_fast_path` 误判代价是按错误意图直接改文档。

**待评估**：它可能同样更适合做成命令而非意图（"列出缺哪些章节"本质是 `:sections` 增强版）。

**动作**：
- [ ] 重新评估「意图 vs 命令 vs 脱糖」三选一
- [ ] 若选脱糖：实现提议步 + 处理 `max_operations` 上限（倾向"一轮最多 5 章"）

---

### B-8 · 「多引几篇文献」这类改写请求落 UNCLEAR，不落 rewrite

**来源**：B-2 删掉 `_BIB_KW` 后的实测发现（不是 B-2 引入的，是它**暴露**的）。

删除前这句话被 `_BIB_KW` 劫为 `fix_bibliography`；B-2 依据 3 写的是「本应是 rewrite」。
删除后实测落 `UNCLEAR`：

```
帮我在相关工作里多引几篇参考文献   -> unclear
参考文献格式帮我修一下           -> unclear
```

**原因**：`_REWRITE_KW` 是 `["改写","重写","补论证","补充论证","换叙述","重新组织",
"改结构","展开","详细写","补一段","加一段","补一句", ...]` —— 「多引几篇」「格式修一下」
一个都不命中。所以这不是 B-2 没做完，是这类措辞**从来没被任何关键词表覆盖过**，
以前只是恰好被 `_BIB_KW` 接住（接错了地方，但至少没落 UNCLEAR）。

**当前行为可接受**：`UNCLEAR` 只打印功能列表，不改文档，是安全的一侧。
active 模式下走 LLM 理解层，本来就不吃这些关键词表，不受影响。

**待评估**：off 模式下要不要扩 `_REWRITE_KW`。倾向**不扩** —— 关键词表加词就是加劫持面
（B-2 的教训正是子串匹配无条件返回），而 active 模式是长期方向。若要做，得先有一批
真实措辞样本，按「误判代价 vs 漏判代价」逐个判，不能凭想象加词。

**动作**：
- [ ] 收集 off 模式下落 UNCLEAR 的真实措辞（可从 `runs/` 里捞）
- [ ] 据样本决定扩表 / 不扩 / 改提示文案（UNCLEAR 的功能列表可以更像"你是不是想…"）

---

### B-4 · 降级通道无计数，会污染 E2E 实验数据

**证据**：`chat.py:_handle_planned_turn`

```python
except Exception as exc:
    print(f"\n理解层出错（{type(exc).__name__}），本轮改用原有流程。")
    return False        # → fall through 到关键词路由
```

**问题**：active 模式下存在一条**不可见的静默降级路径**。跑完 E2E 看到
"20 条请求都处理了"，但分不清是新栈处理 20 条，还是新栈 15 条 + 旧栈兜 5 条。
而且洞正好在最关键处：想测的就是新栈行不行，结果新栈失败时旧栈替它把活干了，
让新栈看起来更行。

**一个无法区分归属的实验，结论无效。**

**动作**：
- [x] `observability/turn_metrics.py`：`TurnMetrics` 归属计数
- [x] `TurnResult.run_error_category`：结构化暴露新栈内部异常类别（不靠解析中文 warning）
- [x] `chat.py` 接线 + `:stats` 命令 + 每 Turn 落盘 `runs/turn_metrics.json` + 退出时摘要

---

## 一·补 · 停止判断：四维决策表（替代早前过于简化的双层说法）

**先纠正一个流传的误解**：Anthropic **没有**说"不应该用 LLM 判断是否结束"。
[长任务 harness 那篇](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
描述的是一个**限定场景**的失败模式：跨 context window 后，**失忆的新 agent 实例**
环顾一圈、看到有进展、就宣布完工。关键限定词是"a later agent instance"——
错误不在"用 LLM 判断"，而在"信息不全时用 LLM 判断"。
Claude Code 在单个 turn 内恰恰完全依赖 LLM 判断停止（`while(tool_call)`）。

**真正的决策维度是四个，不是"确定性 vs 开放"一个**：

| 维度 | 倾向 LLM 判断 | 倾向程序判断 |
|---|---|---|
| 目标可验证性 | 不可验证（"答好了吗"） | 可验证（"配额够了吗"） |
| 信息完整性 | 单 context，信息完整 | 跨 context，实例失忆 |
| 监督 | 有人在旁（可打断） | 无人监督（后台/子 agent） |
| 副作用 | 只读 | 有写入 / 不可逆 |

**第一行与直觉相反**：目标可验证时用 LLM 判断不是"也行"，是**更差且浪费**——
`_evaluate_gaps` 能精确算出还差几篇中/英文，让 LLM 说"够了吧"是把确定答案换成概率答案。
反之，目标不可验证时用 LLM 判断**不是选择，是唯一出路**。

**结论**：
- 写作意图（目标可验证）→ 保持 `gaps` + Gate，**红线：不得替换为 LLM 判断**
- 顶层 agent loop（目标不可验证）→ LLM 判断 + 外层护栏

### 参考实现证据（`claw-code`）

**重要澄清**：`d:\Users\yangwan\mProject\claw-code` **不是 Claude Code 源码**，
是 `ultraworkers/claw-code`——第三方 Rust 重写的 agent harness（README 自述
"public Rust implementation of the `claw` CLI agent harness"，带 `PARITY.md` 追平表）。
它的设计选择不代表 Claude Code，但与官方文章描述的模式互相印证。

`rust/crates/runtime/src/conversation.rs` 的 `ConversationRuntime::run_turn`：

```rust
loop {
    iterations += 1;
    if iterations > self.max_iterations {              // 外层：程序硬上限
        let error = RuntimeError::new(
            "conversation loop exceeded the maximum number of iterations");
        self.record_turn_failed(iterations, &error);
        return Err(error);                             // 超限是 Err，不是 Ok
    }
    ... stream() → build_assistant_message() ...
    let pending_tool_uses = /* ToolUse blocks */;
    if pending_tool_uses.is_empty() { break; }         // 内层：LLM 决定停止
    for tool in pending_tool_uses {
        run_pre_tool_use_hook → permission check → execute
    }
}
```

三个可借鉴细节：

1. **就是双层**：内层 LLM（不再要工具就停）+ 外层程序硬上限。
2. **超限返回 `Err` 不是 `Ok`**：迭代耗尽是失败，不是"任务完成"。
   与本项目 `StopReason` 六态枚举同一哲学——停下来必须说清为什么。
3. **上限值按监督程度分档**（决定要不要硬上限的不是"任务开放性"，是"有没有人在旁边"）：

| 场景 | max_iterations | 位置 |
|---|---|---|
| 主对话（用户在旁） | `usize::MAX` | `conversation.rs:181` |
| sub-agent（无人监督） | `32` | `tools/src/lib.rs:3238 → 3352` |

另：`with_auto_compaction_input_tokens_threshold` 表明**上下文管理是循环的一部分**，
不是外挂。

### 顶层 agent loop 的四层护栏（P-4 设计输入）

```
内层    LLM 不再请求工具 → 停                （唯一可行，也应该这样）
护栏 1  硬迭代上限（只读问答 8–12 足够）
护栏 2  超限 → StopReason.ITERATION_LIMIT_EXCEEDED，绝不报"回答完成"
护栏 3  复用 BudgetLedger（llm_calls / elapsed_s / provider_time_budget 已有）
护栏 4  只读工具集 → permission prompter 整套机制一行都不用写
```

护栏 4 是只读版最大的成本红利：claw-code 每个工具调用前都要过
`run_pre_tool_use_hook` + `PermissionPrompter`，因为它的工具能写文件、跑命令。

**修正早前一个说法**：我曾说"没有可验证目标时不要开循环"，这偏死了。
更准确：**没有可验证目标时，循环必须靠硬上限而非目标来兜底，且上限按监督程度定**。
第一版不循环仍是合理的保守起点（省掉一整类问题），但不是原则性要求。

---

## 二、技术债

### D-1 · `rule_analysis.py` 是运行时死代码，且 `is_fast_path` 是危险半成品

**证据**：grep `rule_analyze` / `is_fast_path` / `Rule_Analysis` —— 运行时零调用，只有测试在用。
但两处文档声称它在流程里：
- `turn_service._handle_active` docstring：`run 内：rule_analyze → 规划循环`（代码直接进 `run_planning_loop`）
- `tasking/__init__.py`：`rule_analysis → operation_analyzer → extractors → ...`

**`is_fast_path` 的风险**（实测，对抗性输入）：

```
输入                          fast?   候选
帮我润色引言                    FAST    polish            ✓
不用润色，保持原样               FAST    polish            ✗ 否定被无视
别润色了                       FAST    polish            ✗
这篇论文的语言是谁润色的？        FAST    polish            ✗ 提问当成指令
这段读起来还行，先别动            -      polish            偶然逃过（"先"命中序列词）
润色之前先告诉我有哪些章节        -      polish            偶然逃过（同上）
```

10 条里 3 条误判为 FAST，且逃过的两条靠运气不靠理解。根因：

```python
def _matched(low, keywords):
    return [kw for kw in keywords if kw.lower() in low]   # 纯子串，无否定/疑问感知
```

用一个读不出"别"和"不用"的判据去决定"我不需要 LLM 了"，方向是错的。
`Rule_Analysis` 文档里的另一用途"Analyzer 失败降级"受同样污染 ——
在最不确定的时刻改用最弱判据，且会把"理解失败"悄悄变成"按关键词猜着执行"，
违反项目其他地方（`relevance.py` / `verification.py`）贯彻的**失败不伪装成结论**纪律。

**决策**：降级为**纯观测**。

- 删 `is_fast_path`（危险的那半）
- 保留 `rule_analyze`，但**只写 trace、不参与任何决策**
- 观测价值：免费得到"规则层认为是 X / LLM 认为是 Y"的分歧统计，
  这是将来判断"能不能删 `router.py`"的**唯一客观依据**
- 修两处撒谎的文档

**动作**：
- [ ] 删 `is_fast_path` 及其测试
- [ ] `rule_analyze` 接成 trace-only 旁路
- [ ] 修 `turn_service._handle_active` docstring 与 `tasking/__init__.py` 数据流描述

---

### D-2 · 双理解栈并存，且"legacy"才是在服役的那套

**命名陷阱**：所有 flag 默认 off ⇒ `router.py` + `interview.py` 是**当前生产路径**，
`tasking/*` + `runtime/*`（~3000 行）默认一行不跑。

**"删除 legacy"不是删除，是重构**。`Intent` 枚举定义在 `router.py`，被 13 个模块 import，
全部来自新栈：
```
tasking/{contracts,plan_builder,plan_validator,operation_analyzer,intent_spec,clarifier,rule_analysis}.py
tasking/extractors/{__init__,base,add_section,rewrite,polish,convert,format_docx}.py
runtime/{handlers,plan_executor}.py
```
`PolishPlan`/`RewritePlan`/`AddSectionPlan`/`ConvertPlan` 定义在 `interview.py`，
被 `handlers.py`（新栈）**和** `intents/{polish,rewrite,convert,add_section}.py`（执行器）同时 import。
`intent_spec.py` 复用 `interview._FORMAT_ALIASES`（注释：避免双份真相源）。

**修正先前判断**：真正可删的只有 `Router.route()` 的关键词分支 +
`interview_polish/convert/rewrite/add_section` 四个问答函数，约 300–400 行，
**不是 3000 行**。其余是新旧共用的词汇表与执行器。
共用本身是**对的**（单一真相源），只是词汇表寄居在旧模块里造成"看起来有两套"的错觉。

**删除前置条件**（缺一不可）：
1. E2E 跑通（新栈开着）
2. shadow / trace 分歧统计（依赖 D-1）
3. 默认值翻 on 并使用一段时间
4. 把 `Intent` 与 `*Plan` 挪到中立模块

**注意**：不能先删。整个 B0 方法论建立在"legacy 是行为锚点"上，
`test_retrieval_throughput_b1.py` monkeypatch `_run_attempt_legacy` 验证全关路径不进新分支。
且 E2E 从未跑通 —— 先删再测，失败时分不清是新栈 bug 还是删除的连带损伤，且没有可回退基线。

**动作**：
- [ ] 满足四个前置条件后再启动
- [ ] 给 legacy 定明确的死亡条件（否则每代新能力都在给越来越宽的 flag 矩阵交税）

---

### D-3 · flag 组合矩阵指数增长

`attempt.py` 已 1139 行，同时维护 `_run_attempt_legacy` + B1 + B2 三条路径。
`retrieval-throughput/tasks.md` 要求测 8 种组合，且规范明写
"legacy 是行为锚点，不能以重构整洁为由删除它"。B3 一上会变成八路分支。

**风险**：正确性成本超过吞吐收益。

**动作**：
- [ ] 暂停 B3，先给每条 legacy 路径定删除条件，把矩阵从 8 压回 2–3

---

### D-4 · 两套预算体系互不相识

| 体系 | 位置 | 计量 |
|---|---|---|
| 写作证据集预算 | `build_writing_orchestrator(budget_chars=8000)` 默认参数硬编码 | 字符 |
| 相关性窗口预算 | `BatchInputBudget`（有 `max_input_tokens` + 注入 tokenizer） | token / 字符 |

一个管相关性判定窗口，一个管写作证据集。应收敛为统一预算 + 注入 tokenizer。

**动作**：
- [ ] 收敛（这是**收敛**不是新建，见三-2 的评估）

---

### D-5 · 工程化配套缺失

全仓库搜索确认不存在：README / CI / ruff / mypy / requirements / lockfile。
且 `pyproject.toml` 的 `dependencies = []`，真实依赖 `openai` / `httpx` 全在
optional 里且未锁版本 —— **依赖不可复现 = 不可部署**。

**动作**（约一小时）：
- [ ] `README.md`：架构图 + 怎么跑 + 当前 flag 状态表
- [ ] `pyproject.toml` 填真实依赖并钉版本
- [ ] GitHub Actions 跑 pytest（170s，完全可行）
- [ ] `[tool.ruff]` 基础配置

---

### D-6 · 文档漂移

- `retrieval-throughput/tasks.md`：Task 1–6 未勾选，Task 7、8 已勾选。
  但 B1 代码（`relevance_batch.py` / `window_packer.py` / `call_control.py` / 预算 reservation）
  与对应测试都已存在 —— **勾选状态与代码状态不一致**。
- `scripts/chat.py` docstring 仍写"Week 1 极简 Chat REPL""Week 1 仅 polish"，
  实际已 import TurnService / Coordinator / planning / literature 全套。
- `literature-evidence` 一个 feature 有 23 个 md（design-a2a…a2f、tasks-a2c-1…a2c-5），
  已完结的应归档。

**动作**：
- [ ] 同步勾选状态、更新 docstring、归档已完结 spec

---

### D-7 · 发布门槛自锁

`tasks.md` 要求 ≥200 条人工标注、≥30 次 replay、≥5 个 live 配对才允许讨论默认开启。
Task 6A 记录人工仲裁 **0/40**。B2 工程 gate 已 GO，发布 gate 因人工标注为零持续 NO-GO。

诚实是对的（如实写 NO-GO 而非调低阈值），但结果是所有 flag 永久停在 off。

**更关键的问题**：你现在**不知道该标哪 200 条**。
标注集应该从真实失败样本里长出来 —— 这依赖 E2E（见 P-1）。

**动作**：
- [ ] E2E 之后重定门槛为可达成的（如 40 条单标注者 + 明确标注"非双盲金标准"）

---

### D-8 · 测试残留物未清理

`.paper_workspace/` 下有大量 `pytest-a2d-20260730/test_*` 目录。
`.gitignore` 已覆盖 `.paper_workspace/`，不入库，但本地堆积。

**动作**：
- [ ] 测试 fixture 加清理，或定期手动清

---

## 三、下一阶段提案评估

### P-1 · 真实端到端 —— ✅ 已完成（2026-08-01，首次跑通）

**配置**：writer `anthropic/qwen3.7-max`；reviewer `openai/gpt-5.6-sol-2026-07-09`（DIVERSE=True）；
`PAPER_RETRIEVAL=openalex`；`SEARCH_SYNTHESIS=true`；`QUERY_PLANNER=hybrid`；
课题「面向空地协同的大视角大尺度差图像匹配研究」；配额 `total≥5 en≥5 zh=0`。
归档：`tests/eval/literature_dogfood/baseline_2026-08-01_total5_en5.txt`

**结果**：

```
[0] 合成检索意图 status=ready
    语义主题='面向空地协同场景的跨视角、跨尺度图像匹配与视觉定位技术'
    5 seeds（3×language.en + 2×language.zh）
[1] 初始缺口 total=5 en=5
[2] · attempt1: cand=10 accept=0 reject=10 verify=20 elapsed=69.9s
    · attempt2: cand=10 accept=1 reject=7  verify=34 elapsed=121.2s
    停止 = budget_exhausted (provider_time_budget_s)
    iterations=1 search_calls=2 verify_calls=34 llm_calls=18 candidates=20 elapsed=121.3s
[3] reject=17 accept_new=1
    拒因：relevance:weakly_relevant=10、relevance:unrelated=7
[4] 入库 1 条（en/high）
[MISS] 仍缺 total=4 en=4
```

**链路完全走通**：SEARCH → 清洗/快照 → 语言 → 精确去重 → 资格预筛 → 严格核验
→ 相关性 → BatchItem → commit → REASSESS → 报告。
入库记录带 `doi=verified@crossref/final; openalex_work=verified@openalex/final`
——Crossref 对 DOI 的 final 权限 + OpenAlex 佐证，A2c-1 严格核验按设计工作。

#### 首个真实性能基线（retrieval-throughput 此前缺的锚点）

| 指标 | 实测 |
|---|---|
| 候选 | 20 |
| active elapsed | 121.3s（打满 120s 预算） |
| verify calls | 34 |
| LLM calls | 18 |
| 平均 | **~6s / 候选** |
| iterations | **1**（预算首轮即耗尽） |

拆解：18 次 GPT-5 判定 × ~3.5s ≈ **63s（约占一半）**；余下主要是 34 次 verify 网络往返。

#### F-1 · 真正的瓶颈是检索质量，不是吞吐也不是判定

被拒候选的标题揭示 OpenAlex 关键词检索语义完全失效：

```
Ground-Water Hydraulics / Basic ground-water hydrology     ← "air-ground" 被拆成 ground+water
Matching roots to their environment                        ← "matching" 当普通词
A Review of Deep Learning in Medical Imaging
The Heliospheric Imagers Onboard the STEREO Mission
Passive millimeter-wave imaging
```

7 个 `unrelated` 全是此类噪声。**GPT-5 reviewer 把它们全判对了 —— 判定层没问题，是输入太脏。**

> **状态：已修（2026-08-05），真机受控 A/B 验收通过 —— 见 F-3b。**
> 上面「关键词检索语义完全失效」的直觉是对的，但归因的**机制**当时没找准：
> 不是 OpenAlex 不认语义、也不是查询词太宽，而是 `search=` 的 OR-ish **词袋语法丢词序**。
> 同样的词加引号即 10/10 切题。受控 A/B：`unrelated` 12 → 5。
> 上面 `air-ground` 被拆成 ground+water 的观察，正是词袋丢相邻性的直接证据。

**结论：优化 relevance 吞吐（B1）之前，先提高检索召回质量的收益更大。**
候选池 20 个里只有 1 个切题，再怎么加速判定也造不出文献。

> **后续（2026-08-06）：F-1 修完后 B1 随即验收并转默认开启 —— 见 F-7。**
> 上面这个先后判断是对的（先修召回、再优化吞吐），但「B1 只优化 63s 里的一部分」
> 低估了它：实测 relevance LLM 占 125s 里的 89.9s（72%），B1 是最大的那一块。

#### F-2 · reviewer 判定谱系合理，GPT-5 可用

| 候选 | 判定 |
|---|---|
| 地下水 / 医学影像 / 日球层成像 | `unrelated` ✓ |
| Image Matching Survey、UAV object detection、street view imagery | `weakly_relevant` ✓ |
| A Practical Cross-View Image Matching between UAV and Satellite | `relevant` ✓ |

三个候选 reviewer（gpt-5.6-sol / gpt-5.5 / claude-opus-4-7）离线冒烟均 3/3 正确、
`1.00 calls/judge`（原生 forced tool，无兼容开销）。

#### F-3 · `accept_weakly_relevant=False` 是当前主要产出杀手 —— ✅ 已修

10 个 `weakly_relevant` 被拒，其中对**引言/相关工作**明显有价值的至少两条：
`Image Matching from Handcrafted to Deep Features: A Survey`、
`Survey on Computer Vision for UAVs`。

`config.py` 自己的注释就写着「引言常引背景/邻近工作」。

**动手时发现问题比 F-3 描述的更严重（P0）**：「哪些相关性结论算接受」这条规则
实现在**三个地方，其中两个硬编码**：

| 位置 | 修复前接受集合 | 可配置 |
|---|---|---|
| `retrieval/attempt.py::RelevanceAcceptancePolicy` | 由 `literature_accept_weakly_relevant` 决定 | 是 |
| `preflight.py::_ACCEPTED_VERDICTS` | `("relevant",)` | **否** |
| `evidence.py::build_bundle` | `("relevant",)` | **否** |

后果：打开 `PAPER_LIT_ACCEPT_WEAKLY_RELEVANT=true` 会得到「**入库了但永远用不上**」
—— 检索接受 weakly 并提交进 Ledger，但可用性统计不数它、证据集不选它 →
配额永远算不满、写作永远看不到。**flag 只改了摄入、没改消费。**
这与项目其他处刻意维持的「单一真相源」纪律矛盾
（对比 `relevance_content_hash` 的注释：读写两侧必须走同一函数）。

**修复**：
- [x] `RelevanceAcceptancePolicy` / `STRICT_ACCEPTANCE` / `WEAKLY_ACCEPTANCE`
      迁至 `literature/relevance.py`（判定词汇的定义地）；`attempt.py` re-export 保兼容
- [x] `assess_availability(..., acceptance=)` 与 `build_bundle(..., acceptance=)`
      改为**注入**，默认 `STRICT_ACCEPTANCE` → 不传时行为逐字节不变
- [x] `acceptance_for_purpose(purpose, *, force_weakly=False)` 纯函数解析器；
      未知用途**降级为更严格**（与 `_env_lang_policy` 同纪律，不静默放宽）
- [x] 编排器 `WritingLiteratureOrchestrator.run()`：**一次解析、三处注入**
      （Attempt 经 `dataclasses.replace(deps, acceptance_policy=...)`）
- [x] config 新增 `literature_acceptance_mode`：`strict`(默认) | `by_purpose` | `permissive`；
      非法值降级 strict 并告警；旧 bool `=True` 等价 `permissive`（向后兼容）
- [x] `tests/test_acceptance_policy_consistency.py`（22 项），含**属性式**断言：
      任意 (策略 × 判定) 组合下 `availability` 与 `bundle` 的取舍必须一致 ——
      这两处一旦分叉就会出现「统计说够了但证据集是空的」

**注意**：收 weakly **不放宽 `title_only`** —— 无摘要候选仍不接受（保守规则不变）。

#### F-3 第一版是「静默失效」的 —— 已修（第二版）

第一版按 `section_purpose` 做**精确集合匹配**：

```python
if section_purpose in {"introduction_background", "introduction", ...}: 收 weakly
```

真机探针实测（2026-08-01）该字段两条路径的**实际值**：

```
deterministic: section_purpose = '面向空地协同的大视角大尺度差图像匹配研究'
llm:           section_purpose = '面向空地协同的大视角大尺度差图像匹配研究'
```

原因：`SearchIntent.section_purpose` 是**自由文本** —— 确定性合成器直接用
`ctx.purpose`，LLM schema 里该字段是 `{"type": "string"}`（无 enum、无 description），
模型不填时 `payload.get("section_purpose", "") or ctx.purpose` 回落到同一值。
而 dogfood runner 传的 `purpose=topic`（整个课题名），真实 handler 传的是用户访谈原话。

**精确集合永不命中 → 静默落回 strict → 改动等于没做。**

**为什么 22 个测试没抓到**：测试喂的是 `"introduction_background"` ——
一个**实践中从不出现**的合成字符串。测了函数逻辑，没测数据能否流到函数。

**第二版修复**：
- [x] `acceptance_for_purpose(*signals, force_weakly=False)`：改**关键词子串匹配**，
      接受多个信号（可靠性从高到低）
- [x] `WritingEvidenceRequest.section_kind`：由**知道章节身份的那一层**显式声明
      （Handler 用 `plan.title` / `plan.section_id`；runner 用 `"引言"`）。
      已验证**不进** `task_fingerprint` / checkpoint 哈希 → 不失效已缓存判定
      （接受策略是**读时投影**，同 `status_under_policy`）
- [x] 编排器 `_resolve_acceptance(request)` 按 `section_kind` → `SearchIntent.section_purpose`
      → `criteria.section_purpose` 依次取信号
- [x] runner 也接线（此前直接调 `assess_availability` 不传策略 → 恒 strict）
- [x] 删掉 `PURPOSE_ACCEPTS_WEAKLY`：它含裸 `"background"`，与刻意排除该词的
      `_WEAKLY_SIGNALS` 冲突 —— **又一个第二真相源**，由新增测试守住不得复现

**刻意排除裸「背景」/「background」**：在视觉领域它们是**研究对象**而非章节名
（"复杂背景下的红外小目标检测"、"background subtraction"）。当关键词会造成
**放宽**方向的误判，违反本项目「降级恒为更严格」纪律。已固化为测试。

**验证**：`tests/test_acceptance_policy_consistency.py` 33 项（含用真实字符串的回归）；
全量 **1124 passed**，无回归。

#### F-3 真机复测结果（2026-08-01，`by_purpose`）

归档：`tests/eval/literature_dogfood/run_2026-08-01_by_purpose_total5_en5.txt`
基线 facts 另存 `_facts_baseline_strict.json`（strict 的 1 篇 + 17 条判定）。

**离线预测**（用基线那 17 条判定重算，零成本）：STRICT 接受 1 → WEAKLY 接受 9。
`weakly+有摘要`=8 收、`relevant`=1 收、`unrelated`=7 拒、`weakly+title_only`=1 **仍拒** ✓

**实跑结果**：

| | 基线 strict | by_purpose |
|---|---|---|
| 入库 | **1** | **9** |
| 处置 | accept 1 / reject 17 | accept 9 / reject 8 |
| 拒因 | unrelated 7、weakly **10** | unrelated 7、weakly **1**（title_only） |
| 配额 total≥5 en≥5 | 仍缺 4 | ✅ **满足**（首次） |
| 候选 / 耗时 | 20 / 121.3s | 20 / 123.6s |
| verify / llm calls | 34 / 18 | 34 / 17 |

runner 现在首行打印 `接受策略: mode=... section_kind=... → 接受判定=[...]`，
**一眼可确认模式真的生效**，不会再出现第一版那种静默失效。

#### F-3a · 配额满了，但质量参差 —— F-1 仍是真瓶颈（诊断已更精确）

入库 9 篇人工核对：

| 判定 | 年份 | 标题 | 切题度 |
|---|---|---|---|
| relevant | 2021 | UAV-Satellite View Synthesis for Cross-View Geo-Localization | ✅ 核心 |
| relevant | 2020 | A Practical Cross-View Image Matching between UAV and Satellite | ✅ 核心 |
| relevant | 2014 | Accurate Geo-Registration by Ground-to-Aerial Image Matching | ✅ 核心 |
| weakly | 2020 | Image Matching from Handcrafted to Deep Features: A Survey | ✅ 引言必引 |
| weakly | 2006 | Photo tourism | ⚠️ 勉强（大规模匹配奠基工作） |
| weakly | 2013 | UAV for High-Resolution Reconstruction of Topography | ❌ 地形重建 |
| weakly | 2012 | Georectified Mosaics from Ultra-High Resolution UAV | ❌ 正射镶嵌 |
| weakly | 2012 | UAV PHOTOGRAMMETRY FOR MAPPING AND 3D MODELING | ❌ 摄影测量综述 |
| weakly | 1999 | Flexible syntactic matching of curves | ❌ 曲线句法匹配 |

**4 篇真切题 + 1 篇勉强 + 4 篇偏题。**

**新诊断（比 F-1 原描述更精确）**：噪声性质变了。
- 基线那次是**纯噪声**（Ground-Water Hydraulics、植物根系、医学影像）——"air-ground" 被拆词
- 这次是**领域相邻但主题不对**：seeds 里的 `UAV` 是个过宽的词，把 OpenAlex 带进了
  **无人机遥感应用**领域（地形重建/树木检测/树皮甲虫/作物表型），而不是**图像匹配算法**领域。
  7 个 `unrelated` 全是这一类。

另一个信号：入库有 1999 / 2006 / 2012 / 2013 的老论文且都是高被引经典 →
**OpenAlex 的相关性排序偏好被引量而非主题匹配度**。

**对 F-1 的启示**：
- 不是"检索完全失效"，而是**查询词粒度问题**：`UAV` / `image matching` 这类宽词
  召回的是应用领域大热门，不是问题域论文
- 可能的方向（待验证）：seeds 更长更专（`cross-view` + `viewpoint change` + `scale difference`
  组合而非单 `UAV`）、限定 OpenAlex 的 concept/field、按年份加权、或改用支持语义检索的源

> **⚠️ 上面这条「更长更专」的方向已被实测否证（2026-08-05）**，见 F-3b。
> 真因是**查询语法**（词袋丢词序），不是查询词粒度；合成器早已在给长组合 seed。

**尚未触达的路径**：runner 在 `[OK] 配额可满足` 就结束，**没有调 `build_bundle`**。
所以 P-3a 质疑的「补满预算」那步、以及 `READY_TO_WRITE` 之后的写作链路仍未跑过。

#### F-3b · F-1 的真因是查询语法，不是查询词粒度（2026-08-05，已修）

F-3a 建议「seeds 更长更专」。直连 OpenAlex 实测（零 LLM、可复跑）**否证**了它，
且发现合成器**早就在做**它建议的事 —— `last_run.txt` 记录的真实 seeds 是
`cross-view image matching UAV ground vehicle` 这类长组合，不是单 `UAV`。再加长反而更差：

| 查询 | total | 前 10 条切题 |
|---|---|---|
| `cross-view image matching UAV ground vehicle`（现状 seed） | 9423 | 2/10 |
| 加长到 `... large viewpoint change scale difference air-ground`（F-3a 建议） | 7370 | **1/10** |
| `"cross-view image matching"`（同样的词，**加引号**） | 215 | **10/10** |

**真因**：OpenAlex `search=` 是 OR-ish 词袋 + 相关性排序，词越多匹配面越大、越向
该领域高被引大热门倾斜，**词序相邻性在词袋里被丢掉**。所以 `air-ground` 招来
Ground-Water Hydraulics，`UAV` 招来树皮甲虫和作物表型。这也顺带解释了 F-3a
观察到的「OpenAlex 偏好被引量而非主题匹配度」——不是排序器偏心，是**候选池本身**
就是宽词袋捞出的高被引集合，排序器只能在这个池子里挑。**症状在排序，病在语法。**

**已实施**（`openalex.py::build_phrase_first_query` + 两级阶梯）：
引住前 3 词、其余留自由词；命中 < `min(limit,5)` 则**丢弃并回退裸词**。
⇒ 召回下界与改造前完全相同，最坏只多一次 GET（≈1.6s，对照单 attempt ~72s）。
flag `literature_openalex_phrase_first`（默认开）；核验路径（`verify_adapters`）
**显式关掉**——书目反查给的是完整标题，短语优先只会白花 HTTP（基线 34 次 verify）。

跨 4 主题验证，无一例显著变差（联邦学习裸词已 10/10，持平）：
跨视角 2/10→**7/10**、大模型代码生成 5/10→**10/10**、知识图谱问答 7/10→**10/10**。

**引号会打穿到 0**：`"air-ground visual localization"` → 0 命中；整句引住同样 → 0；
中文 seed 加引号也有害（`"空地协同" …` 2 条 vs 裸词 202 条）。故三重守卫 + 回退缺一不可。

##### 真机验收（2026-08-05，受控 A/B）

**先踩了一个坑**：直接跑两臂 A/B **不干净** —— LLM planner 每次吐的 seeds 都不同，
其中一臂它**自己**就吐出了带引号的 seed（`"cross-view image matching" UAV ground`），
那一臂策略是 `bare`（已含引号则不二次加工）、结果也不差。
⇒ 必须 `PAPER_LIT_SEARCH_SYNTHESIS=0` + 钉死 `--query`，才能让两臂唯一差异是本改动。

同 query（`cross-view image matching UAV ground vehicle`）、同配额（total≥5 en≥5）：

| 指标 | A 关闭 | B 开启 |
|---|---|---|
| `relevance:unrelated` | **12** | **5** |
| 入库可用 | 4（配额未满足） | 6（满足） |
| verify_calls | 32 | 26 |
| llm_calls | 16 | 11 |

A 臂捞的是「UAV 应用大全」：树皮甲虫、作物表型、火星硫酸盐、冰湖识别、土壤湿度降尺度。
B 臂是 UAV-VisLoc 数据集、跨视角注意力掩码、卫星影像实时地理定位、UAV 大倾斜影像定位。
**顺带收益**：脏候选少了 ⇒ 不用挨个核验，verify/llm 调用同步下降；某次非受控跑
停止原因从 `budget_exhausted` 变成 `quota_satisfied`。

关键词代理预测 2/10→7/10，reviewer 实测 unrelated 12→5，方向与量级一致
⇒ 该代理指标可以继续当先导指标用。

**局限**：单主题、每臂 n=1。LLM 判定有随机性，给不出置信区间。12→5 的量级不像噪声
（配合 4 主题代理指标 + 多次非受控跑的一致方向），但**不声称统计显著**。

**连带修掉 2 个接线漏洞**（都属第七节「能力写对了，接线送不到」）：
- `PROVIDER_SEARCH` 没进 `recorder.py` 白名单 ⇒ 事件发了但 trace.json 里没有。
  已补 + 加测试专盯白名单。**教训重复了**：见记忆 observability-blind-spot-literature。
- **dogfood runner 从来没有观测**：它直装 orchestrator、不走 Coordinator ⇒ 没人登记
  ambient bus ⇒ provider 层与 `structured_call` 埋点在这条路径上全是 no-op、`runs/` 不产 trace。
  已在 runner 内自建 bus 并打印查询策略分布（上表的 `phrase_first`/`bare` 归因即出自它）。
- flag 只在 `Config` 有默认值、`from_env` 未映射 ⇒ 已加 `PAPER_LIT_PHRASE_FIRST`（A/B 开关）。

---

## 七、系统性问题：「能力写对了，接线送不到」

本次会话**三次**同一类 bug，且失败全是静默的：

| # | 表现 | 根因 | 测试为何没抓到 |
|---|---|---|---|
| 1 | `fix_bibliography` 在 active 不可达 | analyzer 不支持该意图，`_handle_planned_turn` 无条件接管 | 没有「active 下每个意图都可达」的测试 |
| 2 | 相关性判定在 OpenAI 端点全失败 | 工具 schema 缺 `type`，Anthropic 容忍/OpenAI 不容忍 | `MockLLMProvider` 无条件吸收 `tools`、不校验形状 |
| 3 | `by_purpose` 静默失效 | 判断字段是自由文本，精确匹配永不命中 | 测试喂了实践中不存在的合成字符串 |
| 4 | 只读问答通路整条无观测（2026-08-12 修） | `plan.md` 列了 `ASK_TURN_START/TOOL_CALL/TURN_END` 三个落点，实现时整项漏做；且 `:ask` 跑在 `Coordinator.run` 之外，压根没有 bus | 25 问真机评测只看**答得对不对**，不看 trace 里有没有；当时 1912 个测试没有一条断言埋点存在 |

**共同结构**：单元测试验证了「函数逻辑对」，但没验证「真实数据能流到这个函数」。

第 4 例多一层教训：**通路的观测面本身也是"接线"**，而它的缺失比功能缺失更难发现 ——
功能坏了用户会说，观测坏了没有人报错，只在事后要归因时才发现查不到。react 通路
（模型自己决定调什么工具）比写死阶段的 workflow 更依赖埋点：后者靠
`GENERATION_ATTEMPT_*`/`GATE_*` 能还原过程，前者不记录就无从复现。
补的测试**读落盘的 `trace.json`**，不 spy `bus` —— `recorder._record_event` 有一张
kind 白名单，只断言"publish 被调了"会在漏加白名单时照样绿。

**建议的纪律（低成本，未实施）**：
- [ ] 每个新 flag/mode 必须有一条**「真的改变了可观测行为」**的测试，
      输入用**真实路径产生的值**，而非合成的理想值
- [ ] `MockLLMProvider` 增「严格模式」：在测试里校验 tool schema 协议合法性（第二道防线）
- [ ] 扩展 `scripts/_wiring_check.py`：把「每个意图在 active 下可达」纳入断言
- [ ] **新通路的 spec 落点清单要逐条勾掉再算完成**。第 4 例就是落点写在
      `plan.md` 里、实现时跳过、没人对账。`agent-loop/plan.md` 现在改成
      「顺序与验收」逐条带 ~~删除线~~ + 完成日期，缺的一眼看得出。

#### F-4 · B2 的离线 gate 严重低估了它的真实收益

B2 自己的报告：verify 28→22（**-21.4%**）。
真实数据：18 个候选进相关性，只有 1 个通过。若先判相关性、只对 survivor 核验，
verify 从 **34 次降到 ~2 次（-94%）**。

原因：离线样本的相关性通过率远高于真实检索。**B2 应按真实数据重新评估优先级 ——
它可能比 B1 更该先上**（B1 省的是 63s 里的一部分，B2 省的是 34 次网络往返里的 32 次）。

> **状态：结论已被真机时间归因否定（2026-08-06）—— 见 F-7。**
> `-94% verify` 这个数字是对的，但它省的是**只占 25% 总时间**的那部分：
> 34 次 verify 累计 31.7s（均值 0.93s），而 15 次 relevance LLM 累计 89.9s（72%）。
>
> **方法论教训：归因要用「秒」，不能用「省掉调用的百分比」。**
> `-94%` 听起来远比 B1 唬人，但摊在 0.93s 的调用上；B1 省的是 6s/次的调用。
> 这是本轮最值得记住的推理错误 —— 比较收益时两边必须换算到同一个单位（时间）。

#### F-5 · 多轮迭代能力在真实条件下从未被执行

`iterations=1`，预算在第一轮就耗尽 → planner 续搜、`_prioritize_seeds` 的多轮逼近、
`NO_PROGRESS` 检测**全都没跑到**。`literature_retrieval_time_budget_s=120` 对真实
条件（6s/候选）太紧。要么调大预算，要么先解决 F-1/F-4 降低单候选成本。

（`--zh 0` 时 2 个中文 seed 被正确跳过：`search_calls=2` 只用了英文 seed ✓ 设计如预期。）

> **状态：已解（2026-08-06，B1 默认开启后）。** 走的是「降低单候选成本」这条路、
> 没动预算：`iterations=3`、`search_calls=3`，`stop_reason` 首次为 `quota_satisfied`。
> `_prioritize_seeds` 与 planner 续搜第一次真正执行。见 F-7。

#### F-6 · 落盘记录质量（B-2 的直接输入）

`relevance_store` 缓存了全部 17 条判定（重跑不会重判 ✓）；
`committed_batch_ids` 幂等键正常；入库记录字段：

```
bibkey            = ding2020practical          （自动生成 ✓）
numeric_aliases   = ["1"]
work_identifiers  = doi:10.3390/rs13010047, openalex_work:W3115854999
authors           = 4 人 ✓      year = 2020
container_title   = ""     volume = ""     issue = ""     pages = ""   ← 全空
container_identifiers = issn:20724292
```

**对 B-2 的意义**：即使是完全核验通过的记录，也缺 `container_title/volume/issue/pages`。
有 authors + year + DOI，够写一条**最小但合法**的条目，但写不出完整期刊引用。
B-2 的「补全 `\bibitem`」要接受这个上限，或增加一次 Crossref 元数据回填。

（另注：OpenAlex 给的 `year=2020`，而 DOI `10.3390/rs13010047` 实际是 Remote Sensing
2021 年卷期 —— 单源元数据存在偏差，交叉核验目前只验存在性、不校对年份。）

#### F-7 · 时间归因：relevance LLM 占 72%，B1 已验收并转默认（2026-08-06）

**先补埋点。** 做归因时发现 verify 路径**完全没有埋点** —— 要优化的东西没被测量。
补了 `VERIFY_FETCH`（含缓存命中标记），并给 `LLM_CALL_END` 加了 `label` 字段
（judge_relevance / propose_search_plan 分桶用；此前 label **只存在于 span 名**里，
attributes 读不到）。两者都被 recorder 白名单挡过 —— 详见下方「白名单」教训。

**归因结果**（真机，phrase_first ON、synthesis OFF、`--total 5 --en 5`）：

| | 次数 | 累计 | 均值 |
|---|---|---|---|
| relevance LLM | 15 | **89.9s（72%）** | 5.99s |
| verify 网络往返 | 34 | 31.7s（25%） | 0.93s |

**B1 受控 A/B**（同上条件，约 120s 预算）：

| 指标 | B1 关 | B1 开（size=10） |
|---|---|---|
| relevance LLM 调用 / 累计 | 15 / 89.9s | **5 / 51.1s** |
| **LLM 秒 / 候选** | 4.50 | **2.15（-52%）** |
| 候选数 / 入库可用 | 20 / 6 | **30 / 17** |
| iterations | 2 | **3** |
| `stop_reason` | `budget_exhausted` | **`quota_satisfied`** |

**最关键的一行是 `stop_reason`：项目首次在受控条件下正常收工而非预算耗尽。**
省下的时间被用于多跑一轮检索 ⇒ 连带解决 F-5（多轮迭代首次执行）。

**判定质量未退化**：`unrelated` 占比 25%→20%（6/30）。新入库的是 CVM-Net、
Lending Orientation to Neural Networks、Optimal Feature Transport 等跨视角匹配核心工作，
未见「同批里前一篇判 relevant 就跟着放宽」的迹象（这是批量判定的主要风险）。

**已转默认**：`literature_relevance_batch_enabled=True`、`batch_size=10`（dataclass 与
env 两处）。注意二者**必须同改** —— `_active_relevance_runtime` 对 `size<2` 直接抛
`CompositionUnavailable`，「开了 batch 但 size=1」是不可装配的组合。
改默认值连带修了 4 处依赖旧默认的测试（详见下方「改默认值」教训）。

**新瓶颈是 verify**：占比 25%→43%（49.4s / 50 次）。均值稳定在 0.93→0.99s，
即 verify 没变慢，是候选变多自然要多核验。**现在轮到 B2 了** —— 但先后顺序不能反：
先 B1 把 LLM 从 72% 压到 43% 是对的，若按 F-4 原建议先上 B2，那 90s 的 LLM 仍会顶死预算。

---

#### F-8 · 预算耗尽被谎报成 provider_failed（B1 转默认后放大 10 倍，已修）

转默认后的第一次**纯默认**跑（不带任何 env 覆盖）反而比 A/B 臂差：
`stop_reason` 退回 `budget_exhausted`、入库可用 10（A/B 臂 17）。
处置分布里有 `relevance:provider_failed` **×10**，而同一轮
`relevance_llm_calls=0` —— 一次 LLM 都没发出，却报「模型失败」。

链路（三层各自都"合理"，叠起来变成谎报）：

```
预算耗尽 → BudgetAwareLLMProvider.complete 抛 ExternalCallRejected
        → structured_call 的宽 except Exception 压成 PROVIDER_FAILED
        → _assess_once 把「整窗」batch_size 条全判 ERROR
```

**爆炸半径随 batch_size 放大**：单篇路径下预算停止只损失 1 条，size=10 时
损失 10 条。这正是上一轮当作"假设风险"提过的 size=10 降级半径 —— 它是真的，
且由 B1 转默认引入。ERROR 不入 `relevance_store`（`_remember_success` 只收
SUCCESS），故无缓存污染，复跑会重判。

**修法**：`_flush` 补窗前预算检查（与 `_judge_b2` 第 633 行对齐），未判定候选
显式落 `relevance:budget_exhausted`。注意**不能只 break** —— `pending.clear()`
会把未判定候选整批丢掉，连 disposition 都不产生，`candidates` 计数与处置分布
对不上，比理由写错更难查。

真机复验（纯默认）：`provider_failed` 归零、`stop_reason=quota_satisfied`、
入库可用 10→16。回归测试注入验证（`cp` 备份而非 `git checkout`）确认变红。

**教训**：宽 `except Exception` 会把**控制流信号**（预算/取消/超时）和**真实故障**
折叠成同一个状态。批量化放大了后果：一条错误的状态归因现在污染整窗。
诊断字段自相矛盾（`llm_calls=0` 却报 `provider_failed`）是这类 bug 的指纹 ——
真机跑要交叉核对计数与理由，不能只看理由分布。

---

#### F-9 · B2 首轮 A/B：verify 降 45% 但 LLM 升 24%，未达成配额（待复测）

同预算（120s）下 B2 开/关对比：

| | B2 关 | B2 开 |
|---|---|---|
| verify | 53 次 / 54.7s | **29 次 / 30.4s** |
| LLM | 67.8s | **83.9s** |
| 入库可用 | **16** | 13 |
| stop_reason | **quota_satisfied** | budget_exhausted |

verify 收益如期兑现（-24.3s，-45%），但被 LLM 的 +16.1s 吃掉大半，净收益仅
约 8s，且配额未达成。两个原因，都说明**这组数据还不能定论**：

1. **两臂都撞了 120s 天花板**，比较的是"预算内做了多少"而非"做完要多久"，
   净收益被预算截断。
2. B2 开臂出现 `verify:budget_exhausted` **×6** —— 这 6 条**已经付了 LLM 判定
   成本**才死在 verify 上。B2 把便宜的操作（verify 1s）挪到贵的（LLM 14s）之后，
   预算耗尽时浪费的是更贵的那一半。这是重排的固有代价，不是 bug。

**下一步**：放宽预算到 300s 让两臂都跑到 `quota_satisfied`，比较"达成同一配额
各花多少秒"。**当前被 OpenAlex 日配额挡住 —— 见 F-10，须等 UTC 午夜重置后复测。**
在此之前 B2 **保持默认关闭**。

**教训**：预算封顶时的 A/B 只能比较吞吐、不能比较总成本。要论证"某优化省了多少秒"，
必须让两臂都跑到同一个终止条件（这里是 `quota_satisfied`），否则天花板会同时
压住两边、把差异吸收掉。

---

#### F-10 · OpenAlex 已改信用点计费：日额度 ≈100 次请求，429 不该重试（已修）

复测 B2 时连续拿到 429，我起初按"短时速率窗口"处理，探测了 25 分钟仍失败。
**裸 curl 同样 429**（排除了我们 provider 的实现问题），读响应体才看清性质：

```json
{"error":"Rate limit exceeded",
 "message":"Insufficient budget. This request costs $0.001 but you only have $0
            remaining. Resets at midnight UTC.",
 "retryAfter":55256, "dailyRemainingUsd":0, "creditsRemaining":0}
```

不是速率限流，是**每日额度耗尽**：`X-RateLimit-Limit-USD: 0.1`、单请求
`$0.001` ⇒ **每天约 100 次请求**；`Retry-After: 55256` ≈ **15.3 小时**，UTC 午夜重置。
OpenAlex 已转向信用点计费模型，`OPENALEX_MAILTO` polite pool 不再豁免。

**连带发现一处真实缺陷**（`http_util.get_with_retry`）：429 被无条件当作可重试
瞬时故障，退避 1.5/3/6s 重试 3 次。配额耗尽时这 3 次注定失败，后果是
**每次重试都白烧一个额度**（本就只有 100 个/天），还把"要等 15 小时"的信息丢掉 ——
错误信息只剩「已重试 3 次」，于是我按短时限流反复探测，浪费了约 25 分钟。

**修法**：以 `Retry-After` 是否超过 `_RETRY_AFTER_CEILING_S`（30s）区分两类 429。
超过即判配额耗尽，不重试、并把等待时长写进错误信息；小于则保持原有退避
（Semantic Scholar 匿名访问那类秒级窗口仍需重试）。无 `Retry-After` 时无法判定，
保持重试（宁可多试）。注入验证：原实现发 4 次请求，修复后 1 次。

**教训**：429 有两种性质完全不同的成因 —— 短时速率窗口（重试有用）和配额/额度耗尽
（重试必然失败且有害）。把它们当同一件事，重试就从"提升稳定性"变成"加速耗尽额度"。
**读响应体和 `Retry-After`，不要只看状态码**：状态码相同而正确动作相反。
真机 A/B 要把"源的日配额"当成有限资源排进实验预算（约 100 次请求/天，
一次 dogfood 跑掉 30~50 次 ⇒ 每天只够 2~3 跑）。

---

### P-1（原始记录：跑之前的状态与理由）

**现状**：`tests/eval/literature_dogfood/last_run.txt` 停在
```
[1] 初始缺口: [('total', 20), ('language.zh', 5), ('language.en', 15)]
[2] 检索中（打真实源 + reviewer 判相关性）…
```
后面没有了 —— 那次真实 dogfood 没跑完。

**装配已证明可用**：`scripts/_wiring_check.py` 全 mock 下 25/25 通过，
`build_writing_orchestrator` / `literature_port` / `classifier` / `search_synthesizer` /
`retrieval_limits` 全部成功注入；`orchestrator._limits` 存在；
mock LLM 下 analyze 失败走 `PlanningExit(NEEDS_INPUT)`、`run_status=completed`，
未崩、未伪装成功。**电路是通的，只是从未接过真实负载。**

**目的**：不是"证明能用"，是把四个盲区变成有数字的问题：

| 现在的盲区 | 跑完之后 |
|---|---|
| 不知道 `[2] 检索中…` 之后发生什么 | 知道挂在哪、为什么 |
| retrieval-throughput 在优化无基线的链路 | 有 Legacy 基线，加速比才有意义 |
| B1 人工标注 0/40，不知该标什么 | 从真实失败样本里长出标注集 |
| 删旧栈无数据 | 配合 D-1 的 trace 得到分歧统计 |

**配置**（不要测默认配置——默认测的是你打算删的那套）：
```
PAPER_INTENT_PLANNING_MODE=active
PAPER_LIT_WRITING_INTEGRATION=true
PAPER_LIT_SEARCH_SYNTHESIS=true
PAPER_TRACE=true
```
配额先砍到最小（`total≥5`、只英文、2 个 seed），**先求跑完，不求跑好**。

**前置**：B-4（否则数据不可信）。B-1/B-2/B-3 可用临时手法兜。

**动作**：
- [ ] 最小配额 dogfood 脚本（trace 落盘 + 分段计时）
- [ ] 跑一次，记录断点与耗时分布

---

### P-2 · 记忆系统 —— 80% 已存在，但有一个真缺口

**已存在的部分**（不要重建）：

| 你的描述 | 现有实现 |
|---|---|
| 通过 gate 的文献格式化保存 | `ReferenceLedger` + `facts.json` schema_version=4 的 `references` 段 |
| 方便追溯 | `verification_events` 事件溯源 + `dedup_suggestions` + `integrity_report()` |
| 组装成上下文交给 LLM | `evidence.build_bundle()` + `context_builder.build_evidence_block()` |

而且现有实现比"格式化保存"强得多：五套索引（work key / bibkey / alias / numeric / merged_from）、
`work_identifiers` 精确去重、`resolve_token` 命中多条时返回 AMBIGUOUS **绝不任选也不谎报**、
bibkey + numeric_alias 双标记体系、`resolvable_*` vs `citable_*` 两个引用集合。

**真缺口**：**没有"哪一章实际引用了哪篇文献"的持久化映射。**

- Ledger 存的是"我知道哪些文献"（known / resolvable / citable）
- 正文实际引用靠每次 regex 扫（`diff_gate._cite_tokens`），**不持久化、不按章节归属**
- `ReferenceEntry.citation_aliases` 语义是"导入源文档使用的引用标记"，不是这个

这个缺口正好是 B-2 的根因：**有了这个索引，commit 时写 bibitem 就是查表操作。**

**建议**：不叫"记忆系统"，叫 `CitationUsage` —— 一个 `(reference_id, section_id)` 映射，
约 150 行。不要建新子系统。

**顺序**：先跑 P-1，看真实的 `facts.json` 长什么样，再决定要不要改 Ledger。
你还没见过一次真实检索入库的结果。

#### 已完成（2026-08-12）：`citation_usage.py`

按上面的建议做了，没建新子系统：`src/paper_agent/literature/citation_usage.py`
（约 160 行），复用既有 `SectionIndex` 分章 + `Ledger.resolve_token` 解析，
落在 facts.json envelope 的 `citation_usage` 段。旧 envelope 无此段 → 空表，向后兼容。

- `SessionFacts.refresh_citation_usage(text, ext)` 整体重算（不增量）。增量维护要求
  每个正文写入路径都记得更新，那正是 `citation_aliases` 语义漂移的成因；全量重算
  对万字级文档的开销可忽略，换来"表与正文必然一致"。
- 解析失败**留痕不静默丢**：NOT_FOUND / AMBIGUOUS 记入 `unresolved`。把"引了一篇
  我不认识的文献"记成"本章没引"，会让配额判定和 bibliography 都建在假数据上。
  AMBIGUOUS 照 `resolve_token` 的纪律绝不任选。
- 分不出章节的格式归到伪章节 `_document`，不返回空表 —— 否则 `all_cited_ids()`
  会把"我分不出章节"谎报成"这篇没有引用"。

顺带把它接进了 `:ask` 只读问答：新工具 `section_citations` 回答"引言引了哪些文献"
"某篇在哪几章被引"，此前这类问题只能靠模型读正文数方括号（不准且贵）。
`capture(session, facts)` 是只读的 —— 不写回 `citation_usage`、不 save，
`:ask` 保持零副作用（有测试钉住）。没传 facts 时报"无法回答"而非"没有引用"。

测试：`tests/test_citation_usage.py`（9 条）、`tests/test_ask_section_citations.py`（10 条）。

**B-2 的下一步**：commit 时写 bibitem 现在可以是查表操作 —— `sections_citing(rid)`
与 `all_cited_ids()` 已就位。还没接进 `sync_bibliography`，那是独立的一步。

### P-2a · `trace.json` 不能当记忆用（结构性原因，比隐私理由更硬）

一度考虑"关掉脱敏后把 trace 当记忆"。**不可行，而且主因不是隐私。**

`recorder.py` 自己的 docstring：

> **容忍残缺/乱序 Span（关键）**：观测错误不拖垮业务 → 可能丢事件。

> **v1 不做**：Event_Journal / durability / fsync

具体表现：
- `FanOutSink` 隔离下游失败 → 事件可被**静默丢弃**
- `_merge_attrs` 首写为准，重复写只记 warning 然后**扔掉**
- 只有 START 无 END → `state=None`；只有 END 无 START → orphan span
- `LLM_DELTA` / `LLM_THINKING` **原文根本不落盘**（`SENSITIVE_EPHEMERAL_KINDS` 直接 return），
  与脱敏无关——流式输出的正文在 trace 里从来不存在

**两者需求相反**：

| | trace | 记忆 |
|---|---|---|
| 丢数据 | 可接受（保业务优先） | 不可接受 |
| 组织 | 每 run 一棵 span 树，`runs/<run_id>/trace.json` | 跨 run 累积、可查询 |
| durability | 明确不做 | 必须有 |

把 trace 当记忆等于把"为保业务可以丢事件"和"记忆不能丢"两个矛盾契约塞进一个文件。
**关掉脱敏也不行——结构就不对。**

### P-2b · 记忆应走文件路线，不是向量库

Claude Code 的记忆载体是文件：`CLAUDE.md`、todo list、`claude-progress.txt` + git history。
官方称之为 **structured note-taking / agentic memory**——agent 定期把笔记写到 context
之外，之后拉回来。生态里另一条路线是 MemGPT/Letta 的分层记忆（core/archival/recall
+ 自主记忆操作 + 递归摘要），以及 Mem0 / Zep / Cognee 等独立记忆层。

**本项目应走文件路线**，理由：
- session 绑定一篇论文，作用域天然小（不是"记住用户三年偏好"）
- `facts.claims` 已经是 progress 文件的结构化版本
- 已有 git（`FileBackedSession` 的 baseline / commit）
- 向量库会引入 embedding 版本、召回质量、chunk 策略——全是新的不确定性，
  而当前最不需要的就是新的不确定性

**注意**：若记忆存原文，仍要跑 `sanitize.redact()`（防用户在对话里粘了 API key），
且确认 `.paper_workspace/` 在 `.gitignore` 内（已确认）。

---

### P-3 · 上下文工程 —— 骨架已有，真问题是预算不统一

**你的判断是对的**："记忆系统只是上下文工程的附属模块"。
而且你的代码**已经这样组织了**：`evidence.py` 从 ledger 读 → `context_builder` 渲染成 block。

**已存在**：三意图 message 构造、硬约束 system prompt、违规回灌重试、
证据数据块（`wrap_as_data` 防注入）、预算截断、
`EvidenceSpan` offset 精确到 NFC 码位、
`capability` 区分 `DISCOVERY_ONLY`/`CLAIM_SUPPORT`、
无摘要条目明确禁止据标题推结论。

**真问题**：见 D-4，两套预算体系。以及只有字符计量、没有 token 计量。

### P-3a · 新增假设：`build_bundle` 最后一步"补满预算"可能是负收益

[Anthropic 的 context engineering 文章](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
指出 context rot（[Chroma 研究](https://research.trychroma.com/context-rot)）：
输入 token 数增长时，模型准确召回的能力下降，所有主流模型都有此特性。
故上下文是**边际收益递减**的资源，指导原则是"最小高信号 token 集合"。

而 `evidence.build_bundle()` 的顺序是：
筛 → 固定原有引用 → 保 zh/en 下限 → 补 total → **剩余预算补高相关**。

最后那步在**尽量填满 8000 字符预算**。若 context rot 成立，这一步可能让生成变差。

**可验证的假设**：把 20 篇文献摘要全塞进 prompt，可能比精选 8 篇更差。

**这比 D-4 更有价值**：D-4 是让预算统一，这条是**质疑预算该不该用满**。
E2E 之后可直接做 A/B（同一任务，补满 vs 只保下限），成本低、结论明确。

#### P-3a 三臂 A/B 结果：假设未成立，但发现了一个更值钱的取舍（2026-08-12）

真机跑过（25 条语料、预算固定 24000 字符、`--generate` 已发请求）。
脚本 `tests/eval/literature_dogfood/ab_context_budget.py`，
报告 `ab_context_budget.25rec.txt`，三臂正文 `ab_gen_{A_full,B_trunc1000,C_trunc700}.md`。
**这些产物都被 `.gitignore:25` 挡住不入库，所以结论必须记在这里**，否则下次会重复付费。

| 臂 | 摘要档 | 选入 | 占用 | 引用率 | 带出处的具体数据 |
|---|---|---|---|---|---|
| A_full | 全摘要 | 15 篇 | 23821（99%） | 15/15 | **12 个百分比** |
| B_trunc1000 | 截 1000 | 23 篇 | 23375（97%） | 23/23 | 0 |
| C_trunc700 | 截 700 | 25 篇 | 17940（74%） | 25/25 | 0 |

**1. P-3a 的假设（补满 → 浪费）未观察到。** 三臂引用率全是 100% —— 给多少篇模型就引
多少篇，不存在「塞进去没被引用」的篇目。按 P-3a 原本的判据（未被引用即浪费），
补满的浪费为零。

**2. 真正的代价在另一个维度：截摘要会让正文丢掉具体数据。**
A 臂产出 12 个有摘要出处的百分比，B/C 两臂都是 0，行文停留在定性描述。
摘要后半段被当作「多为实验细节、价值低」而截掉 —— 但那里正是**可引用的量化结果**，
而具体数据恰是学术写作的核心价值。这个损失比篇数少几篇严重得多。

**3. 由此得到的策略**：截断应是**最后手段**，不是扩大覆盖面的默认手段。
优先级为 全摘要少篇 > 截摘要多篇。`_ABSTRACT_LIMIT_LADDER` 现在的行为
（装不下就逐档截）方向是对的（比旧实现直接判 INSUFFICIENT 好），
但不应为了多塞篇数而主动降档。

**结论强度的两点保留**（避免把它当成已定论）：
- A 臂原按 total=20 装不下（需 30645 > 24000），是下探到 15 篇才参与对比的
- 100% 引用率本身可疑：读 C 臂正文，25 篇呈现为一句一个标记的罗列式行文，
  更像堆砌而非每篇真的必要。「引用率 100%」不等于「每篇都有价值」，
  这个判据本身偏弱，后续要换成人工判行文质量

**未完成**：25 条那次没落 `bundle_*.jsonl` 逐条明细，floor/topup 划分事后无法复原。
现已补上（两个脚本都写），但盘上的 `bundle_*.jsonl` 是 13 条语料那次的，
两者不可混用。

**一个踩过的坑**：`ab_context_budget.txt` 固定文件名，被一次 13 条语料的离线重跑
覆盖掉了 25 条那次的付费结果（内容已复原）。现在输出按语料规模分文件
（`ab_context_budget.<N>rec.txt`），且离线跑不许覆盖含真机结果的报告。
另外 13 条语料时三臂产出**完全相同**（全摘要就装得下、阶梯不触发），
脚本现在会显式警告「本次不构成 A/B」—— 否则会被误读成「三臂无差异」。

### P-3b · agent loop 与写作意图应采用**不同**的组装策略（两者不矛盾）

| | 组装策略 | 为什么 |
|---|---|---|
| polish / rewrite / add_section | **预加载**（`original` 全文进 prompt） | 要改那段就必须给那段，没有 JIT 空间 |
| agent loop | **just-in-time**（轻量索引 + 按需工具） | 开放问题事前不知道需要哪部分 |

官方文章的核心转向正是从预加载到 JIT：维护轻量标识（文件路径、查询、链接），
运行时用工具动态加载；Claude Code 用 `CLAUDE.md` 前置 + `glob`/`grep` 按需取，
绕开索引过期问题。

**本项目的"轻量索引"已经存在**：`SectionIndex.list()` 返回 `(id, title, level)`，不含正文。
agent loop 第一版工具集：

```
list_sections()        → 轻量，几十 token
read_section(id)       → 按需取正文
list_references()      → Ledger 摘要
get_section_intent(id) → facts.claims
```

**风险警告**："上下文工程"这个词很容易长成一个无边界的框架项目。
必须给它一个**可测的目标**（例如"同等质量下 token 降 X%"），
否则会做成第二个 retrieval-throughput：34 条 property、12 个 task、永远不发布。

**建议**：定位为**收敛 D-4**，不是新建系统。
顺序：先跑 P-1 看一次真实渲染结果 —— 可能已经够用，也可能会发现 8000 字符远远不够，
但这只能靠看真实输出知道。

---

### P-4 · 顶层 Agent Loop —— 三个提案里最该做的，但必须只读

**需求成立**。当前 UNCLEAR 出口是：

```python
return RouteResult(intent=Intent.UNCLEAR,
    note="支持：polish（润色）、rewrite（改写）、add_section（新增章节）、...")
```

用户问"缺哪些章节"，得到一句功能列表。体验很差。

**关键洞察：你举的例子全部是只读问答。**

- "告诉我当前论文存在哪些章节，以及缺少哪些"
- "告诉我当前章节哪块写得好，哪块不好，好在哪里"
- "告诉我引言这章你是怎么规划的，采用什么叙述流程"

**这些问题都不需要写权限。**

**所以第一版必须是只读的**：能读文档 / Ledger / claims / trace，
**不能调 `execute_*`、不能碰 working copy**。

只读的风险面极小：没有 Gate 需要保护（因为不写）、没有回滚需要考虑、
失败最坏是答错。这与现有五意图（全是写操作、每个都有 Gate）是**完全不同的风险等级**。

**红线**：绝不让 agent loop 直接写文档。
它若要写，必须产出 `Task_Plan` 交给现有 `plan_executor` 走 Gate。
否则你会绕过整个 Gate 体系 —— 那是这个项目最有价值的部分。

**可并行**：只读、新增、不碰现有路径，不依赖 P-1。

> **状态：已实施（2026-08-12），默认 `on`。** 见 `.kiro/specs/agent-loop/plan.md`。
>
> 落地形态与上面这段设想的三处差别：
> - **工具是 5 个，不是这里列的 4 个那套**。`list_references` / `get_section_intent`
>   都没做：真正被问的是"哪一章引了哪几篇"（→ `section_citations`），不是题录列表；
>   `claims` 真机是空的（X-10 未修项 B），提供恒返回"没有记录"的工具只会诱导模型去调。
>   计划外补了 `edit_history` —— 真机 25 问里历史类 3 问全答不上，而数据一直在
>   session 的 git commit 里，缺的只是读取通路。
> - 上面举的**第三个例子答不了**（"引言这章你是怎么规划的"），卡在 claims 没数据，
>   不在循环。前两个能答。
> - **红线守住了，且是结构性的**：`AskContext` 是 frozen 快照、**不持有 session**，
>   工具闭包里根本没有 `write_working` 可调 —— 比"约定工具不写"强。
>   `tests/test_ask_loop.py` 把写入面做成会记账的假实现并断言零调用。

---

## 四、元风险（记录用）

三个新提案有一个共同模式：**都是"再建一个系统"**。

而这个项目当前的核心问题恰恰是**系统很多、通电的少**：
3.7 万行代码、1057 个测试、十个默认关闭的 flag、**零次成功的真实端到端运行**。

在没有反馈信号的情况下增加第四、第五、第六个子系统，不会改善这个状况。
瓶颈不是能力不足，是**能力未验证**。

**原计划顺序**（B-4 + P-1 已完成）：

```
B-4 ──→ P-1（真实 E2E）──→ 看 facts.json / 渲染结果 ──→ 决定 P-2 / P-3 要不要做
                                    │
P-4（只读 agent loop）可并行 ────────┘
```

---

## 六、P-1 之后的重排（依据真实数据，2026-08-01）

E2E 跑通后，优先级发生了实质变化。**新的第一优先级是检索召回质量（F-1），
不是吞吐、不是记忆、不是上下文工程。**

| 优先级 | 事项 | 依据 |
|---|---|---|
| ~~1~~ | ~~检索召回质量（F-1）~~ **已修** | 真因是 `search=` 词袋语法非查询词粒度；短语优先已上，受控 A/B `unrelated` 12→5。见 F-3b |
| ~~1~~ | ~~时间预算与单候选成本（F-5）~~ **已解** | B1 默认开启后 LLM 秒/候选 4.50→2.15，`iterations=3`、`stop_reason=quota_satisfied`。未动预算值。见 F-7 |
| ~~3~~ | ~~重估 B1 vs B2 优先级（F-4）~~ **已定** | 归因否定了 F-4：verify 只占 25%（31.7s），relevance LLM 占 72%（89.9s）。B1 先上、已默认。见 F-7 |
| **1** | B2 管线重排：**复测待做**（首轮不定论） | 首轮 A/B：verify -45%（54.7→30.4s）但 LLM +24%（67.8→83.9s），净约 -8s，入库 16→13。两臂都撞 120s 天花板 ⇒ 只比了吞吐、没比总成本。需放宽到 300s 让两臂都 `quota_satisfied` 后复测；被 OpenAlex 429 打断。B2 暂保持默认关。见 F-9 |
| ~~2~~ | ~~预算耗尽误报 provider_failed（F-8）~~ **已修** | B1 转默认后放大：整窗 10 条被谎报模型失败（`llm_calls=0` 却 `provider_failed`×10）。`_flush` 补窗前检查 + 显式落 `budget_exhausted`。入库 10→16。见 F-8 |
| **2** | `accept_weakly_relevant` 按 section_purpose 分场景（F-3） | 10 个 weakly 被拒，其中含引言必引的综述（注：dogfood 已见 `mode=by_purpose` 生效，需重新确认此条是否仍成立） |
| **3** | 批量判定的规模上限与降级行为 | 当前 `size=10` 未经调优；`retry_limit=1` 下整批 schema 失败会让**整窗**判 ERROR。真机尚未观察到，但 size 越大爆炸半径越大 |
| 5 | B-2 bibliography（受 F-6 上限约束） | 元数据缺 container_title/volume/pages |
| 6 | P-4 只读 agent loop | 不依赖上述，可并行 |
| 7 | P-2 / P-3（记忆 / 上下文工程） | facts.json 结构已验证可用；暂无证据表明需要改造 |

**降级的判断**：
- ~~**B1（批量相关性）从"进行中的重点"降级**~~ —— ~~它优化的是 63s 里的一部分，
  而 F-1 才是决定产出的因素。B1 代码已在（flag 关），不必回滚，但不该继续投入。~~
  → **此判断已推翻（2026-08-06）**：B1 优化的不是"一部分"，而是 72% 的大头。
  「先修 F-1 再优化吞吐」的**顺序**是对的，但对 B1 收益量级的估计错了。
  已默认开启，见 F-7。教训同 F-4：估算收益占比时必须落到秒。
- **P-2 记忆系统进一步确认不必新建** —— `facts.json` v4 落盘结构完整、
  `relevance_store` 缓存有效、幂等键正常。唯一缺口仍是 `CitationUsage`（哪章引了哪篇）。
  → **该缺口已补（2026-08-12）**：`literature/citation_usage.py`，落在 facts envelope
  的 `citation_usage` 段，并接进 `:ask` 的 `section_citations` 工具。见 P-2 小节。
- **P-3 上下文工程尚无数据支撑** —— 本次只有 1 篇入库，`build_bundle` 的
  "补满预算"路径（P-3a 质疑的那步）根本没触发。要验证 context rot 假设，
  需要先有 ≥10 篇的证据集，即先解决 F-1。
  → **F-1 已修后此前提解除**：非受控跑已见入库 10 篇（`quota_satisfied`）。
  P-3a 质疑的那步现在**够条件跑了**，但 runner 仍在 `[OK] 配额可满足` 就结束、
  没调 `build_bundle`。要验 context rot，下一步是让 runner 跑完 BUILD_BUNDLE。
  → **已跑完（2026-08-12）**：三臂 A/B 完成，P-3a 假设未成立（引用率三臂皆 100%、
  无浪费），但发现截摘要会让正文丢掉全部量化数据（12 个百分比 → 0）。
  见上面「P-3a 三臂 A/B 结果」。上下文工程这块**已有数据**，不再是"无支撑"。

---

## 四·补 · 已修 P0：工具 schema 缺 `type` 导致 OpenAI 端点全线失败

**发现时机**：为 E2E 挑 reviewer 模型时，切到 OpenAI 兼容网关就发现相关性判定
全部 `provider_failed`。**这是"先跑 E2E"这个决策的第一笔回报——bug 在真正开跑前就暴露了。**

**根因**：两个协议适配器不对称。

```python
# AnthropicProvider._to_anthropic_tools —— 只读 function 子字典，忽略顶层 type
fn = t.get("function", t)

# OpenAICompatibleProvider（修复前）—— 原样透传，OpenAI 协议要求每项带 type
call_opts["tools"] = tools
```

**故障链（这才是它难定位的原因）**：

```
schema 缺 "type": "function"
  → OpenAI 端点返回 invalid_request_error
  → structured_call._looks_unsupported() 的 _UNSUPPORTED_HINTS 含 "invalid_request_error"
  → 误判为「provider 不支持 forced tool」→ capability 降级 + 重试（第 2 次物理调用）
  → 仍失败 → RelevanceAssessment(status=ERROR, error_kind="provider_failed")
```

外部症状：**每次判定 2 次物理调用后仍失败，真实错误被吞掉。**
实测第一次 `calls=2`、后续 `calls=1`（capability 状态已记住降级），成本翻倍且全错。

**受影响 schema**（4 个缺，2 个本来就对）：

| 文件 | 修复前 |
|---|---|
| `literature/relevance.py::_TOOL` | 缺（**核心链路**） |
| `literature/relevance_batch.py::_BATCH_TOOL` | 缺（B1） |
| `literature/retrieval/query_planner.py::_TOOL` | 缺 |
| `tasking/query_synthesizer.py::_TOOL` | 缺 |
| `tasking/operation_analyzer.py::_ANALYZE_TOOL` | 有 |
| `tasking/extractors/base.py::build_tool_schema` | 有 |

**为什么 1073 个测试没抓到**：`MockLLMProvider.complete` 无条件吸收 `tools`、
**从不校验形状**。mock 太宽松 → 协议层的错在离线测试里不可见。

**修复**（两处，主修在 provider）：
- [x] `openai_compatible._normalize_tools()`：统一补齐 `type`，兼容三种入参形态
  （已合规 / 缺 type / 裸 function 定义）。符合项目自己的分层纪律
  「协议差异在 provider 内消化」，且让未来新 schema 无法再引入此 bug。
- [x] 四个 schema 补上 `"type": "function"`（约定一致性；provider 归一化是兜底不是借口）
- [x] `tests/test_tool_schema_protocol.py`（18 项）：**遍历项目全部工具 schema**，
      断言每个都是 OpenAI 协议合法形状、且能被两个适配器同时正确消化。
      新增 schema 漏了 type 会立刻红。

**验证**：全量 **1091 passed**（1073 + 18），无回归。
真机复测三个候选 reviewer 均 `status=SUCCESS`、判定 3/3 正确、`1.00 calls/judge`（无兼容开销）。

**遗留**：`MockLLMProvider` 仍不校验 tool schema 形状。可考虑给它加一个
「严格模式」在测试里校验协议合法性，作为第二道防线。

---

## 五、已产出的工具

- `scripts/_wiring_check.py` —— active 模式装配冒烟（全 mock、零网络、数秒完成）。
  能在改装配时立刻发现"某个注入漏了"或"某个意图不可达"。
  **待决定**：改名 `scripts/wiring_check.py` 正式收进来，或挪进 `tests/` 成为测试用例，或删除。

---

## 八、真实骨架 rewrite 预检（2026-08-01，P-1 的下一跳）

**对象**：用户真实小论文骨架（`骨架.docx` 经 pandoc 3.10 转 →
`examples/airground_skeleton.md`，10166 字节）。目标：写引言，配额 total≥20 / en≥15 / zh≥5。

**意图判定**：`rewrite`（不是 `add_section`）。骨架的「1 引言」已存在且含 4 个子节概述，
`interview_add_section` 会因同名章节已存在而拒。用户的判断正确。

### 预检结果（`scripts/_skeleton_check.py`，确定性、零 LLM）

```
find('引言')      -> ('1_引言', '1 引言', level=2)      ✓ 宽松包含匹配命中
find('1 引言')    -> 同上                                ✓
find('introduction') -> None                             ← 全中文文档，英文 anchor 无效
target_level = 2          -> 候选正文出现 level<=2（# / ##）即判越界
original_paragraphs = 8   -> RewriteGate 允许区间 [6, 13]
original_chars = 497
original_citation_tokens(md) = ()        ← 全文零引用标记
original_data_numbers = ['1','2','3','4']  ← 来自 ### （1）～（4）
intro body 内标题层级 = [3, 3, 3, 3]
```

### 由预检得出的四个可预测风险

| # | 约束 | 风险 | 处置 |
|---|---|---|---|
| 1 | 段落数 ∈ [6, 13] | 保留 4 个 `###` 标题后只剩 ≤9 段容纳 20 篇引用（≈2.2 篇/段）。若 LLM 去掉子标题写成 4 段 → 低于下限 6 被拒 | 输入里明确要求「保留原 4 个 `###` 子标题结构」；违规会回灌重试（`polish_max_retry`，默认 2） |
| 2 | 候选正文不得含 level ≤ 2 标题 | LLM 输出 markdown 时习惯用 `##` | 同上，回灌可修 |
| 3 | 新数字须已知或用户确认 | 已知只有 `1,2,3,4`。LLM 写「约 90°」「5 个子节」等会触发逐个确认交互 | 预期行为，交互时按真实性回答即可 |
| 4 | 引用须全在 `facts.ref_ids()` | `.md` 路径下 = `resolvable_numeric_aliases()`，即 `[1] [2] …`；原文零引用 ⇒ 20 篇必须全部检索入库并进 bundle | 依赖检索，见下方预算 |

### 关键接线确认（避免重演 F-3 那种静默失效）

- `handlers.py:_prepare_citation_quota(..., criteria_topic=plan.section_id)`
  → `WritingEvidenceRequest.section_kind = plan.section_id`。
  `section_id` 会是「引言」或「1_引言」，两者都含 `"引言"`，
  而 `"引言"` 在 `relevance._WEAKLY_SIGNALS` 内 ⇒ `by_purpose` 模式**会**命中 WEAKLY ✓
- 同一个 `criteria_topic` 也是 `RelevanceCriteria(topic=...)`。
  **不开 `SEARCH_SYNTHESIS` 时 topic 就只有「引言」两个字**，相关性判定失去课题信息。
  ⇒ 本次必须开 `PAPER_LIT_SEARCH_SYNTHESIS=true`，走 `_synthesize_intent`，
  由 `purpose`/`direction`（用户输入原话）+ `facts.claims` 合成检索意图。
  **推论：用户输入里必须带完整课题名**，否则合成出的意图是空的。
- 两套预算**互不相干**（这次是好事）：`planning_max_llm_calls=20` 只作用于
  `tasking/budget.py` 的理解链路；文献检索走 `literature/retrieval/budget.py::BudgetLedger`。
  grep 确认 `planning_max_llm_calls` 只出现在 `config.py` + `tasking/budget.py`。

### 预算测算（按 P-1 实测外推，默认值全部不够）

实测锚点（F-3 复测那次）：20 候选 / 123.6s / verify 34 / llm 17，
by_purpose 通过率 9/17 ≈ 53%。要入库 20 篇 ⇒ 约 **40–50 个候选**。

| 变量 | 默认 | 需要 | 理由 |
|---|---|---|---|
| `PAPER_LIT_RETRIEVAL_TIME_BUDGET` | 120 | **1200** | 50 候选 × ~6s ≈ 300s，加 zh 多轮 |
| `PAPER_LIT_RETRIEVAL_MAX_LLM_CALLS` | 30 | **150** | 50 候选 ≈ 50 次判定 + planner/synthesizer；**默认会先爆** |
| `PAPER_LIT_RETRIEVAL_MAX_VERIFY_CALLS` | 120 | **400** | ~1.9 次/候选 × 50 ≈ 95，留余量 |
| `PAPER_LIT_RETRIEVAL_MAX_SEARCH_CALLS` | 30 | **80** | 多轮 × 中英双语 seeds |
| `PAPER_LIT_RETRIEVAL_MAX_ITERS` | 6 | **12** | 填 zh 缺口需要多轮逼近 |

### 已知高风险：zh≥5 很可能填不满

OpenAlex 中文覆盖差，且 `literature_lang_policy=high_only` 下 medium/low 置信语言判定
**不计入**语言配额。预期走 `NEEDS_GAP_DECISION` 询问是否接受偏差。
这不是 bug，是设计（F-5 那条未完成的 dogfood 正是 `zh≥5` 卡住的）。
**顺带价值**：这会第一次跑到 `NEEDS_GAP_DECISION` 与 `build_bundle`
——即 P-3a 质疑的「补满预算」那步，此前从未触达。

**探针**：`scripts/_skeleton_check.py`（一次性，跑完已删）。

---

## 九、真机 rewrite 首次尝试：两个连环 bug（2026-08-01，均已修）

用户拿真实骨架跑「写引言」，连撞两次 `section_not_found`。两次的表象一样、根因完全不同。
共同点：**诊断信息把根因说错了**，把用户引向错误的排查方向（两次都问「这是什么 bug」）。

### X-5 · docx 载入后章节类意图全不可用，却报「找不到章节」

**复现**：`python scripts/chat.py "骨架.docx"` → 请求改写引言 →
`rejected(section_not_found: 计划期找不到要修改的章节：引言部分)`

**根因**：`SectionIndex` 只有 `.tex`（`\section{}`）和 `.md`（`#`）两个解析器。
docx 是 zip 二进制，解析不出标题 → `chat.py::_build_index` 返回 `SectionIndex("", "docx")`
空索引 → `plan_validator` 的投影集合为空 → rewrite/polish/add_section 必然被拒。

**为什么诊断是错的**：报的是「找不到**引言部分**」，暗示换个章节名可能有救。
真相是这个格式下**任何**章节都找不到，与输入的章节名无关。
系统自己知道这件事（`_TEXT_EXT` 那个集合就是干这个的），却选择不说。

**修复**：
- [x] `section_index.py` 新增 `SECTIONABLE_EXTS` + `supports_sections()` —— 唯一真相源。
      `chat.py` 原本硬编码了同一份集合（第二真相源），改为复用
- [x] `SectionIndex.supports_sections` property：区分「格式不支持」与
      「格式支持但文档确实无标题」两种空 `sections`
- [x] `plan_validator` 新增 `FORMAT_LACKS_SECTIONS`，**先于**章节存在性检查，
      命中后 `continue` 不再叠加 `section_not_found` 等次生诊断。
      消息给出出路（转 .md/.tex，且转换须单独一轮）
- [x] `chat.py::_warn_if_no_sections`：**载入时**就说清不可用的操作 + 给 pandoc 命令
- [x] `tests/test_format_lacks_sections.py` 24 项。含反向守护：docx 上
      `format_docx` / `convert_format` 是正当操作，不得被这条检查误伤
      （`convert_format` 正是 docx 用户的出路）

### X-6 · 章节编号前缀让「引言部分」匹配不上「1 引言」

**复现**（格式换成 `.md`、15 个章节全部正常解析之后）：
输入「我现在需要去写我的引言部分」→ LLM 填 `section_id="引言部分"` →
同样的 `section_not_found`。

**根因**，用真实值走一遍：

```
turn_service 传的 current_sections = ids + titles
    _norm("1_引言") = slugify("1_引言") = "1_引言"
    _norm("1 引言") = slugify("1 引言") = "1_引言"    两者同归一
LLM 填的 anchor
    _norm("引言部分") = "引言部分"

_matches 的双向包含：
    "引言部分" in "1_引言"  → False
    "1_引言" in "引言部分"  → False
```

公共部分是「引言」，但**两边都不完整包含对方** —— 因为标题带编号 `1_`、
用户措辞带后缀「部分」，两端各多一截，双向包含同时失效。

**为什么修复必须在匹配层，不在抽取层**：按设计 LLM **拿不到章节清单**
（`analyze(text, llm)` 是消息的纯函数，`current_sections` 只用于校验投影、
从不参与生成 —— 见 `plan_validator` docstring 与本文档 B-3）。
所以它只能照抄用户原话。⇒ **容忍用户措辞是匹配层的责任。**

**顺带发现的结构问题**：`SectionIndex.find` 与 `plan_validator._matches` 是
同一判据的**两份独立实现**。`_matches` 的注释写着「呼应 SectionIndex.find」
—— 呼应但不共用，正是会各自漂移的结构。分叉的两个方向都很糟：

| 分叉方向 | 后果 |
|---|---|
| validator 松、find 紧 | 计划期放行，执行期 `章节不存在` 运行时失败 |
| validator 紧、find 松 | 明明能改却在计划期被拒（**本次 bug 的形态**） |

**修复**：
- [x] `match_key()`：**匹配专用**归一，只做一件 `slugify` 不该做的事 —— 剥章节编号
      （`1_引言`→`引言`、`2_1跨视角...`→`跨视角...`、`一、引言`→`引言`）
- [x] **不动 `slugify`**：它生成持久化 id，被 `facts.claims` 的键与 checkpoint
      指纹引用，放宽它会让已有会话数据失配。拆出 `_slug_core()` 让两者共用字符
      规则、但 `match_key` 不继承 `"section"` 空值兜底
      （否则空 anchor 会匹配上真名叫 section 的章节 —— 空输入匹配到东西比匹配不到更糟）
- [x] `anchor_match_rank()`：五档强度，成为「章节指代」的**唯一判据**，
      `find` 与 `_matches` 共用
- [x] `find` 改取**最强**匹配而非首个命中

**分档**（档 4 刻意排在档 3 之上）：

```
5  id 或标题精确相等
4  去编号后精确相等           「方法」→「3 方法」
3  标题包含 anchor            原 find 的宽松档
2  去编号后章节名 ⊂ anchor    「引言部分」→「1 引言」   ← 本次 bug
1  去编号后 anchor ⊂ 章节名   「引言」→「引言与相关工作」
```

档 4 高于档 3 的理由：文档同时有「3 方法」和「3.1 方法概述」时，
anchor=「方法」在旧逻辑下两者**同档**，结果由它们在文档里的先后决定
—— 靠排版顺序碰对答案，不是靠判据。

**测试**：`tests/test_section_anchor_matching.py` 56 项。最要紧的一条是
`test_find_and_validator_agree`：对一张 anchor 表断言两条路径**同时可解析或同时不可解析**，
直接守住分叉这个 bug 类别。另有 `test_slugify_not_loosened` 守住 id 稳定性。

### 修正一处早前的判断范围

写测试时才发现：**原有逻辑已经能解析「引言」**（`"引言" in "1 引言"` 命中原宽松档），
`_matches` 也能（`"引言" in "1_引言"`）。真正失效的只有 **anchor 比章节名多出词**
的情况（「引言部分」「引言这一章」）。修复面比最初判断的更窄。

### 对第七节的补充：这是第 4、5 例

| # | 表现 | 测试为何没抓到 |
|---|---|---|
| 4 | docx 上章节意图报「找不到章节」 | 没有「格式 × 意图可用性」矩阵测试；docx 只被 format_docx 的测试覆盖 |
| 5 | 「引言部分」匹配不上「1 引言」 | 测试用的章节名都是无编号的合成值（`"引言"`、`"introduction"`），**真实文档的标题带编号** |

第 5 例与第 3 例（`by_purpose` 静默失效）**根因完全相同**：
测试喂的是理想化的合成字符串，真实数据的形态没有进入测试。

**这条纪律该从"建议"升为"要求"**：
- [ ] 涉及"用户/LLM 提供的标识符"的匹配逻辑，测试必须包含**真实文档产生的形态**
      （带编号的标题、带后缀词的指代），不能只有干净的合成值。
      本次已在 `test_section_anchor_matching.py` 用真实骨架标题结构做了，
      但 polish / add_section / convert 的同类测试仍是合成值。

### X-7 · 校验器知道正确做法却拒绝执行（真机第三次失败）

**复现**：对已有「1 引言」章节说「帮我完善我的引言」→ 理解层产出
`add_section(title="引言")` → 校验器回：

```
rejected（section_exists:章节「引言」已存在，应改用 rewrite。）
```

**问题不在判定，在处置。** 判定完全正确 —— 章节确实存在，确实该用 rewrite。
但校验器**把正确答案写在了错误消息里**，然后终止，要求用户自己去改措辞。
用户得先知道系统内部有 `add_section` / `rewrite` 之分，才能把话说对。

这与 B-2 里 `fix_bibliography` 是同一性质的**责任转移**：
系统有足够信息自行完成，却要求用户先学会系统的内部规则。

**为什么理解层会选错**：按设计 `analyze` 是消息的纯函数，**拿不到章节清单**
（B-3 那条刻意维持的性质）。「完善我的引言」既可以理解成「引言还没写，写一个」
也可以理解成「引言写了框架，扩写它」。不给文档状态，这是**无法确定的**。
LLM 选 add_section 不算离谱。

**修复：给静态校验加一条自修复通道。**

`planning_loop` 里**本来就有**重规划机制（`PlanRevisionRequest` + `continue`），
`unmapped` 补充说明与用户修改都在用，只是没接到校验失败上：

```python
if validation.result == "rejected":
    hint = _repair_hint(validation, current_sections)
    if hint and repairs_used < _MAX_VALIDATION_REPAIRS:
        repairs_used += 1
        revision = PlanRevisionRequest(plan, hint, RevisionReason.VALIDATION_REPAIR)
        continue                      # 回循环顶重建计划
    return PlanningExit(REJECTED, _issue_summary(validation))
```

**白名单**（判据：校验器已经知道正确做法，只是没去做）：

| 问题码 | 修复提示 |
|---|---|
| `section_exists` | 「完善/改进/扩写**已存在**的章节应用 rewrite，不要用 add_section」 |
| `section_not_found` | 附上**现有章节清单**，让它重选或改用 add_section |

**白名单判据是两条，不是一条**（开发中被既有测试纠正过一次，见下）：

1. 校验器已经知道正确做法
2. **修复只改变「怎么做」，不改变「做什么」** —— 这一条更关键

`convert_not_terminal` **满足第 1 条但不满足第 2 条**，故排除：它的修复只有
「挪到最后」（改了操作顺序，而顺序有语义：「转 docx 再润色」≠「润色再转 docx」）
或「本轮不做转换」（丢掉一条用户要求）。这种取舍必须由用户做，
系统替他决定就是**静默改需求**。

**这条判据是被既有测试逼出来的**：第一版白名单把 `convert_not_terminal` 收进来了，
全量回归时 `test_planning_clarifier_loop.py::test_loop_rejects_convert_not_terminal`
失败。那个测试编码的是一条真实设计决定，正确地拦住了一次误放宽。
（记一笔：这是本次会话里**唯一一次**既有测试成功拦住新改动的越界 ——
与第七节那五例「测试没抓到」形成对照，说明锚点型测试确实有价值。）

其余不在白名单里的：`format_lacks_sections`（改不了文件格式）、
`unsupported_intent`（能力不存在）、`conflicting_constraints`（需用户裁决）、
`convert_same_format`、`format_requires_docx`。重试只会烧调用。

**四条边界**：
- 混有不可修复问题 → 整体拒绝，**不做半修复**（否则用户看到一份改了一半的计划）
- 上限 1 次（`_MAX_VALIDATION_REPAIRS`）—— 给了明确指令还改不对，再试也不会对
- 用尽后报告**原始**校验问题，不是「超过修订次数」（不能把归因丢掉）
- 发 `PLAN_VALIDATION{phase="self_repair"}` 事件，自修复必须可观测

**关于「operations 是消息的纯函数」这条性质**：`section_not_found` 的提示里注入了
章节清单。**初次 analyze 仍然只看消息**，注入发生在**修订**路径上，而
`revision_text` 本来就承载任意文本（用户补充说明、执行期重规划原因）。
初次理解的确定性未被削弱。这条已写进 `_repair_hint` 的 docstring。

**测试**：`tests/test_planning_self_repair.py` 15 项，含真机场景回归
（`add_section(引言)` → 自修复 → `rewrite(引言)` → ConfirmedPlan）。

---

## 十、对「是否过度设计」的回答（用户 2026-08-01 提出）

连撞三次才跑起来之后，用户问：「当前的系统是否过度设计了？」

**不是过度设计，但也不是无辜。** 三次失败按性质分：

| # | 性质 | 与"过度设计"的关系 |
|---|---|---|
| X-5 docx 无章节索引 | **功能缺失**（只写了 tex/md 两个解析器）+ 诊断归因错 | 无关。是没做完 |
| X-6 「引言部分」匹配不上「1 引言」 | **实现缺陷**（编号前缀）+ 同一判据两份实现 | 无关。是没测真实数据 |
| X-7 知道该用 rewrite 却拒绝 | **设计缺陷**：层与层之间只传递拒绝，不传递已知的修正 | **有关**，但根因不是"层太多" |

三次都不是"抽象太多"造成的。真正的共同根因是第七节那条：
**单元测试验证了"函数逻辑对"，但没验证"真实数据能流到这个函数"。**
1148 个测试全绿，而这条链路从来没有人真的从"用户说一句话"走到"产出正文"。

**代价是可量化的**：`.kiro/specs/` 12 个 feature、~40 份设计文档、十几个默认关闭的
flag，全部产出为零篇成稿。不是设计太复杂，是**设计与验证的比例失衡** ——
设计文档的密度远高于端到端验证的密度。

**唯一站得住的"过度设计"指控是 X-7 那类**：一个操作要经过
analyze → pre_extract → build_plan → validate → clarify → unmapped → confirm →
execute → gate 九道关。关本身都有理由，但它们之间**只传递"通过/拒绝"，不传递
"我知道正确答案是什么"**。九道关里任何一道知道答案却只说"不行"，用户就得自己当调试器。

**所以 X-7 的修法（自修复通道）比"减少层数"更对症**：不是拆掉关卡，
而是让关卡把已有的确定性知识往回传。这条模式应当推广到其他关卡，逐个审查
"这个拒绝里是否已经蕴含了正确做法"。

**优先级建议**（写在这里以免下次又被新功能挤掉）：
- [ ] 端到端验证密度补齐：一个"真实用户措辞 → 产出正文"的冒烟，纳入 CI（D-5 的一部分）
- [ ] 审查其余拒绝出口，凡"已知正确做法"的一律改为自修复或追问，不得终止
- [ ] 在补齐上述两条之前，**不新增 feature flag**

---

## 十一、X-8 · unmapped 记账漏项，与「mock 全对、真实全错」的根因（2026-08-01）

### 现象

```
> 我目前小论文只写了一个框架，我现在需要去写我的引言部分，你能帮我完善我的引言部分吗，
  要求参考文献10，英文8，中文2

有一条要求我没能安排进任何操作：「参考文献10」
怎么处理？(1=补充说明 / 2=忽略这条 / 3=取消本次)
```

注意**只有「参考文献10」**落空（上一次尝试是整句都落空）。说明 `英文8` / `中文2`
这次被配额槽认领了 —— **值已经在配额对象里，只是账本没记上第三条。**

### 机制

`plan_builder._build_dispositions`：一条 requirement 只有落进某个
`SlotValue.requirement_ids` 才算 mapped。而那个列表是**LLM 自报**的
（「我这个槽位吃掉了哪几条要求」）。一个槽位覆盖多条要求时，LLM 会漏报。

**这与 X-5/X-6/X-7 不是一类**：
- 那三个是「明明能做却不做」——功能/实现/设计缺陷
- 这个是**记账漏项**，且失败方向是**安全的**（宁可多问一句，不敢静默丢弃）

D4「可验证零静默丢失」的目标是对的，实现代价是偶发的无谓追问。

### 为什么 1219 个测试全绿也抓不到

**因为没有任何测试能抓到它。** mock LLM 完全按脚本返回，它报几条 requirement_ids
就是几条 —— 脚本里写的一定是对的。这类问题**只有真实 LLM 评测能发现**。

而项目**已经建了正确的工具**：`tests/eval/intent_planning/`
（`golden.json` + `scorer.py` + `runner.py`，度量意图准确率/约束召回/
requirement 漏识别率/Silent_Loss）。工具是对的，问题在**装进去的东西**。

审查 35 条 golden 用例，三项覆盖为零：

| 缺口 | 实际情况 | 对应漏掉的 bug |
|---|---|---|
| 引用配额 | **0 条**用例带配额。A2d 整个 feature 的核心槽位零覆盖 | X-8 |
| 带编号的章节标题 | 全是 `introduction` / `method` / `讨论` 这种干净名 | X-6 |
| 口语铺垫式长句 | 全是「润色引言」「重写相关工作这一节」命令式短句 | X-6 / X-7 |

**golden 集编码的是开发者设想的用户说话方式，不是用户真实的说话方式。**
这正是第七节那条纪律的最深层版本：不只是单元测试喂了合成值，
连**评测集**本身喂的也是合成值。

### 还发现两处评分器自身的问题

**1. `expected_unmapped` 是死数据。** `load_golden` 读进 `GoldenCase`，
`score_case` **从不检查**。于是「哪条应该落 unmapped」「哪条不该落」这项人工标注
不进任何指标。`silent_loss` 抓不到 X-8（那条确实进了 unmapped，账本是平的），
只有**跟人工标注对账**才能发现。

**2. `scorer.py` 有两套同名澄清指标。** `ClarificationScore` /
`ClarificationMetrics` / `score_clarification` / `aggregate_clarification`
各定义两次，字段还不同（旧：`needed_clarification/repeat_ask_count/unnecessary`；
新：`asks/unnecessary_asks/repeat_asks/confirm_calls`）。Python 后定义者遮盖前者
⇒ 旧的一整套是**死代码**，runner 调的一直是新的。写 Task 18 时是追加而非替换。
`__all__` 也因此列了两遍。

### 已修

- [x] `expected_unmapped` 接成活指标，**两个方向都查**（严重性不同）：
      - `unmapped_missing`：标注应落却没落 → 系统**静默接受**了做不到的事（危险）
      - `unmapped_spurious`：标注不该落却落了 → 用户被追问已满足的要求（扰民，X-8）
      - `spurious_cases` 带原文进基线 JSON，便于定位
- [x] 删掉 `scorer.py` 的死代码块，留注释记明教训
- [x] golden 集补 **8 条真实形态用例**，每条带 `note` 说明它守的是什么：
      `real_quota_rambling_intro`（**用户原话**）、`real_quota_explicit_sentence`
      （规范措辞，与前者配对以区分「配额抽取不行」vs「口语措辞才不行」）、
      `real_numbered_section_with_suffix_word`、
      `real_improve_existing_section_is_rewrite`、`real_quota_on_new_section`、
      `real_docx_rewrite_should_still_parse`、`real_rambling_with_context_preamble`
- [x] `tests/test_eval_scorer_unmapped.py` 11 项（确定性、零 LLM、进默认套件）。
      含 X-8 的最小复现（构造「槽位认领 2/3 条」→ 断言 `silent_loss==0` 但
      `unmapped_spurious==1`），以及三条 golden 集覆盖度守护
      （配额 ≥3 条、带编号标题 ≥4 条、长句 ≥2 条），防止真实用例被后续编辑删掉
- [x] `console_interactor.resolve_unmapped` 文案：说明「没能安排」是**账本口径**、
      不等于做不到，并指向紧接着的计划确认可核对

### 未做

- [ ] **X-8 本体未修**（只修了「能测出来」）。两个方向：
      - 让槽位的 requirement 认领不再单靠 LLM 自报。`AtomicRequirement` 有
        程序定位的 `source_start/source_end`，但 `SlotValue` 只有 `verbatim` 字符串、
        没有 offset。按 verbatim 子串包含补认领是可行的，但**有过度认领风险**：
        若某抽取器把整条消息当 verbatim，所有 requirement 都会被认领，
        unmapped 永不触发 —— 那正是这套机制要防的静默丢失。需先给 `SlotValue`
        加程序定位的 offset，再按 span 重叠补认领。
      - 或让 analyzer 不要把一个复合配额拆成三条原子要求（拆分本身也由 LLM 做，
        同样不可控）
- [ ] **扩展 `PlanningInteractor` 协议**，让 `resolve_unmapped` 能展示「已理解的操作
      与槽位」。当前只传要求原文，用户无从判断选「忽略」会不会丢东西。
      涉及协议签名 + 若干测试 fake，故本次只改了文案
- [ ] **跑一次 golden runner**（`python tests/eval/intent_planning/runner.py`，打真实模型）
      并归档 `baseline.json`。这是本项目**唯一**能发现 X-8 这类问题的手段，
      而它从未在补入真实用例后跑过

### 对「是否做得过于垃圾」的回答（补第十节）

用户问：「mock 的测试全对，一上真实场景全是 bug，是否做得过于垃圾了？」

**"mock 全对、真实全错"不是代码质量问题，是验证策略问题**，而且是有明确解法的那类。

四个 bug 里，mock 测试**原理上**能抓到的只有两个（X-6 匹配逻辑、X-7 处置逻辑），
它们没被抓到是因为测试输入用了合成值。X-5 需要 docx×意图的矩阵测试。
**X-8 任何 mock 测试都抓不到** —— mock LLM 按脚本返回，脚本里写的一定是对的。

所以正确的验证分层是：

| 层 | 抓什么 | 现状 |
|---|---|---|
| 单元（mock） | 函数逻辑 | 1219 项，**输入多为合成值** ← 症结在这 |
| golden 评测（真实 LLM） | LLM 自由度导致的问题（意图选错、槽位漏报、requirement 漏认领） | 工具已建好，**装的是理想化用例**，且从未跑过基线 |
| 端到端（真实全链路） | 接线、格式、真实数据形态 | **零次** |

**三层里两层是空的。** 这就是"1219 测试全绿 / 0 篇成稿"的全部解释。
不是代码垃圾，是**只建了金字塔的底座**。

**具体该做什么（按顺序，不要跳）**：
1. 跑 golden runner 出真机基线 —— 现在有真实用例了，一次几分钟，会直接量化出
   意图准确率与 `unmapped_spurious`
2. 按基线暴露的问题改 prompt / schema（比如给 `citation_quota` 的 slot description
   明确写「把全部配额相关要求的 requirement_id 都列进来」）
3. 一条「真实措辞 → 产出正文」的端到端冒烟，进 CI（D-5 的一部分）
4. **在这三步做完前不新增 feature flag**（与第十节同一条）

---

## 十二、X-9 · 引用配额从未被抽取（2026-08-01，已修）

spec：`.kiro/specs/constraint-fidelity/`（bugfix + design）

### 更正 X-8 的因果分析

第十一节 X-8 基于**不完整日志**得出「配额槽认领了『英文8』『中文2』却漏报
『参考文献10』，纯记账漏项」——**结论错误**。完整日志显示三条配额一条都没被认领，
`citation_quota` 自始至终 `user_absent`、无取值。

连带影响：X-8 提出的「给 `SlotValue` 加程序定位 offset、不靠 LLM 自报
`requirement_ids`」这一修复方向**不解决本缺陷**——没有取值的槽位不在任何匹配候选里。
X-8 已完成的部分（`unmapped_spurious` / `unmapped_missing` 活指标、8 条真实形态
golden、`scorer.py` 死代码清理）仍然有效。

### 排除的两个错误假设（均有实测证据）

**「`REWRITE_SPEC` 没有文献相关槽位」——不成立。** 实测：

```
REWRITE_SPEC.slots 含 citation_quota，binding=citation_quota，value_type=object
tool schema 顶层参数含 citation_quota，嵌套 constraints[].dimension 枚举
  = {total, language.zh, language.en}，operator 含 bare
```

槽位在、绑定在、schema 在，**一切就位，模型什么都没填**。
（对照：`POLISH_SPEC` 确实没有该槽 ⇒「润色引言要引 10 篇」落 unmapped 是**正确行为**。）

### 真正的两处缺陷

**缺陷一：路由——analyzer 算出的归属被丢弃。**

`RawOperation.requirement_ids` 由 analyzer 填写（「本操作覆盖哪些要求」），
但 grep 全部下游读取点：**一处都没读**，所有读取都是抽槽之后的
`SlotValue.requirement_ids`。于是抽取器只看到 `verbatim` 切片
`完善我的引言部分` —— 一个数字都没有，模型按槽位说明「没提就不要给」正确省略。

字段被丢弃的理由见 `plan_builder` docstring：「覆盖判定按 Slot 消费粒度，
仅挂在 Operation 上不算 mapped」。**那条规则本身正确**，但它只关于**记账**。
不能用于记账 ≠ 不能用于**路由**。两件事混为一谈 ⇒ 字段被用于零处。

**次生问题（被上面掩盖）**：`_understand` 传的是 `ar.requirements`（**全部**要求），
所以多操作时每个抽取器的 system 消息里都列着别人的要求——交叉污染风险一直存在。

**缺陷二：object 型槽位在扁平 payload 下从未被读到过。**

这一条是**写测试时才发现的**，且路由修复单独治不了它：

```python
# _coerce_slot 原判据
if not isinstance(raw, dict):
    raw = {"value": raw}          # 只有裸值才包装
value = raw.get("value")          # citation_quota 的 dict 没有 "value" 键 → None
```

`build_tool_schema` 生成的是**扁平** schema ⇒ 模型必然发
`citation_quota: {"filters": [...], "constraints": [...]}` 这种裸 dict ⇒
被误认成 `{value, status}` 信封 ⇒ `value=None` ⇒ 静默判 `user_absent`。

**即使路由修好、文本送到、模型正确填了配额，值仍会在这里被丢掉。**
判据错在「看类型」，应当「看有没有信封标志键」。

### 修复

- [x] `operation_analyzer.requirements_for(op, all_reqs, n_ops)` 纯函数：三档归属
      （`claimed` / `sole_operation` / `fallback_all`），幻觉 id 过滤与 plan_builder 同口径
- [x] `extractors/base.build_extractor_user_text()`：user 消息 = 操作原话 +
      归属要求原话（去重、标点不重复）。**放 user 而非 system**：要求列表此前已在
      system 里，模型仍不填——两个信号冲突时它采信 user 消息与槽位 description。
      同一信息放在不同位置权重不同
- [x] `extractors/base._unwrap_slot_payload()`：信封识别改看 `value`/`status` 键
      （缺陷二）
- [x] `extractors/base._is_empty_object()`：object 型槽位的空壳判 `INVALID`
      （声明 `clarify_if_user_invalid` 时）或 `user_absent`。**刻意不覆盖 array 型**
      ——空列表改判会让 `source` 从 `user_explicit` 变 `defaulted`，是可观测行为变化
      且与本缺陷无关。判据是**类型**不是槽位名，故非单槽位特判
- [x] `planning_loop._understand` 接线 + `fallback_all` 时发观测事件
- [x] **`turn_service._compute_shadow` 与 `tests/eval/intent_planning/runner.py`
      一并接线** —— runner 不接线会导致 golden 指标不反映修复，
      这正是 F-3 踩过的坑（「runner 此前不传接受策略 → 恒 strict」）
- [x] 缓解措施（单列，不得用其效果论证不变量已满足）：`verbatim` description 补
      **正向数量要求**的例子（原举例全是禁止类，是被裁掉的直接诱因）；
      `requirement_ids` description 说明它现在是**承载性**字段、漏填的要求不会被执行
- [x] `tests/test_constraint_fidelity.py` 26 项。全量 **1256 passed**，无回归

### 刻意未做（记录理由，避免重复讨论）

- **不新增 `RawOperation.constraints` 字段**：已有 `requirement_ids` 足够；
  且 `Operation.constraints` 是被**刻意删除**过的（反规范化副本会陈旧，
  曾导致确认摘要显示「目标=空」），同名字段会误导后来人
- **不新增要求分类**（`kind_of` / `SlotSpec.bearable_kinds`）：`isBearable` 只在
  **验证时**需要，运行时不需要。验证地面真值已存在（golden 的 `expected_unmapped`
  + `unmapped_spurious`）。而 `AtomicRequirement.category` 是**自由文本**，
  与 F-3 的 `section_purpose` 同一陷阱
- **不新增覆盖检查指标**：`unmapped_spurious` 已能回答「残余遗漏率」
- **不改记账规则**：路由用 `raw.requirement_ids`，记账仍只认 `SlotValue.requirement_ids`
- **作废的前一版设计**：基于字符区间包含的归属算法（新增 `tasking/attribution.py`、
  `locate_operations`、`RawOperation` 三个 span 字段、一条多操作追问分支）。
  作废理由：为一个可用现成字段解决的问题引入区间运算，且整个方案压在
  「`verbatim` 是原文连续子串」这个被自己列为一号风险的假设上。
  **净效果对比：9 个文件 + 1 新模块 + 3 新字段 → 5 个文件 + 0 新模块 + 0 新字段**

### 一号风险

`requirement_ids` 此前是**装饰性字段**（填了也没人读），现在变成**承载性**字段，
其填充质量**从未被度量过**。若 analyzer 漏填率高，修复效果受限。

- 观测点：`PRE_EXTRACT` 事件的 `attribution=fallback_all` 计数 + `unmapped_spurious`
- 降级已定：多操作 + 未声明归属 → 退回现状（列全部），**不比现状更差**

### 记入 backlog、不在本 spec 修

`_infer_consumption` 有一处潜伏失配：`target_ext` 的 normalizer 把 `Word` 归一成
`docx`，而 requirement 原话是「转换成 Word」——**子串匹配不上**。现在能对上纯靠
模型顺手填了 `verbatim` 字段。属于「每槽位 `verbatim` 必填」那个重构的范围。

### 用户提出的三条架构判断（已评估，认定成立，列为后续方向）

1. **抽 RawOperation 这一步基本多余** —— 它的硬理由只有多操作拆分，
   而拆分与填槽**可以在一次调用里做完**（返回 `list[{intent, slots}]`）。
   调用数从 `1+N` 降到 `1`，且本缺陷在结构上不可能发生。
   代价：schema 变成五意图槽位并集（约 25 字段），条件有效性要靠 `oneOf`；
   字段级 description 的实测收益（约束召回 73%→100%）是否被稀释需实测。
   **净效果是删代码**：`operation_analyzer.py` + `extractors/` 7 个文件 → 1 个模块
2. **`_infer_consumption` 的文本对比很烂** —— 成立（见上面的 normalizer 失配）。
   建议替代：**每槽位 `verbatim` 必填**（「引用用户的原话，说明你为什么填这个值」），
   消费判定改为 requirement span ⊆ 各槽 verbatim span 并集。既不需要 LLM 做 id 记账
   （不可靠），也不被归一化打败
3. **意图/槽位设计 vs agent loop** —— 结论是**分层而非替代**：
   理解+规划该用 agent loop（开放、可迭代、能读文档），执行+护栏该保留现设计
   （Gate 靠结构化槽位定义、`plan_executor` 需执行前知道操作清单、
   目标可验证时程序判断严格优于 LLM）。与 P-4 的红线一致。
   **支撑数据：本次会话四个缺陷，三个在理解层，零个在执行层与 Gate 层。**
   纠正一点：「agent loop 不需要用户确认」不是架构性质而是**可逆性**性质；
   本项目改动本已可逆（working copy + git baseline），故放宽确认是独立决定

**顺序纪律**：上述三项都是换架构而非修缺陷，且本项目成稿数仍为 0。
在拿到第一份真实产出前不启动重写——否则是重复「设计密度远高于验证密度」这个老问题。
X-9 的修复不给上述任何一项制造债务：它改动的代码在重构里本来就要删。

---

## 十三、X-10 · 证据集预算 8000 硬编码，让 `total=10` 恒定失败（2026-08-02，已修；未修项 A 已于 2026-08-12 补修）

### 现象

真机第四次失败。用户「完善引言」，配额 `参考文献10 / 英文8 / 中文2`。
检索入库 10 篇、`【配额缺口】` 只报 `language.zh`（2 缺 2）、用户接受偏差，
随后 `plan_status: failed`：

```
rejected — 写作前文献预检未完成（needs_gap_decision）：
上下文预算不足以容纳配额所需证据，需调整预算或接受偏差
```

### 根因（三个独立缺陷叠加）

**1. `budget_chars=8000` 从未按真实数据校准，且 CLI 无法调整。**
它只是 `build_writing_orchestrator` 的默认参数，`config.py` 无字段、`.env` 无变量、
`chat.py` 不传。实测（`proj_1780f6e14f27` 的 10 条 OpenAlex 记录）
平均 1521 字符/条 ⇒ **8000 只装 5 条**，而 `total=10` 需 15213 字符。
即：**只要用户要 10 篇，此路必死**，且无任何配置出路。

**2. 接受偏差救不了，因为它只动有缺口的维度。**
`literature_flow.py:279-284` 把 `effective_lower_bound` 冻结为 `g.achieved`，
而 `gaps` 只含 `language.zh`。`total=10` 没缺口（可用正好 10）⇒ 一字未动 ⇒
`build_bundle` 第 4 步「补 total」照样选满 10 条 ⇒ 预算照样爆。
**用户被要求接受的偏差，与真正卡住的约束无关。**

**3. 提示的两条出路都不通，且是死胡同。**
"调整预算"→ 无入口（缺陷 1）。"接受偏差"→ 刚接受过且无效（缺陷 2）。
`_pause` 写了 checkpoint 但 `chat.py` 无 `resume` 命令 ⇒ 断点存了没法恢复。
而且预算不足分支**没有再调 `port.resolve_quota_gap()`**，直接返回。

**这是 X-7 那条模式的第二次复发**：关卡知道问题是什么，却只说「不行」，不给能走的路。

### 已修

- `Config.literature_bundle_budget_chars`，默认 **24000**（≈16 条全摘要），
  `PAPER_LIT_BUNDLE_BUDGET_CHARS` 可覆盖，0=不限。
  `build_writing_orchestrator(budget_chars=None)` → 取 config；显式传值仍优先
  （dogfood runner 与测试用 0）。
- `BundleEntry.char_cost()` 不再重复计标题（`_entry_of` 已建 title span，
  此前又加 `len(self.title)`）。真机 10 条虚高 746 字符。
- `chat.py` 启动时打印生效预算与约合篇数。

**验证**：拿真机 `facts.json` 的 10 条原样复算 ——
旧 8000 → `context_budget_insufficient`；新 24000 → `ok`，10 条全进，cost=14467。

### A（预算被当成正确性约束在执行）— 已修，2026-08-12

原文记的缺陷：`build_bundle` 资源不足时**否决用户明确提出的要求**，而不是降级；
而按这批文献的相关性分布（2 relevant / 8 weakly）实测「relevant 全摘要 +
weakly 只给题录」只需 ≈4000 字符，即连 8000 都够用 —— 撞墙的是「一律全摘要」
这个一刀切，不是预算值。

修法：`build_bundle` 的降级阶梯从两档扩到**三档**，依次尝试、取第一个装得下的：

1. 全摘要（`degradation="full"`）
2. 按 `abstract_ladder` 截短摘要（`"truncated_abstract"`，原有）
3. **按 `drop_ladder` 退回题录**（`"bibliographic_only"`，本次新增）：排序靠后的
   条目只给题录，摘要留给靠前的；每退一档都重新从全摘要试起，因为腾出的空间
   可能让留下的条目拿回全摘要（依据 P-3a：全摘要少篇 > 截摘要多篇）

三档都装不下才 `CONTEXT_BUDGET_INSUFFICIENT`。原文说的「降级机制已经存在但没接上」
正是这么接的：退回题录后 `evidence_available=False`，Req 12.12 那条
「不得据标题推导具体结论」自动生效，没有新写 prompt 约束。

**按排序位次而不是按 verdict 分级**（与原文表格的差异，重要）：默认
`STRICT_ACCEPTANCE` 只接受 `relevant`，`eligible` 里全是同一个 verdict ⇒ 按 verdict
分级在默认档下**恒等于什么都不做**，正好在本条抱怨的场景里失效。改用 `eligible`
既有的稳定排序（verdict → confidence → 年份新 → id）位次：verdict 混合时
（`WEAKLY_ACCEPTANCE`）weakly 自然排在后面先被退掉，两种接受档下都成立。
阶梯 `(0.5, 0.75, 0.9)` 刻意不含 1.0，且至少留 1 条摘要 —— 全是题录的 bundle
支撑不了写作，那时该交上层决策而不是产出空壳。

`BundleEntry.abstract_dropped` 与「记录本身没摘要」**必须分开**：两者
`evidence_available` 同为 False，但渲染措辞不同。把"我没给"说成"原文没有"，
模型会据此向用户断言该文献没有摘要 —— 那是把我们的取舍讲成文献的事实。

**验证**：10 条 ~1540 字符记录、`total=10`，budget=4000 ⇒ 旧行为
`INSUFFICIENT`（用户要的 10 篇一篇不给）、新行为 `ok` 且 10 篇全进；
混合 verdict（2 relevant / 8 weakly）下 budget 降到 2000 仍 ok，
退掉的 8 条全是 weakly、2 条 relevant 保住摘要 —— 与原文表格的判据一致。
新增 6 条 bundle 测试 + 2 条渲染措辞测试。

`degradation` 现有三个取值，A/B 脚本 `ab_context_budget.py` 已显式
`drop_ladder=()` 关掉第 3 档 —— 该实验的自变量只能是 `abstract_ladder`，
否则 A 臂会从"少给几篇全摘要"变成"给满篇数但部分只有题录"，与已归档的
25rec 真机付费结果不再可比。

### 未修（本次范围外，按严重性排序）

**B. rewrite 完全看不到全文上下文 —— 不报错，但静默降低质量。**
grep `generate/` 确认：无任何地方把论文标题、大纲、其他章节标题、
摘要、贡献点送进 prompt。rewrite 拿到的只有
`目标章节正文 + 证据块 + 已知数字 + 改写方向`。
写引言时模型看不到方法章讲了什么、贡献点是什么、结论声称什么——
而引言恰是全篇最依赖这些的一章。

`facts.claims` 有 `novelty`/`gap`/`contributions`，`add_section` 传了
（`context_builder.py:176`），**`rewrite` 不传**；且真机 `facts.json` 里
`claims == {}`，从未被填过。**通道对 rewrite 不存在，数据本身也没有。**
这比预算数字严重：它不会失败，只会让引言泛泛而谈且看不出原因。

**C. 字符计量而非 token 计量**（P-3 已记）。中文约 1–1.5 字符/token、
英文约 4 字符/token ⇒ 同一预算下中文真实 token 成本是英文的 3–4 倍。
对一个显式带 zh/en 配额的系统，这个口径本身就是错的。

**D. 两套预算体系仍未统一**（D-4）。同一 prompt 里目标章节正文
（`original`，通常远大于证据块）**无任何上限**，只有证据块被卡。
把最重要的那段限死、最占体积的那段不限，是 D-4 的具体形态。

**注**：A 与 P-3a 是同一件事的两面 —— P-3a 质疑「补满预算」是负收益（context rot），
A 指出预算不足时该降级而非否决。两者都指向**分级证据**而非**调大预算**。
A 已修（见上），P-3a 的「补满是否负收益」仍未定论；C/D 的计量口径问题不受 A 影响 ——
三档降级仍按字符计，中文真实 token 成本仍被低估 3–4 倍。
