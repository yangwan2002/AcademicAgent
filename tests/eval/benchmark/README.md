# AcademicAgent Benchmark v1

统一比较代码、模型和 Prompt 变更对真实任务结果的影响。**这是开发 benchmark，不是已通过人工校准的正式发布认证。** 默认自检不联网、不读真实模型响应，也不能作为质量基线。

## 当前覆盖

| Suite | 样本数 | 执行路径 | 评分 |
|---|---:|---|---|
| planning | 41 | production Analyzer → Extractor → PlanBuilder | 意图序列、目标、约束/要求捕获、unmapped、silent loss、已有澄清标签 |
| ask | 10 | TurnService → Planning → AskLoop → 实际只读工具 | 所有必要事实、禁止内容、工具选择/参数、只读不变量、独立 Judge |
| editing | 10 | TurnService → Planning → PlanExecutor → polish → 磁盘工作副本 | 章节结构、修改范围、数字/引用/公式、实际修改、独立 Judge |
| relevance | 40 | production RelevanceJudge（被测 writer 模型） | 三分类准确率、接受精确率、接受召回率、误接受率 |

总计 **101 个案例**。编辑包括 2 个双章节顺序任务。问答/编辑使用一篇完全合成的固定英文 TeX 文稿，中英文指令覆盖同一文档家族；**不是 20 篇独立文档，也不是留出集**。目前不覆盖格式转换、新增章节、开放文献搜索或完整引用写作链路。

相关性沿用 `../relevance_batch/manifest.json`，40 条标签仍是待人工复核草案。这里评估候选分类，**不声称是搜索引擎 Recall@K / nDCG@K**。原有 batch consistency / B2 性能 gate 保持独立，未混入当前总分。

## 安装与运行

在仓库根目录执行（Python >=3.10）：

```bash
pip install -e ".[all,dev]"
python -m tests.eval.benchmark validate
python -m tests.eval.benchmark run --smoke
```

`--smoke` 使用明确标记的协议桩：问答/相关性会读取参考标签，编辑返回脚本化文本，规划故意返回空计划以验证失败路径。结果显示 `protocol_smoke`，Quality Gate 永远为 `INCONCLUSIVE`。命令返回 0 只表示脚本完整运行且没有执行器/评分器异常，**不表示模型得分合格**。`gate` 子命令对这样的报告仍返回 2。

真实运行沿用项目 `.env` 或进程环境变量：

```text
PAPER_LLM=<provider>
PAPER_LLM_MODEL=<explicit model version>
PAPER_BASE_URL=<optional endpoint>
PAPER_KEY_ENV=<environment variable containing writer API key>
PAPER_REVIEWER_LLM=<judge provider>
PAPER_REVIEWER_LLM_MODEL=<a different judge model>
PAPER_REVIEWER_BASE_URL=<optional judge endpoint>
PAPER_REVIEWER_KEY_ENV=<environment variable containing judge API key>
```

```bash
python -m tests.eval.benchmark run --live --allow-paid-provider
python -m tests.eval.benchmark run --live --allow-paid-provider --suites ask,editing --repeats 3
python -m tests.eval.benchmark run --smoke --limit 2
```

问答/编辑缺少独立 Judge 时结果为 `INCONCLUSIVE`，不会回退成“自己评自己且默认放行”。这里用不同的显式模型 ID 检查独立性；如果两个 ID 实际指向同一模型，需要配置者自行纠正。

`--limit` 是**每个 suite** 的样本上限，只适合调试，不允许通过完整数据集检查。每次输出到新的 `runs/benchmark/<run-id>/`；`--output` 可指定新目录，但拒绝覆盖已有目录。

## 报告与失败案例

```text
report.json             全部样本、身份、预算、逐项检查、汇总与 Gate
report.md               可读结果、失败原因
bad_cases.jsonl         所有非 PASS 样本，可用于排障和后续回归设计
trials/<suite>/<id>-N/
  input.tex             问答/编辑原始输入副本
  output.tex            编辑后的工作副本快照
  workspace/            该 trial 独占的应用会话目录
  responses.json        模型正文、请求工具及参数、Token 用量；不记录隐藏推理
  result.json           该 trial 原始结果
```

每条任务重新启动进程、重新建立会话，不共享上一条任务的修改、缓存或记忆。编辑测的是生产系统的“工作副本已落盘”契约，源文件应保持不变；执行器不会替模型额外调用保存再给它记功。所有任务的确认策略固定为接受计划，缺失字段回答空串，未映射要求取消，避免测试器临时补答案。

PASS 要求全部硬检查及三个 Judge 维度（correctness / completeness / clarity）都达到阈值；原样输出不能获得编辑成功。Judge 给出原文、指令、实际产物及明确 1–5 分规则；问答除关键词检查外还需判断否定、数字归属和事实依据。Judge 尚未经过独立人工校准，结果应抽查。未来改进 Judge 时需升级 rubric 版本并重跑基线。

工具指标只覆盖这些问答任务：允许先列章节，也允许直接读一个已标注的证据章节；计数对象是模型在 AskLoop **请求**的工具调用，报告另记实际执行工具名。不把规划结构化输出工具计入工具选择准确率，不把这组窄范围指标宣传为全系统工具准确率。

汇总按 suite 分列，并给出等权宏平均，防止 41 条规划案例掩盖 10 条编辑案例。所有已计划 trial 都进入分母，错误/超时不删除。另报每个案例全部重复运行均成功的比例。95% Wilson 区间仅用第一轮，避免把重复运行当独立样本；同文档相关性仍限制统计解释。P50/P95 为 worker 内执行时间，进程启动不计入正常样本耗时。

Token 只使用 provider 真正返回的数值；未知为 null，未配置价格所以美元成本为 null。不能把 logical call 数当物理 API 请求或费用。

## 预算与异常

默认串行运行，每个 trial 最多 24 次 writer 逻辑调用、2 次 Judge 调用；全局预算 3000 次，HTTP 超时 60 秒、进程硬超时 240 秒。参数：

```bash
python -m tests.eval.benchmark run --live --allow-paid-provider --max-calls 12 --max-total-calls 500 --request-timeout 30 --trial-timeout 120
```

关闭应用层 provider 自动重试，保留生产结构化协议兼容回退与最多一次编辑内容重试，全部计入逻辑调用。SDK 内部可能有额外重试。全局预算按下一条最大调用量预留，不够则标记 ERROR；被杀死的进程因调用数未知，按最大额度扣除。超时会终止子进程并记录失败，不会重跑到成功。当前支持逐条落报告，**不支持中断续跑**，不要复用部分报告冒充完整运行。

## 基线与 Quality Gate

`policy.json` 为待校准的开发阈值：各 suite 成功率最低 80%/85%、最多退化 5 个百分点、硬约束违反数 0、Judge 各维度至少 4、相关性 precision >=0.90 / recall >=0.80。零接受时 precision=null，召回为 0，不能拿到 100% 精确率通行证。

报告状态：

- `PASS`：评测有效且全部规则通过。
- `FAIL`：有效评测但绝对质量、硬约束或基线退化不合格。
- `INCONCLUSIVE`：自检、缺数据、Judge/Provider 异常、未完成标注、基线不兼容等；同时保留已发现的质量失败原因。

真实 `run` 和 `gate` 仅 PASS 返回 0，其余返回 2。执行器错误的 smoke 返回 1。

完成相关性双人复核和仲裁后，按 `../relevance_batch/README.md` 导出/导入为新 manifest，再执行：

```bash
python -m tests.eval.benchmark run --live --allow-paid-provider --relevance-manifest tests/eval/relevance_batch/manifest.adjudicated.json --output runs/benchmark/candidate-v1
python -m tests.eval.benchmark baseline --report runs/benchmark/candidate-v1/report.json --output tests/eval/benchmark/baselines/approved-v1.json
```

`baseline` 是操作者显式采纳报告的动作，仅接受通过绝对 Gate 的真实评测，且永不覆盖已有文件。仓库不预置伪造的全通过基线，现有旧 runner 的 baseline 也不改写。

```bash
python -m tests.eval.benchmark run --live --allow-paid-provider --relevance-manifest tests/eval/relevance_batch/manifest.adjudicated.json --baseline tests/eval/benchmark/baselines/approved-v1.json --require-baseline
python -m tests.eval.benchmark gate --report runs/benchmark/candidate-v2/report.json --baseline tests/eval/benchmark/baselines/approved-v1.json --require-baseline --expected-commit <release-commit-sha>
```

报告绑定 source hash、commit、工作区变更标志、harness/scorer hash、数据集哈希、模型 ID、端点哈希、Judge rubric、预算与 policy。比较允许 writer/代码变化，但数据集、执行协议、Judge、评分器版本不同会拒绝。改变预算/repeats/样本选择后也要使用对应基线。

`--expected-commit` 额外要求代码提交匹配、所记录的相关工作区为干净状态、四个 suite 全量运行。这是**开发质量检查的版本绑定**，不能替代正式生产发布所需的独立留出集、人工 Judge 校准和业务发布标准；当前数据明确记录这两项尚未完成。

## CI

`.github/workflows/benchmark.yml` 在 PR/main push 运行协议自检和关键验收测试，**没有自动付费计划任务**。手动 workflow_dispatch 勾选 live 后才调用真实模型。

在 GitHub `benchmark` Environment 配置：

- Secrets：`BENCHMARK_WRITER_KEY`、`BENCHMARK_JUDGE_KEY`。
- Variables：`BENCHMARK_LLM_PROVIDER`、`BENCHMARK_LLM_MODEL`、可选 `BENCHMARK_BASE_URL`；以及 `BENCHMARK_JUDGE_PROVIDER`、`BENCHMARK_JUDGE_MODEL`、可选 `BENCHMARK_JUDGE_BASE_URL`。

live 默认仍读取草案 manifest，因此会产生报告但被 Gate 拦住；选择已完成仲裁的 manifest 才满足标签条件。当前 live job 是绝对阈值开发检查，不自称含已批准基线的发布检查。基线建立后，将上面的 `--baseline --require-baseline` 接入 live job，并把相应检查设为必需项。正式发布任务还需依赖针对同一提交的 gate 成功，不能把被跳过的 live job 或 smoke 成功当成放行。

无论通过或失败，CI 都上传报告。此实现只新增本地 workflow 文件，未修改远端 Secrets、分支保护或发布配置。

## 扩展方法

1. 在 `fixtures/` 增加不同学科、语言、文档格式的固定材料；同文档变体留在同一数据划分。
2. 在 `tasks.json` 添加唯一 ID、输入、目标、必需事实/不变量；新能力先增加对应 adapter 和评分器。
3. 用人工确认的正确、错误及边界输出测试评分器；为 Judge 建立独立双人标注校准集。
4. 固定一个不用于调参的留出集；冻结新 dataset/rubric/policy 版本，再跑新基线。
5. 加入固定语料库及完整相关性标注后，再增加真正的检索排序指标；接入外部搜索时另列实时可用性，避免与离线回放混分。
