# Bugfix Requirements Document

## Introduction

用户在真机（2026-08-01，active 模式）用一句话同时说了「完善引言」和「参考文献 10、英文 8、中文 2」。
系统只安排了一个 `rewrite` 操作，**三条引用配额要求全部落进 unmapped**，被逐条追问后忽略；
最终计划里没有 `citation_quota` 槽，若继续执行会产出一篇零引用的引言。

缺陷的性质不是「记账漏项」，而是**配额从未被抽取**：理解链路在 `operation_analyzer` 切片时
就把配额文本裁掉了，抽取器看到的用户原话里一个数字都没有，于是按槽位说明「没提就不要给」
正确地省略了 `citation_quota`。

本 spec 要修的是一类**约束保真度（constraint fidelity）**问题——「约束保真度」指
用户表达出的、系统**有槽位可承载**的每一条修饰性要求（约束/配额/参数），
必须在最终计划里有一个 `SlotValue` 真正承载它的取值，而不是变成一次追问或一次静默丢失。
`citation_quota` 只是第一个暴露出来的实例。

### 对 `docs/backlog.md` 第十一节 X-8 的更正

X-8 当时的因果分析基于不完整日志，**结论有误**：该节写「配额槽认领了『英文8』『中文2』
却漏报『参考文献10』，纯记账漏项」。真机完整日志显示三条配额**一条都没被认领**，
`citation_quota` 自始至终是 `user_absent`、没有取值。因此：

- X-8 的「让 `SlotValue` 带 offset、不靠 LLM 自报 requirement_ids」这一修复方向**不解决本缺陷**
  （没有取值的槽位不在任何匹配候选里）；
- X-8 已完成的部分（把 `expected_unmapped` 接成 `unmapped_missing` / `unmapped_spurious` 活指标、
  补 8 条真实形态 golden）仍然有效，且 `unmapped_spurious` 正是本缺陷的可测信号。
- backlog 后续按此更正同步。

### 复现（真机原始记录，非合成例子）

用户输入：

```
我目前小论文只写了一个框架，我现在需要去写我的引言部分，你能帮我完善我的引言部分吗，要求参考文献10，英文8，中文2
```

系统输出：

```
有一条要求我没能安排进任何操作：「参考文献10」
怎么处理？(1=补充说明 / 2=忽略这条 / 3=取消本次)
> 2
有一条要求我没能安排进任何操作：「英文8」          → 2
有一条要求我没能安排进任何操作：「中文2」          → 2

计划包含 1 个操作（按执行序）：
  1. rewrite，目标=引言部分，目标描述=完善引言部分
执行这个计划吗？
```

对应的 golden 用例：`tests/eval/intent_planning/golden.json` 中
`real_quota_rambling_intro`（`expected_unmapped: []`，即三条配额都必须被槽位认领）。

## Bug Analysis

### 缺陷链路（已定位，作为需求的事实前提）

1. `operation_analyzer.analyze()` 产出 `RawOperation(intent=rewrite, verbatim="完善我的引言部分")`
   与三条独立 `AtomicRequirement`：`参考文献10` / `英文8` / `中文2`。
2. `extractors/base.py::PreExtractor._build_tool_messages` 发给抽取器的 user 消息是
   `operation.verbatim`（切片），不是原始整句。抽取器视野内没有任何数字。
3. `CITATION_QUOTA_SLOT_SPEC.description` 要求「仅当用户明确提出篇数/语言配额时给出；没提就不要给」，
   且 `build_tool_schema` 的 `required` 恒为空 —— 从模型视角，**省略 `citation_quota` 是正确行为**。
4. 三条 requirement 只以背景列表形式出现在 system 消息里；user 消息与槽位 description
   都在说「用户没提配额」。信号冲突，模型选了后者。
5. `_infer_consumption()` 无法兜底：它只对**已有取值**的槽位做文本双向子串匹配，
   `user_absent` 且无取值的 `citation_quota` 不在候选集内。
6. `plan_builder._build_dispositions()` 于是把三条判为 `UNMAPPED` → 三次追问。

两个加剧因素（说明这不是偶发抖动）：

- `verbatim` 字段 description 的举例**全是禁止类约束**（「别改数字」「保留引用」「不超过 300 字」）。
  引用配额是**正向数量要求**（「要引 10 篇」），不在例子的语义覆盖内，注意力偏置导致被裁掉。
- 「逐字连续摘取」与真实句式冲突：要同时覆盖动作与配额，连续片段必须包含
  「你能帮我…吗，要求…」这类客套与转折，模型倾向只摘干净的动作短语。

设计文档预见到了裁切风险（`verbatim` description 写着「宁长勿短……被裁掉的内容就等于丢失」），
但**只用一句 prompt 指令去防，程序侧零兜底**。

### 缺陷条件 C(X) 与验证属性

**输入 X** 定义为一次理解请求：`X = (message, current_sections, current_format)`。
`F(X)` 为修复前的理解链路产出的 `Task_Plan`，`F'(X)` 为修复后的产出。

判定 C(X) 需要两个可判定谓词。

```pascal
// 谓词一：requirement r 在计划 P 中是否被真正承载
FUNCTION isBorne(r, P)
  INPUT: r of type AtomicRequirement, P of type Task_Plan
  OUTPUT: boolean

  FOR EACH op IN P.operations DO
    FOR EACH (name, sv) IN op.slots DO
      IF r.requirement_id IN sv.requirement_ids
         AND sv.field_status IN {PRESENT, AMBIGUOUS, INVALID}
         AND sv.value IS NOT NULL
         AND spec_for(op.intent).slots[name].execution_binding IS NOT NULL
      THEN RETURN true
    END FOR
  END FOR
  RETURN false
END FUNCTION
```

```pascal
// 谓词二：requirement r 在计划 P 的操作集合上是否「有处可承载」
// 这是本 spec 的核心难点：必须区分「可承载但没承载」与「本就无处承载」。
// 判据：r 的要求类（requirement kind）落在某个 in-spec、有 execution_binding 的
// 槽位所声明的可承载要求类集合内。要求类由 analyzer 在识别 requirement 时标注，
// 槽位可承载要求类由 SlotSpec 声明——两侧都是静态声明，故该谓词是确定性可判定的，
// 不依赖 LLM 二次判断。
FUNCTION isBearable(r, P)
  INPUT: r of type AtomicRequirement, P of type Task_Plan
  OUTPUT: boolean

  FOR EACH op IN P.operations DO
    FOR EACH (name, slot_spec) IN spec_for(op.intent).slots DO
      IF slot_spec.execution_binding IS NOT NULL
         AND kind_of(r) IN slot_spec.bearable_kinds
      THEN RETURN true
    END FOR
  END FOR
  RETURN false
END FUNCTION
```

```pascal
// 缺陷条件：存在一条「有处可承载却没被承载」的 requirement
FUNCTION isBugCondition(X)
  INPUT: X of type UnderstandRequest
  OUTPUT: boolean

  P ← F(X)
  RETURN EXISTS r IN P.canonical_requirements SUCH THAT
           isBearable(r, P) AND NOT isBorne(r, P)
END FUNCTION
```

对真机用例：`kind_of("参考文献10") = citation_quota`，`REWRITE_SPEC.slots["citation_quota"]`
有 `execution_binding` 且可承载该类 ⇒ `isBearable = true`；而 `citation_quota` 为
`user_absent`、无取值 ⇒ `isBorne = false`。故 `isBugCondition(X) = true`。

对 `polish_with_unmapped`（「顺便帮我订一张去北京的机票」）、`illegal_translate`、
`illegal_plagiarism_check`：`kind_of` 不落在任何 in-spec 槽位的可承载类内 ⇒
`isBearable = false` ⇒ `¬C(X)`，落 unmapped 是**正确行为**。

```pascal
// Property: Fix Checking —— 修复后所有可承载要求都被承载
FOR ALL X WHERE isBugCondition(X) DO
  P' ← F'(X)
  FOR EACH r IN P'.canonical_requirements DO
    ASSERT (NOT isBearable(r, P')) OR isBorne(r, P')
  END FOR
  ASSERT COUNT(d IN P'.dispositions WHERE d.status = UNMAPPED
               AND isBearable(d.requirement, P')) = 0
END FOR
```

```pascal
// Property: Preservation Checking —— 非缺陷输入上行为逐字不变
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)     // 比较口径：操作序列、槽位取值与状态、dispositions、
                          // unmapped_requirements、missing_fields（忽略随机 id）
END FOR
```

**golden 评测上的可测代理信号**：`isBugCondition(X)` 在真实 LLM 上的外部观测量即
`scorer.py` 的 `unmapped_spurious`（人工标注 `expected_unmapped` 不含该条却落了 unmapped）。
`unmapped_spurious_total > 0` ⇒ 缺陷仍在；`unmapped_missing_total > 0` ⇒ 修复过头，
把做不到的事静默接受了。

### Current Behavior (Defect)

1.1 WHEN 用户一句话同时表达操作动词与引用配额（如真机原话「…完善我的引言部分吗，要求参考文献10，英文8，中文2」）THEN `operation_analyzer` 产出的 `RawOperation.verbatim` 只含动作短语「完善我的引言部分」，三条配额文本被裁在 verbatim 之外

1.2 WHEN 抽取器处理该 operation THEN 它收到的 user 消息只有 `operation.verbatim` 这一切片，视野内没有任何配额数字，`citation_quota` 被记为 `user_absent`

1.3 WHEN `citation_quota` 为 `user_absent` 且无取值 THEN `_infer_consumption()` 不把该槽列入文本匹配候选，三条配额 requirement 无任何槽位认领

1.4 WHEN `plan_builder._build_dispositions()` 结算去向 THEN 三条配额被判为 `UNMAPPED`，用户被逐条追问「有一条要求我没能安排进任何操作」

1.5 WHEN 用户对三次追问都选择「忽略这条」THEN 计划里不含 `citation_quota`，执行阶段会产出一篇零引用的引言，与用户明确的「10 篇（英 8 / 中 2）」直接相悖

1.6 WHEN 用户表达的是**正向数量要求**（「要引 10 篇」）而非禁止类约束（「别改数字」）THEN 由于 `verbatim` 字段 description 的举例只覆盖禁止类，正向数量要求被系统性裁掉，而非偶发抖动

1.7 WHEN 用户改用规范措辞（golden `real_quota_explicit_sentence`：「需要引用 10 篇参考文献，其中英文 8 篇、中文 2 篇」）THEN 配额是否被保留仍取决于模型对连续片段的取舍，程序侧没有任何保障

### Expected Behavior (Correct)

**修复性要求**

2.1 WHEN 用户一句话同时表达操作动词与引用配额 THEN 系统 SHALL 让承载配额的槽位（`citation_quota`）拿到取值，且该操作的三条配额 requirement SHALL 全部被该槽位认领（`isBorne = true`）

2.2 WHEN 任一 requirement 满足 `isBearable(r, P)` THEN 系统 SHALL 保证 `isBorne(r, P)` 成立，即计划中 SHALL NOT 出现「有处可承载却落 unmapped」的 requirement

2.3 WHEN 复现输入为真机原话 THEN 系统 SHALL 产出 0 次 unmapped 追问，并 SHALL 在计划中呈现引用配额（总数 10、英文 8、中文 2）

2.4 WHERE 抽取器接收操作文本 THEN 该文本 SHALL 覆盖该操作的全部修饰语，且系统 SHALL 提供**程序侧可验证的完整性契约**——即存在一个确定性断言，可判定「各操作 verbatim 的并集 ∪ unmapped_text」是否覆盖原消息的全部实义片段；覆盖不全 SHALL 被判为缺陷而非静默通过

2.5 WHEN `verbatim` 完整性契约被违反（有实义片段既不在任何操作 verbatim 内、也不在 unmapped_text 内）THEN 系统 SHALL 使该违反可观测（可断言、可计量），SHALL NOT 只依赖 prompt 指令预防

2.6 IF 用户确实表达了配额语义但取值无法解析或含糊 THEN 系统 SHALL 按 `clarify_if_user_invalid` 追问澄清，SHALL NOT 静默丢弃，也 SHALL NOT 回退成 `user_absent`

2.7 WHEN 要求类为跨操作修饰语（引用配额、全局风格等）THEN 系统 SHALL 有明确且唯一的归属规则（挂到具体操作或提升为计划级），该规则 SHALL 对所有跨操作修饰语一致适用

2.8 WHERE 新增一种跨操作修饰语类型（如「全文统一用英式拼写」）THEN 使其被正确承载 SHALL 只需声明式改动（新增槽位声明 / 要求类映射），SHALL NOT 需要修改 analyzer 或抽取器的控制流

**反向约束（明确排除的三个廉价修法）**

2.9 WHERE 实现修复 THEN 系统 SHALL NOT 把原始整句无差别传给每一个抽取器；理由：在多操作场景下会造成约束交叉污染（见 3.3），把一个确定的丢失换成一个不确定的错配

2.10 WHERE 实现修复 THEN 系统 SHALL NOT 为 `citation_quota` 添加「总是看整句」的单槽位特判；理由：一个槽位一个特例不可扩展，下一个跨操作修饰语又要加一个特例

2.11 WHERE 实现修复 THEN 系统 SHALL NOT 通过「事后按 requirement 补记账」来消除 unmapped 提示；理由：配额是真的没被抽取出来，不是抽出来没记账，补记账会把「烦人的提示」变成「静默丢失」，比现状更糟，并直接违反 3.6

### Unchanged Behavior (Regression Prevention)

3.1 WHERE `build_tool_schema` 生成 function schema THEN 系统 SHALL CONTINUE TO 令 `parameters.required` 恒为空；schema 层 required 会禁止模型省略、逼它为没说的字段编造取值，缺口就永远不会暴露给 Clarifier，整套澄清机制被架空

3.2 WHERE `operation_analyzer.analyze()` 的签名与语义 THEN 系统 SHALL CONTINUE TO 保持其为「消息的纯函数」——不接收文档状态；`current_sections` SHALL CONTINUE TO 只用于 `plan_validator` 的校验投影，绝不参与生成

3.3 WHEN 一条消息含多个操作（如「先润色引言，别改公式，再改写结论」）THEN 系统 SHALL CONTINUE TO 保证各操作的抽取器互不看见、互不抢占对方的约束，即 A 操作的约束 SHALL NOT 出现在 B 操作的槽位里

3.4 WHEN 用户提出系统真正做不到的要求（golden `polish_with_unmapped` 的「顺便帮我订一张去北京的机票」、`illegal_translate`、`illegal_plagiarism_check`）THEN 系统 SHALL CONTINUE TO 将其落入 unmapped 并追问，SHALL NOT 因本次修复而被强行映射到最接近的槽位

3.5 WHERE 抽取器产出槽位状态 THEN 系统 SHALL CONTINUE TO 只产 `present` / `user_absent` / `ambiguous` / `invalid` / `unavailable`，默认值 SHALL CONTINUE TO 由 PlanBuilder 按 IntentSpec 填入并标 `source=defaulted`

3.6 WHERE requirement 去向结算 THEN 系统 SHALL CONTINUE TO 维持零静默丢失（D4）：每条 requirement 必须「被某槽消费」或「进 unmapped」，二者皆非即静默丢失；本次修复 SHALL NOT 为了消掉追问而让 requirement 凭空消失（`silent_loss` SHALL CONTINUE TO 恒为 0）

3.7 WHEN 保序不变量（D0）被检验 THEN 系统 SHALL CONTINUE TO 令操作顺序等于用户表达顺序，重排只进 `reorder_suggestions`

3.8 WHEN 对 golden 集中所有非配额用例（`polish_*` / `constraint_*` / `multi_*` / `convert_*` / `format_*` / `rewrite_*`）求值 THEN 系统 SHALL CONTINUE TO 产出与修复前一致的操作序列、约束召回与 unmapped 账本（`unmapped_missing_total` SHALL CONTINUE TO 为 0）

### 开放决策点（需在设计阶段裁决，本节不含设计结论）

1. `RawOperation.verbatim` 的语义契约究竟是什么：是「该操作的完整可执行文本」还是「定位锚点」；
   2.4 要求的完整性断言以什么为实义片段口径（非停用词切片 / 数字与量词 / 其他）。
2. 跨操作修饰语的归属：挂到单个操作，还是提升为计划级对象；单操作与多操作场景是否同一规则。
3. 正向数量要求 vs 禁止类约束是否需要在 analyzer 层显式分类（即 `kind_of(r)` 的产出方式与取值集合），
   以及该分类是否就是 `AtomicRequirement.category` 的扩展。
4. 嵌套结构槽位（`json_schema_override`，如 `RAW_QUOTA_JSON_SCHEMA`）的抽取契约是否需与标量槽位不同——
   尤其 `isBorne` 中「有取值」对嵌套对象的判定（空 `filters`/`constraints` 是否算有取值）。
5. 验证分层：哪些性质能用确定性单元测试覆盖（`isBearable`/`isBorne` 谓词、完整性断言、
   多操作隔离、账本不变量），哪些**只能**靠真实 LLM 的 golden 评测
   （`tests/eval/intent_planning/`，指标 `unmapped_spurious` / `unmapped_missing`）；
   golden runner 需在修复前后各跑一次并归档基线。
