# Design — B：真实相关性评测（RelevanceJudge 的 Golden 跑分）

> 目的:为 A2a `RelevanceJudge` 建一套**离线可测的评测骨架** + **真机跑分 runner**,
> 度量相关性判定的真实质量。这是 A2d 无人监督（unsupervised）Active 上线的**前置门槛**
> ——评测达标前,写作前证据集确认恒 supervised（design-a2c / A2d 已如此）。
>
> 复用现有 `tests/eval/intent_planning/`（golden.json + scorer.py + runner.py）的成熟范式:
> **scorer 纯函数确定性、进默认 CI;runner 打真实模型、不进 CI**。

## 诚实边界（先说清楚）
- 本设计只交付**评测机制**:golden 数据 schema、确定性 scorer、真机 runner。
- **"评测达标"本身需要**：(a) 人工标注的 golden 集,(b) 真实 reviewer LLM 跑分。二者
  由你提供/运行——我无法凭空造标注,也不为准确率打包票。
- 我会附一小份**种子 golden**（少量、含中英/有无摘要/明显相关与不相关的对照）作为格式
  样例与管线自测用,**不足以作为上线判据**;真正门槛用你扩充后的集。

## 度量（design 依据:A2a Req 7 的判定纪律）

对每条 golden = `(criteria, candidate{title,abstract}, 人工标注 label{verdict,confidence?})`,
跑 `judge.assess(candidate, criteria)`,得 `RelevanceAssessment`,算:

1. **verdict_accuracy**：`assessment.verdict == label.verdict` 的比例（仅 status=SUCCESS）。
2. **三分类 P/R/F1 + 混淆矩阵**：over {relevant, weakly_relevant, unrelated}。
   相关性下游最关心 **relevant 的 precision**（误判为 relevant → 污染证据集）与
   **unrelated 的 recall**（漏拒无关 → 同样污染）。
3. **failure_not_verdict 不变量**（A2a 评审#1）：`status=ERROR` 的样本**绝不计入**任何
   verdict 命中;单独统计 `error_rate`。系统失败被伪装成 verdict 即判骨架 bug。
4. **title_only 保守性**（A2a 评审#5）：无摘要样本上,`verdict==relevant` 必须被降级为
   `weakly_relevant`（且不出现 relevant/high）。统计 `title_only_overclaim_count`（应为 0）。
5. **缓存键一致性**（承接上轮修的 seam bug）：同一 (criteria, candidate) 二次 assess
   必须命中缓存、结论一致;统计 `cache_consistency`（应 100%）。

聚合输出 `as_dict()`,`runner.py` 打印并写 `baseline.json`（与 intent_planning 一致）。

## 门槛建议（占位,最终由你定）
- 供讨论的初始门槛:`relevant_precision ≥ 0.85`、`unrelated_recall ≥ 0.85`、
  `error_rate ≤ 0.05`、`title_only_overclaim == 0`、`cache_consistency == 1.0`。
- 达标前:supervised 默认不变。达标后:才允许在 config 放开 unsupervised（另做,不在本期）。

## 结构（镜像 intent_planning）

`tests/eval/relevance/`：
- `golden.json`：`{"cases": [{ "id","criteria":{topic,...},"candidate":{title,abstract},
  "expected":{"verdict","confidence"?,"has_abstract"} }]}`。
- `scorer.py`（**纯函数、零 LLM、进 CI**）：
  - `load_golden(path) -> list[RelevanceGoldenCase]`
  - `score_case(case, assessment) -> RelevanceCaseScore`
  - `aggregate(scores) -> RelevanceMetrics`（含混淆矩阵 + 上述指标）
- `runner.py`（**打真实 reviewer LLM、不进 CI**）：
  - `run_all(cases, judge)`：对每条跑 `judge.assess`,scorer 打分聚合。
  - `main()`：从 `.env` + `build_reviewer_llm_provider`（缺省回退 writer）装配真实 judge,
    跑 golden,打印 + 写 `baseline.json`。
- 种子 `golden.json`：~8–10 条对照样本（中/英 × 有/无摘要 × 相关/弱相关/无关）。

## 测试（进默认 CI,全部离线）
`tests/test_relevance_eval.py`：
- 用**脚本化 judge**（stub reviewer LLM,按样本返回预设 verdict/ERROR）驱动 scorer,验证:
  - verdict_accuracy / P/R/F1 / 混淆矩阵算得对（构造已知输入对拍）。
  - **ERROR 样本不计入 verdict 命中**,单独进 error_rate。
  - title_only 样本模型回 relevant → 被判 overclaim（或按 A2a 已降级则 overclaim=0）。
  - 缓存一致性:同输入二次 assess 命中缓存、结论一致。
- 种子 golden 能被 `load_golden` 正确解析（schema 守门）。
- runner 的 `run_all` 能用脚本化 judge 跑通（不联网），产出 Metrics。

## 影响面 / 纪律
- **纯新增**:`tests/eval/relevance/*` + `tests/test_relevance_eval.py`。不改任何产品代码。
- scorer 零 LLM、确定性、进 CI;runner 打真实模型、`if __name__=="__main__"`,不进 CI。
- 不改 supervised 默认(本期只建评测,不放开 unsupervised)。
- 全套既有测试保持绿。

## 需要你拍板
1. **门槛数值**:上面那组(relevant_precision≥.85 等)是否合理?还是先只跑分不设硬门槛?
2. **种子 golden 规模**:我先放 ~8–10 条对照样例够吗?还是你想直接给一批真实标注?
3. **命名/落位**:放 `tests/eval/relevance/`,与 intent_planning 平级。可以?
