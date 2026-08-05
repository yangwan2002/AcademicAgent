# Relevance Batch Quality Gate

默认命令只校验冻结清单，不联网：

```powershell
python tests/eval/relevance_batch/runner.py
python tests/eval/relevance_batch/runner.py --protocol-smoke
```

`--protocol-smoke` 使用能读取草拟标签的协议桩，只验证 per-item、batch、candidate_key、窗口变形和指标聚合链路，报告固定为 `NOT_EVALUATED_PROTOCOL_FIXTURE`，不能作为质量结论。

真实模型评测必须显式执行：

```powershell
python tests/eval/relevance_batch/runner.py --live --allow-paid-provider --write-report
```

真实模式会原子写入 `progress_checkpoint.json`，并向 stderr 输出不含 query、标题或摘要的阶段进度。
若进程中断，可显式从已完成的逐篇/窗口结果恢复，避免重复付费调用：

```powershell
python tests/eval/relevance_batch/runner.py --live --allow-paid-provider --write-report --resume
```

checkpoint 会绑定 manifest/profile/provider/model/evaluation kind；身份不一致会拒绝恢复。报告同时区分
逻辑结构化调用、provider 尝试和兼容性重试开销。已发出但尚未来得及写 checkpoint 的在途请求无法恢复，
因此极端中断时最多可能重做当前一个窗口。

当前 40 条标签是待人工复核草案。每条至少由两名标注者独立复核并完成仲裁后，才可把 `label.status` 改为 `adjudicated` 并记录匿名 annotator ID。受控 dogfood 至少需要 40 条全部完成；无监督默认开启至少需要 200 条全部完成。不得用实现所依赖的同一模型生成标签后冒充人工金标准。

人工标注工作表可导出为 Excel 可直接打开的 UTF-8 CSV；完成双人标注和仲裁后，导入为一个新 manifest（不会覆盖草案）：

```powershell
python tests/eval/relevance_batch/annotation_tool.py export --output relevance_review.csv
python tests/eval/relevance_batch/annotation_tool.py import --sheet relevance_review.csv --output manifest.adjudicated.json
python tests/eval/relevance_batch/runner.py --manifest manifest.adjudicated.json --live --allow-paid-provider --write-report
```
