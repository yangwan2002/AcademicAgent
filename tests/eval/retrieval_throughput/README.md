# Retrieval Throughput Baseline

Task 1 freezes the current Legacy attempt semantics without network access:

```powershell
python tests/eval/retrieval_throughput/runner.py
python -m pytest tests/test_retrieval_throughput_baseline.py -q -p no:cacheprovider
```

`legacy_golden.json` stores both a canonical SHA-256 and a readable semantic summary. Volatile UUIDs,
timestamps and elapsed wall time are excluded explicitly by `baseline.py`; business outcomes, call counts,
reference identities, rejection reasons and cache behaviour remain frozen.

The existing live dogfood log is incomplete and is not treated as a valid latency baseline. A new live run
requires explicit network/paid-reviewer opt-in and must not persist the raw research topic in this directory.

Task 8 的 B2 离线配对 Gate：

```powershell
python tests/eval/retrieval_throughput/b2_gate.py --write-report
```

该命令按冻结 profile 配对运行 Legacy、B1、B1+B2 各30次，使用确定性 provider 与模拟
monotonic active time，不联网、不保存 query/title/abstract。工程 Gate 与人工质量发布 Gate 分开报告。
