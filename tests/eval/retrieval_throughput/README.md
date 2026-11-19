# Retrieval throughput evaluation

Production has one `run_attempt`: verify candidates, review relevance, then reduce
results serially. Single-item and batched review are strategies inside that same
pipeline. The production entry and normal application composition reject the old
reorder opt-in; B2 now runs only through the explicitly imported offline experiment
in `experimental_attempt.py`.

```powershell
python tests/eval/retrieval_throughput/runner.py
python -m pytest tests/test_retrieval_throughput_baseline.py -q -p no:cacheprovider
python tests/eval/retrieval_throughput/b2_gate.py --write-report
```

`baseline.py` exercises the current production single-item strategy. Its
`build_single_item_snapshot()` retains the historical snapshot schema, fixture
identities and judge/cache keys so `legacy_golden.json` still proves semantic
continuity. The old algorithm itself is not retained or executed. UUIDs, timestamps
and elapsed wall time are excluded; outcomes, call counts, reference identities,
rejection reasons and cache behavior remain checked.

The paired replay runs `single_item`, `b1` (production batch strategy), and `b1_b2`
(experimental reorder) 30 times, plus the experimental verify-first reference.
It uses deterministic providers and simulated monotonic active time, without
network access or persistent raw query/title/abstract data. The experiment must
still match final dispositions and quotas while reducing strict verification calls.
The default production composition cannot activate it.

The frozen `benchmark_profile.json`, `baseline_report.json` and `b2_gate_report.json`
are historical evaluation assets. Their `legacy` label describes the old baseline;
the current report maps that label to the unified `single_item` strategy. New paired
reports use schema v2, write to `b2_experiment_report.json`, and list production and
experimental modes explicitly. The
engineering gate and human quality gate remain separate; historical reports do not
claim that human adjudication has happened.

The existing live dogfood log is incomplete and is not a valid latency baseline.
Live runs require explicit network/paid-reviewer opt-in and must not persist raw
research topics here.
