---
name: performance-profiler
description: find performance risk indicators in a project (large files, sync I/O in hot paths, N+1-shaped query patterns, missing indexes) before optimizing — use when something is slow and you don't know where, or before a traffic spike
status: published
notes: ported from claude-skills engineering/performance-profiler (MIT); static heuristic scan, not a live profiler
---
# Performance profiler

**Measure first.** Never optimize on a hunch — profile, confirm the bottleneck, fix,
measure again to verify. "I think the N+1 query is slow, let me fix it" is backwards;
confirm it first.

Static scan for risk indicators (large files, sync I/O in request paths, likely N+1
patterns, missing pagination/limits):
```
python3 skills/performance-profiler/scripts/performance_profiler.py <path> --json
```
`--large-file-threshold-kb <n>` tunes what counts as a large file (default 512KB).

This is a **static heuristic scan**, not a live profiler — it flags where to look, not
proof of a bottleneck. For the real measurement, reach for the language-native tool
(flamegraph/py-spy/pprof, or an actual load test) and record before/after numbers:
P50/P95/P99 latency, RPS, error rate, memory. See [[debug-systematically]] for the
reproduce-first discipline that applies here too.
