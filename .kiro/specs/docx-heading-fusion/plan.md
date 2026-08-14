# 修复：polish/rewrite 写回 docx 时吞掉标题

## 现象（真机复现，2026-08-12）

对 4.2 节做一次「保守润色」后：

- `4.2` 的标题与正文首句被合并成一段，且该段仍是 `Heading 3` 样式
- `4.3 掩膜约束下的匹配与几何有效性分析` 整个标题消失，其下 9 段正文并入 4.2
- 文档标题数 15 → 14，docx 体积 456539 → 440435 字节
- 投影标记 `### 4.3 …` 以**字面文字**形式残留在正文段尾
- 全过程 gate 报 `passed: True`，系统报「1 次生成通过校验」

润色的文字质量本身是好的：用词替换合理，12 个实验数据全部原样保留。
**问题只在写回，不在改写。** 源文件未受影响（未 `:save`，已核 sha256 与标题数）。

## 根因（三个独立缺陷叠加，已零 LLM 复现）

### C-1 · `body_of` / `splice` 的换行契约没有防御

`section_index.py:238-247`。`body_start` 紧贴标题文字之后（heading 区
形如 `'### 4.2 主共视支撑域预测结果'`，**不含**尾随换行），故 `body_of`
返回的正文**以 `\n` 开头、以 `\n` 结尾**。

而 LLM 几乎必然返回 strip 过的文本。于是 `splice` 拼接时：

```
text[:body_start] + candidate + text[body_end:]
   ↓ 首部丢 \n                      ↓ 尾部丢 \n
'### 4.2 标题' + '正文首句…'     '…共视内容。' + '### 4.3 标题'
   → 合成一行（标题吞正文）           → 合成一行（正文吞标题）
```

复现（无需 LLM）：对 `body_of` 结果做 `.strip()` 再 `splice`，
即得到与真机完全一致的两处损坏。

`splice` 自身是正确的（恒等 splice 逐字节相同），缺的是对
「候选文本首尾换行」这一隐式契约的保障。

### C-2 · `_heading_levels` 漏了 docx 分支

`diff_gate.py:93` 只认 `("md", "markdown")`。docx 走投影文本、标题形态与 md
完全一致（`#` 前缀），却落到 `elif` 之外 → 返回空列表。

后果：`PolishGate` 第 115 行的标题结构检查对 docx **是空操作**。
同一处损坏，文档若是 `.md` 会被拦下（实测 violations 含「章节/标题结构发生了变化」），
是 `.docx` 就放行。

### C-3 · gate 只看 section body，看不到 splice 结果

`polish.py:59-81`：gate 在 `run_polish` 内对 `original_body` vs `candidate` 判定，
`splice` 在其**之后**。损坏发生在拼接那一刻，gate 结构上无从观察。

即便修好 C-2，gate 也只能看到「body 内部有没有标题」；本例 body 内部
本来就没有标题，首尾换行丢失导致的**跨边界**融合仍不可见。

## 改动方案

按「越靠近损坏点越优先」排序。C-1 是必修，C-2/C-3 是让这类问题以后能被自动发现。

### 1. `section_index.py` — `splice` 保障边界换行（修 C-1）

在 `splice` 内规范化：若原 body 以 `\n` 开头而 `new_body` 不以 `\n` 开头，补上；
尾部同理。理由与位置选择：

- 放在 `splice` 而非各调用方：`polish` 与 `rewrite` 都受影响
  （`rewrite.py:80,236` 同一模式），放调用方要改两处且以后新增意图会再漏
- 不改 `body_start` 的定义：偏移语义被 `extractor.py:148` 依赖，
  且 `_parse` 的边界计算与 tex/md 共用，改定义波及面远大于本 bug

### 2. `diff_gate.py` — `_heading_levels` 纳入 docx（修 C-2）

第 93 行的 `elif ext in ("md", "markdown")` 加上 `"docx"`。
与 `section_index._collect_headings:181` 的既有注释一致
（「docx 走 md 分支：投影文本的标题形态与 md 一致」），这里属于漏改。

### 3. `docx_document.py` — 写回后断言标题结构（修 C-3）

`apply_text` 落盘前，比较「写回前文档的标题层级序列」与「新文本的标题层级序列」。
不一致则抛 `DocxDocumentError`，不保存。

放在 `apply_text` 而不是 gate：它守的是**真正发生损坏的那一层**，
且对所有调用方生效（含 `docx_crossref`、格式化路径）。
写入是原子的、且 working copy 有 git baseline，抛错不会留半截状态。

**边界（已定）**：**仅在标题丢失时报错**，新增标题放行。
判据是「写回前的标题层级序列」是否为「新文本标题层级序列」的子序列 ——
丢标题是损坏，加标题是意图（`add_section` 走 `insertion_offset` + 插入路径，
标题序列本就该变）。这样不需要改任何调用方的签名。

### 4. `chat.py` — 输出编码（独立小修）

`chat.py:152,163,166,177,185,232` 六处 `print("⚠ …")` 在 stdout 被重定向时
崩 `UnicodeEncodeError: 'gbk' codec`（U+26A0 不在 GBK）。
管道、`> log.txt`、CI、后台跑全会崩；交互式终端是 UTF-8 故一直没暴露。

全仓另有 `⇒`(36) `✓`(5) `✗`(2) `▶` `↻` 等，但只有这 6 处在 `print` 里
（其余在注释/docstring，不影响运行）。

改法：在 `main()` 开头对 `sys.stdout/stderr` 做 `reconfigure(encoding="utf-8",
errors="replace")`。比逐个换 ASCII 字符稳妥 —— 后者治不了以后新加的符号。

## 测试

新增 `tests/test_docx_heading_fusion.py`：

1. **复现用例（现在应为红）**：`body_of` → `.strip()` → `splice` → `apply_text`，
   断言标题数不变、`### ` 不以字面文字出现在任何段落里、4.3 仍是独立 Heading
2. `splice` 换行规范化的单元用例（首部丢、尾部丢、两端都丢、两端都在）
3. `_heading_levels(".docx")` 与 `("md")` 结果一致
4. `PolishGate(".docx")` 对「标题被吞」的候选文本报 violation
5. `apply_text` 的标题丢失断言：构造一个会丢标题的新文本，断言抛错且**文件未被修改**
6. `chat.py` 输出重定向：`PYTHONIOENCODING` 未设时管道跑不崩（用 subprocess + mock LLM）

回归：`tests/test_docx_inplace.py` 现有 5 处 `splice` 调用中，
`:195` 与 `:213` 的首参不以 `\n` 开头 —— 需确认规范化后这两条仍绿
（它们断言的是插入位置与样式继承，不是换行）。

## 不做

- 不改 `body_start` / `body_end` 的偏移语义（波及 `extractor.py`、tex/md 共用逻辑）
- 不动 `apply_text` 的 difflib 配对逻辑。**先前一版诊断把根因归在这里，是错的** ——
  它按投影行忠实执行，收到融合行就写融合段，本身无缺陷。
- 不修复已损坏的 working copy：`:undo` 或重新载入即可，源文件完好

## 验证

改完后重跑一次真机润色 4.2（同一节、同样「保守润色」），断言：
标题数 15 不变、4.3 仍独立、正文数据不变。这会再花一次 LLM 调用。
