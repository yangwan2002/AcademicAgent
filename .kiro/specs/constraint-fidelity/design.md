# Technical Design — constraint-fidelity

## Overview

需求文档（`bugfix.md`）确立的缺陷条件：存在「有处可承载却没被承载」的 requirement。
真机表现是三条引用配额全部落 unmapped，计划里没有 `citation_quota`。

**核心裁决一句话**：`RawOperation.requirement_ids` 这个字段 analyzer **一直在填**
（「本操作覆盖哪些要求」），但下游**一处都没读**、被完全丢弃。修复是**开始读它**
——抽取器的输入文本 = `verbatim` + 归属于本操作的 requirement 原话。

零新增字段、零新增模块、零新增声明表。改动是「别扔已有信息」，不是「新增机制」。

### 本文档取代的前一版设计

前一版设计（基于字符区间包含的归属算法）已**作废**，作废理由记录在此以免重复：

| 前一版 | 问题 |
|---|---|
| 新增 `tasking/attribution.py`、`locate_operations` | 为一个可用现成字段解决的问题引入区间运算 |
| 归属靠「requirement span ⊆ operation span」 | 压在「`verbatim` 是原文连续子串」这个假设上，该假设被自己列为一号风险 |
| 多操作 + 无主归属 → 新增一条 NEEDS_INPUT 追问分支 | 归属本可由 analyzer 一次决定（它看到整句，最有资格判断） |

**更准的根因说法**：不是「LLM 裁切不可靠」，而是**我们把一个已有答案扔了**。
analyzer 早已回答「这个操作覆盖哪几条要求」，下游不读，于是抽取器只能看切片。

### 范围自律

本 spec 目标是**让引言今天能写出来**，并拿到第一份真机 golden 基线。
不是让架构变好。以下已识别但**明确不在本范围**：

- 单次调用重构（analyze + N 个 extractor 合成一次调用，消除切片环节本身）
- 每槽位 `verbatim` 必填，取代 `_infer_consumption` 的文本匹配
- agent loop 作为规划器产出 `Task_Plan`

三项都是更好的目标形态，但都是换架构而非修缺陷，且本项目当前成稿数为 0
——在拿到第一份真实产出前启动重写，是在重复「设计密度远高于验证密度」这个老问题。
本 spec 的修复不给上述任何一项制造债务：它改动的代码在重构里本来就要删。

## Glossary

| 术语 | 含义 |
|---|---|
| **约束保真度（constraint fidelity）** | 用户表达出、系统有槽位可承载的每条修饰性要求，必须在计划里有 `SlotValue` 真正承载其取值，而不是变成追问或静默丢失 |
| **路由（routing）** | 决定「哪几条要求的文本交给哪个抽取器」。与**记账**是两件事 |
| **记账（accounting）** | 决定「这条要求算不算被消费了」。判据是 Slot 消费粒度，本 spec **不改** |
| **归属（attribution）** | analyzer 通过 `RawOperation.requirement_ids` 声明的「本操作覆盖哪些要求」 |
| **空容器** | `[]` / `{}` / 各层皆空的嵌套对象。语义上等于「什么都没说」 |
| **缓解措施 vs 程序不变量** | 前者改善概率（改 prompt），后者改变最坏情况（改数据流）。刻意分列 |

## Bug Details

### 复现（真机 2026-08-01，active 模式，原始记录）

```
我目前小论文只写了一个框架，我现在需要去写我的引言部分，你能帮我完善我的引言部分吗，要求参考文献10，英文8，中文2
```

```
有一条要求我没能安排进任何操作：「参考文献10」   → 2
有一条要求我没能安排进任何操作：「英文8」        → 2
有一条要求我没能安排进任何操作：「中文2」        → 2

计划包含 1 个操作（按执行序）：
  1. rewrite，目标=引言部分，目标描述=完善引言部分
```

三条配额全部落 unmapped，计划里无 `citation_quota`。若执行会产出零引用的引言。

golden 用例：`real_quota_rambling_intro`（`expected_unmapped: []`）。

### 已排除的两个错误假设（实测证据）

**假设一：「配额是抽出来了、只是没记上账」（backlog X-8 的结论）——不成立。**
完整日志显示三条一条都没被认领，`citation_quota` 自始至终 `user_absent`、无取值。
连带影响：X-8 提出的「给 `SlotValue` 加 offset、不靠 LLM 自报 `requirement_ids`」
方向**不解决本缺陷**——没有取值的槽位不在任何匹配候选里。
X-8 已完成的 `unmapped_spurious` 指标与真实形态 golden 仍有效。

**假设二：「`REWRITE_SPEC` 没有文献相关槽位」——不成立。** 实测：

```
REWRITE_SPEC.slots:
  section_id      binding=RewritePlan.section_id
  direction       binding=RewritePlan.direction
  details         binding=RewritePlan.details
  search_query    binding=RewritePlan.search_query
  want_search     binding=RewritePlan.want_search
  search_limit    binding=RewritePlan.search_limit
  citation_quota  binding=citation_quota          ← 存在，且有 execution_binding

发给模型的 tool schema 顶层参数：
  section_id / direction / details / search_query /
  want_search / search_limit / citation_quota / newly_discovered_requirements
```

槽位在、绑定在、schema 在，嵌套结构里 `constraints[].dimension` 枚举含
`total` / `language.zh` / `language.en`，`operator` 含 `bare`
——正好对应「参考文献10、英文8、中文2」。**一切就位，模型什么都没填。**

（对照：`POLISH_SPEC` 确实**没有** `citation_quota`。所以「润色引言，要引 10 篇」
落 unmapped 是**正确行为**——这正是 `isBearable` 要区分的两种情况。）

## Hypothesized Root Cause

### 它为什么不填：手里那句话里没有数字

同一次抽取调用，两个信号冲突：

```
tool schema 说：  citation_quota 是给引用配额的
                  「仅当用户明确提出篇数/语言配额时给出；没提就不要给」

user 消息说：     完善我的引言部分
                  ↑ 全部内容就这些
```

模型的推理是对的。`extractors/base.py::_build_tool_messages`：

```python
Message(role="user", content=operation.verbatim)      # 只有切片
```

`operation.verbatim` = `完善我的引言部分`。配额文本在 analyzer 切 verbatim 时
被留在外面了。**不是没地方放，是没东西可放。**

### 被丢弃的答案

`operation_analyzer` 的工具 schema 里，每个操作项有：

```python
"requirement_ids": {"type": "array", "items": {"type": "string"},
                    "description": "本操作覆盖的 requirement 的 id。"}
```

解析时也回填了：`requirement_ids=tuple(str(x) for x in op.get("requirement_ids", []))`。

**grep 全部下游读取点：没有一处读 `raw.requirement_ids`。** 所有读取都是
`SlotValue.requirement_ids`（抽槽之后的）。

它为什么被扔？`plan_builder` docstring 给了理由：

> 覆盖判定按 **Slot 消费粒度**：只有落进某个具体 `SlotValue.requirement_ids`
> 才算 mapped；「仅挂在 Operation 上」不算。

**这条规则本身正确**（挂在操作上不证明执行器会用它）。但它只关于**记账**。
不能用于记账 ≠ 不能用于**路由**。这两件事被混为一谈，结果字段被用于零处。

> **根本原因：analyzer 已经算出了「哪些要求属于哪个操作」，
> 但这个答案没有被传递到需要它的地方。**

### 一个被掩盖的次生问题

`planning_loop._understand`：

```python
er = ex.pre_extract(op, ar.requirements)      # 传的是**全部** requirements
```

所以多操作时，每个抽取器的 system 消息里都列着**全部**要求，不只属于它的
——交叉污染风险**现在就存在**，只是被「user 消息里没内容」这个更严重的问题掩盖。

按 `requirement_ids` 过滤会**同时收紧**多操作隔离，而不是放松它。

## Expected Behavior

1. 真机原话输入 ⇒ **0 次** unmapped 追问，计划中呈现引用配额（总数 10 / 英 8 / 中 2）
2. 抽取器看到的文本 SHALL 包含 analyzer 判定属于该操作的全部要求原话
3. 多操作时，各抽取器**只**看到属于自己的要求（比现状更严格）
4. 空容器（`{"filters": [], "constraints": []}`）SHALL NOT 被当作「有取值」
5. 用户真正做不到的要求（订机票 / 翻译 / 查重）**继续**落 unmapped 并追问
6. 记账规则（Slot 消费粒度）**不变**

## Correctness Properties

### Property 1: Fix Checking — 归属的要求必须进入抽取器视野

**Validates: Requirements 2.1, 2.2, 2.3**

```pascal
FUNCTION requirements_for(op, all_requirements, n_operations)
  INPUT: op of type RawOperation, all_requirements of type list<AtomicRequirement>,
         n_operations of type integer
  OUTPUT: list<AtomicRequirement>

  by_id ← {r.requirement_id: r FOR r IN all_requirements}
  claimed ← [by_id[rid] FOR rid IN op.requirement_ids WHERE rid IN by_id]
                                        // 幻觉 id 过滤，与 plan_builder 同口径
  IF LENGTH(claimed) > 0 THEN
    RETURN claimed                      // analyzer 给了归属 → 按归属过滤（收紧）
  END IF
  IF n_operations = 1 THEN
    RETURN all_requirements             // 单操作：全部只能属于它，无歧义
  END IF
  RETURN all_requirements               // 多操作且 analyzer 未给归属 → 退回现状，
                                        // 不倒退；同时计数告警
END FUNCTION

// 属性：归属的要求原话必须出现在抽取器的输入文本里
FOR ALL op, all_reqs DO
  visible ← extractor_input_text(op, requirements_for(op, all_reqs, n_ops))
  FOR EACH r IN requirements_for(op, all_reqs, n_ops) DO
    ASSERT r.verbatim IS SUBSTRING OF visible
  END FOR
END FOR
```

真实 LLM 上的外部观测量：`scorer.unmapped_spurious_total = 0`。

### Property 2: Preservation Checking — 非缺陷输入行为不变

**Validates: Requirements 3.4, 3.5, 3.7, 3.8**

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT normalize(F(X)) = normalize(F'(X))
END FOR
```

`normalize` **参与比对**的字段：

- `operations` 的 `intent` 序列（顺序敏感，D0）
- 每个操作的 `slots`：`{name: (value, field_status, source)}`
- `dispositions`：`{requirement_id: (status, operation_id, slot_names)}`
- `unmapped_requirements`（集合相等）
- `missing_fields`（集合相等，operation_id 归一后）
- `reorder_suggestions.suggested_order`（id 归一后）

**忽略**：`plan_id` / `operation_id` / `requirement_id`（比对前建双射映射归一）、
`plan_version`、时间戳。

### Property 3: 账本零静默丢失（既有，不得回归）

**Validates: Requirements 3.6, 2.11**

```pascal
FOR ALL X DO
  FOR EACH r IN F'(X).canonical_requirements DO
    ASSERT isBorne(r, F'(X)) OR fuzzy_member(r.verbatim, F'(X).unmapped_requirements)
  END FOR                                          // silent_loss ≡ 0
END FOR
```

`isBorne` 中「有取值」的判定按决策 2 收紧（空容器不算有取值）。

### Property 4: 多操作隔离（本次**收紧**，非仅保持）

**Validates: Requirements 3.3, 2.9**

```pascal
FOR ALL X WHERE COUNT(F'(X).operations) > 1 DO
  FOR EACH op IN F'(X).operations DO
    visible ← extractor_input_text(op, requirements_for(op, ...))
    FOR EACH r IN all_requirements WHERE r NOT IN requirements_for(op, ...) DO
      // analyzer 判定不属于本操作的要求，不得出现在本操作的抽取器视野里
      ASSERT r.verbatim IS NOT SUBSTRING OF visible
    END FOR
  END FOR
END FOR
```

**注意这条属性在修复前是不成立的**（现状把全部 requirements 列给每个抽取器）。
故它是**新建立的**不变量，不属于保持性检查。

## Fix Implementation

### 决策 1：路由 —— 读已有的 `requirement_ids`

**改动点一：`planning_loop._understand` 按归属过滤**

```python
# 现状
er = ex.pre_extract(op, ar.requirements)
# 修复后
er = ex.pre_extract(op, requirements_for(op, ar.requirements, len(ar.operations)))
```

`requirements_for` 是纯函数（伪代码见 Property 1），放在 `tasking/` 下现有模块，
**不新建模块**。

**改动点二：`_build_tool_messages` 把归属要求的原话放进 user 内容**

```python
# 现状
return [Message(role="system", content="\n".join(parts)),
        Message(role="user", content=operation.verbatim)]

# 修复后（示意）
user_text = operation.verbatim
if canonical_requirements:
    extra = [r.verbatim for r in canonical_requirements
             if r.verbatim not in operation.verbatim]      # 去重，避免重复喂
    if extra:
        user_text += "。要求：" + "；".join(extra)
return [Message(role="system", content="\n".join(parts)),
        Message(role="user", content=user_text)]
```

走真机用例：

```
现在：  完善我的引言部分
修复后：完善我的引言部分。要求：参考文献10；英文8；中文2
```

两个信号从此一致：schema 有配额字段，user 消息有配额文本。

**为什么放 user 消息而不是继续留在 system**：现状已经把要求列在 system 里
（「该操作已识别出以下要求，请据此一并填充对应槽位」），模型仍然没填
——因为 user 消息与槽位 description 都在说「用户没提配额」。
**同一信息放在不同位置，权重不同。** 用户说了什么，应当出现在 user 消息里。

**被否决的备选**

- **给 `RawOperation` 新增 `constraints` 字段。** 已有 `requirement_ids` 足够；
  且 `Operation.constraints` 是被**刻意删除**过的（`contracts.py` 记着：
  反规范化副本「生成时算一次、之后不更新」，澄清回填后陈旧，曾导致确认摘要
  显示「目标=空」）。同名字段会误导后来人，第二真相源迟早回来
- **把原始整句传给抽取器。** 违反验收 2.9，且会让 Property 4 从「可收紧」变成
  「必然破坏」
- **前一版的区间包含归属。** 见 Overview 的作废理由

**降级规则（不得倒退）**

| 情况 | 处置 |
|---|---|
| `requirement_ids` 非空 | 按归属过滤（收紧隔离） |
| 单操作 + `requirement_ids` 空 | 全部要求归它（无歧义） |
| 多操作 + `requirement_ids` 空 | 退回现状（列全部），**并计数告警** |

第三行是刻意保守：宁可维持现状的交叉污染风险，也不因 analyzer 少填一个字段
而让抽取器什么都看不到。告警计数使这种情况可观测，不静默。

### 决策 2：空容器判定（独立的第二处缺陷，与路由无关）

`isBorne` 要求「有取值」。`{"filters": [], "constraints": []}` 语义上等于
「没有配额」，不应算有取值——否则验证器会对空壳返回 true，报「已修复」而实际没配额。

**通用规则，不是 `citation_quota` 特判**：

```pascal
FUNCTION effective_status(slot_spec, raw_payload, value)
  OUTPUT: FieldStatus
  IF value IS NULL THEN RETURN USER_ABSENT
  IF is_empty_container(value) THEN
    // 区分「模型没提」与「模型试了但产出空壳」：看键是否出现在 payload 里
    IF slot_spec.name IN KEYS(raw_payload) AND slot_spec.clarify_if_user_invalid
    THEN RETURN INVALID          // 模型尝试过 → 走澄清（验收 2.6）
    ELSE RETURN USER_ABSENT      // 模型未提 → PlanBuilder 填默认
    END IF
  END IF
  RETURN PRESENT
END FUNCTION

FUNCTION is_empty_container(v)
  IF v IS list OR v IS tuple THEN RETURN LENGTH(v) = 0
  IF v IS dict THEN RETURN ALL(is_empty_container(x) OR x IS NULL FOR x IN VALUES(v))
  RETURN false
END FUNCTION
```

对既有 `array` 槽位（`constraints` / `presets` / `subsections`）与现状同结果：
空列表 → `user_absent` → PlanBuilder 填 `default_factory` 的 `[]`。

**例外须核对**：`FORMAT_DOCX_SPEC.slots["operations"]` 是
`required=True, must_ask=True`。空列表在新规则下变 `user_absent` → 进
`missing_fields` → 触发澄清。这是**行为变化**，方向正确
（「排版但一条操作都没给」本该追问）。实现时须确认是否有既有测试锁定旧行为；
若有，该测试锁的是缺陷，应更新并留注释。

### 决策 3：不新增要求分类（沿用前一版裁决）

`kind_of(r)` 与 `SlotSpec.bearable_kinds` **不实现**。需求文档的 `isBearable`
读作意图声明，而非实现指引。

理由：**`isBearable` 只在验证时需要，运行时不需要。** 运行时不必知道
「参考文献10 属于配额类」，只需知道「这段文字属于这个操作」，剩下交给槽位说明匹配。

验证时的地面真值**已存在**：`golden.json` 的 `expected_unmapped` 是人工标注，
`scorer.py` 的 `unmapped_spurious` / `unmapped_missing` 是对应指标。

**否决理由（最硬的一条）**：`AtomicRequirement.category` 当前是**自由文本**
（`category=str(r.get("category", ""))`），与 backlog F-3 的 `section_purpose`
是同一陷阱——那次教训是「LLM 填自由文本 + 程序做精确匹配 = 静默失效，
22 个测试没抓到，因为测试喂的是实践中不存在的合成字符串」。

### 决策 4：不新增覆盖检查指标（对前一版的进一步化简）

前一版打算把 offset 覆盖检查降级为验证时指标。**本版连这个也不做。**

理由：**`unmapped_spurious` 已经能回答同一个问题。** 若路由修好，配额被认领
⇒ 该指标为 0；若 analyzer 漏填 `requirement_ids` 导致配额仍落 unmapped
⇒ 该指标 > 0。残余遗漏率由已有指标度量，无需新建。

**净效果：新增声明表 0 张，新增指标 0 个，新增模块 0 个。**

### 缓解措施（改善概率，不改变最坏情况）

刻意与上述程序改动分列。**不得用其效果论证不变量已满足。**

1. `operation_analyzer` 的 `verbatim` 字段 description 补入**正向数量要求**的例子
   ——现状举例全是禁止类（「别改数字」「保留引用」「不超过 300 字」），
   而引用配额是正向要求（「要引 10 篇」），注意力偏置是本次被裁掉的直接诱因
2. `requirement_ids` 字段的 description 从「本操作覆盖的 requirement 的 id」
   加强为明示「**该操作的全部修饰语（约束、配额、参数）对应的 id 都要列进来**」
   ——这个字段现在从「装饰性」变成「承载性」，说明必须跟上

### 需要改动的模块

| 模块 | 改动 | 风险 |
|---|---|---|
| `tasking/planning_loop.py` | `_understand` 按归属过滤 + `requirements_for` 纯函数 | 低 |
| `tasking/extractors/base.py` | `_build_tool_messages` 组装 user 文本；`_coerce_slot` 空容器判定 | **中**，五意图公共路径 |
| `tasking/operation_analyzer.py` | 两处 description（缓解措施） | 低 |
| `application/console_interactor.py` | `resolve_unmapped` 文案（可选） | 低 |

**4 个文件，零新模块。** 对比前一版的 9 个文件 + 1 个新模块。

`resolve_unmapped` 文案：现状是刚改的保守版（「可能它已被其他要求涵盖」）。
路由修好后这句仍然成立（analyzer 漏填时确有此情况），故**本 spec 不改**，
留待残余遗漏率数据出来后再定。

### 向后兼容核对

- **无数据结构变更**（`requirement_ids` 已存在且已填充）⇒ `Task_Plan.schema_version`
  **不需要** bump
- **`facts.json` schema v4** ⇒ 规划层不落 facts，不受影响
- **checkpoint / `task_fingerprint`** ⇒ 属文献检索路径，规划层不参与其计算。
  **实现时必须实测确认**——backlog F-3 的教训是「以为不影响」与「验证过不影响」
  是两件事
- **`_infer_consumption` 保留不动** ⇒ 它处理「槽位已有取值、模型未自报
  `requirement_ids`」这个正交场景。已知它有一处潜伏失配（`target_ext` 的
  normalizer 把 `Word` 归一成 `docx` 后，requirement 原话 `转换成 Word`
  子串匹配不上，能否对上取决于模型是否顺手填了 `verbatim`）。
  **记入 backlog，不在本 spec 修**——它属于「每槽位 verbatim 必填」那个重构的范围

### 不得回归的既有纪律（实现时逐条自检）

1. `build_tool_schema` 的 `parameters.required` **恒为空**
2. `analyze()` 保持**消息的纯函数**：不接收文档状态
3. 抽取器**不填默认值**：默认值由 PlanBuilder 按 IntentSpec 填、标 `source=defaulted`
4. **零静默丢失**：`silent_loss` 恒 0
5. **D0 保序**：操作顺序 == 用户表达顺序
6. **记账规则不动**：只有落进 `SlotValue.requirement_ids` 才算 mapped。
   路由用 `raw.requirement_ids`，记账仍只认槽位——两件事分开
7. 三条反向约束（2.9 整句下传 / 2.10 单槽位特判 / 2.11 事后补记账）不得被绕过

## Testing Strategy

### 确定性单元测试（无 LLM）

| 性质 | 断言方式 |
|---|---|
| `requirements_for` 按 id 过滤 | 显式 fixture，两操作各持不同 id |
| 幻觉 id 被过滤 | `requirement_ids` 含不存在的 id → 忽略，不污染 |
| 单操作 + 空 `requirement_ids` | 全部要求归它 |
| 多操作 + 空 `requirement_ids` | 退回列全部 + 告警计数（不倒退） |
| **归属要求进入视野**（Property 1） | 断言每条归属要求的原话是 user 文本的子串 |
| **多操作隔离**（Property 4） | 断言不属于本操作的要求原话**不**在视野里 |
| user 文本去重 | 要求原话已在 verbatim 内时不重复追加 |
| 空容器 → user_absent / INVALID | 表驱动覆盖 dict / list / 嵌套 |
| `format_docx.operations` 空列表 | 变 `missing_fields` → 触发澄清（行为变化，须显式锁定） |
| 账本不变量（Property 3） | `silent_loss == 0` |
| 保持性（Property 2） | mock LLM 同脚本，按 `normalize` 口径逐字段比对 |

### 真实 LLM golden 评测

指标读法：

| 指标 | 本 spec 的读法 |
|---|---|
| `unmapped_spurious_total` | **> 0 ⇒ 缺陷仍在**（含 analyzer 漏填 `requirement_ids` 的残余情况） |
| `unmapped_missing_total` | **> 0 ⇒ 修过头**（做不到的事被静默接受） |
| `silent_loss_total` | 必须恒 0 |
| `requirement_miss_rate` | 分离信号：analyzer 漏识别，不属本 spec |

**基线要求（本 spec 的第一个任务，不是收尾）**：
修复**前**跑一次 golden runner 并归档。修复前基线尚未跑过——
没有它就无法证明「改好了」而不是「改动了」。

### 端到端冒烟（一条，进 CI）

真机原话 → 计划里出现引用配额 → 0 次 unmapped 追问。
用真实措辞，不用合成句——backlog 第七节反复出现的教训是
「测试验证了函数逻辑对，但没验证真实数据能流到这个函数」。

### 验收的最终判据

**用户能用真机原话把引言写出来，且引用数满足配额。** 这是本 spec 存在的理由，
其余指标都是它的代理信号。

## 已知风险

1. **analyzer 可能漏填 `requirement_ids`。** 该字段此前是「装饰性」的
   （填了也没人读），现在变成「承载性」，其填充质量从未被度量过。
   若漏填率高，修复效果受限。
   **观测点**：多操作退回分支的告警计数 + `unmapped_spurious`。
   **降级已定**：退回现状而非更差（见决策 1 降级规则表）

2. **`extractors/base.py` 改动面广**（五意图公共路径）。
   保持性检查必须覆盖 golden 全部非配额用例，优先用确定性 mock 测试锁住

3. **user 消息文本拼接可能影响其他槽位的抽取。** 追加「。要求：…」会改变
   模型看到的输入形状，理论上可能影响 `direction` / `details` 等槽位的取值。
   这是 Property 2 保持性检查要覆盖的主要面
