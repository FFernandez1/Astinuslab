# Bounded workflow benchmarks

`tools/benchmarks/benchmark_workflows.py` measures five small synthetic workflows
through existing production helpers. It does not start VMs, run forensic tools,
change product behavior or measure actual memory-dump analysis throughput.

## Run

Use an existing development Python environment with this repository's dependencies,
Node.js, installed Playwright and its existing Chromium browser. No tool is installed
by the runner. Linux cgroup v2 must expose a writable parent with `memory` and `pids`
already enabled. The runner creates a new private sibling cgroup; it never changes
an existing group/controller or moves the supervisor or existing processes. Missing
enforcement produces a recorded blocked result before workloads start.

From the checkout to be measured:

```bash
mkdir -p storage/next-tasks-evidence
/path/to/existing/venv/bin/python tools/benchmarks/benchmark_workflows.py \
  --output storage/next-tasks-evidence/workflows-01 \
  --node-modules /path/to/existing/checkout/node_modules
```

The output directory must not exist and its parent must be canonical, with no
symlink components. In a worktree, `--node-modules` can point at the original
checkout's installed dependencies. Worker imports explicitly prefer the current
checkout, including when the supplied venv has an editable installation elsewhere.
`--python` selects a different existing worker interpreter; results distinguish
supervisor and actual worker versions.

Defaults are one repetition, 8 MiB hash input, 1,000 top-level result rows,
nesting depth 8, one 256 KiB field and 100 retained runs. There are no warmups.

| Scenario | Actual code exercised | Validation and measurement |
| --- | --- | --- |
| `hash` | `forensics.vol3_support._sha256_file` | Known synthetic bytes/digest, immutable source identity; helper elapsed and bytes/s |
| `results` | `forensics.vol3_plugin_run._parse_json_rows_file` | Oversized/nested JSON, Depth column, 1,008 total / 1,000 retained rows, truncation and unchanged input |
| `history` | `forensics.api._recent_runs` | Real disposable database schema and 100 result/header pairs; 99 visible in order after hiding one; every source hash retained |
| `cancel` | `jobs.job_control.kill_job_subprocesses` and verified PID registration | Registered child exits, unregistered task-owned sentinel survives, registry removed; call-to-exit latency |
| `ui` | Real `/volatility` template and workbench JavaScript through Playwright | Synthetic API result, all 1,000 DOM rows and completed state; response delivery through DOM completion and two animation frames |

Select only a needed scenario with `--scenario results` (repeat the option to select
several). Other options are `--dump-mib`, `--rows`, `--depth`, `--item-kib`,
`--retained`, `--seconds` and `--repetitions`. Values are checked before creating
workload files or launching workers. A maximum of three repetitions is accepted
only when the combined conservative output estimate also fits. Do not repeat
successful scenarios to refresh evidence after an unrelated case fails; use a new
output directory and select only the affected case. Record correction attempts.

## Limits and outputs

Each scenario has a maximum 30-second supervisor deadline. Kernel cgroup limits
are 512 MiB for non-browser scenarios, 1 GiB for the entire UI tree, no swap, and
256 processes. The internal worker entry point requires a private inherited
handoff and verifies its actual new cgroup, limits, parent, workload and isolated
environment before importing the application. Calling `--worker` directly cannot
bypass the supervisor.

The 64 MiB aggregate task-output budget combines conservative generated-size
validation and a watchdog. It is **not a filesystem quota**: polling can observe
a small overshoot. Individual files also have a 64 MiB kernel limit; diagnostic
logs are capped at 1 MiB. Browser scratch symlinks are counted without following
their targets. Every process, directory and cgroup cleaned up belongs to this run.
A limit, failed validation, nonzero worker exit or cleanup failure causes a nonzero
runner exit and a failed/blocked report; it is never a successful measurement.

`preflight.json` and `results.json` record exact argv/cwd, source revision/dirty
state, benchmark source hashes, CPU/platform/affinity, RAM/disk availability,
load averages and CPU/memory/I/O pressure, Python/package/browser versions,
workload parameters and limits. Each scenario retains `input.json`, bounded logs,
`outcome.json` on success and `supervision.json`. Peak process-tree RSS is sampled
at approximately 25 ms and can miss short peaks. Kernel `memory.peak` is separately
recorded; it includes charged cache/kernel memory and is not an RSS measurement.
The supervisor is outside the measured/limited scenario tree.

Helper operation timing excludes fixture generation; supervised wall time includes
imports, generation and cleanup. Hash input is generated immediately beforehand
and is likely page-cached: its rate is not sustained physical-storage throughput.
History has an initialized schema but no corresponding Job/action ownership rows,
so it represents retained legacy headers, not a populated production database.
Cancellation is synthetic process control, not VM/capture cancellation. UI timing
includes response handling and paint scheduling, not production network latency.

## Actual forensic throughput remains unverified

A valid operator-supplied memory capture, exact matching symbols, a verified
Volatility executable and an independently known artifact are required. Record
input/tool/symbol hashes, tool version and provenance, kernel/build identity,
plugin/arguments, expected artifact, hardware, output bytes, elapsed time and
peak memory. The earlier 112-row parity evidence is not a new benchmark.

On an operator-authorized environment with these inputs and existing tools, this
is a concrete bounded direct PsList measurement (replace each `/operator/...` path
with the verified bundle). It is separate from the synthetic runner:

```bash
mkdir -m 700 /operator/results/pslist-01
sha256sum /operator/tools/vol /operator/capture/memory.lime \
  /operator/symbols/linux/matching.json > /operator/results/pslist-01/inputs.sha256
systemd-run --user --scope --property=MemoryMax=512M \
  --property=MemorySwapMax=0 --property=TasksMax=256 -- \
  /usr/bin/prlimit --fsize=67108864:67108864 -- \
  /usr/bin/time -v -o /operator/results/pslist-01/time.txt \
  /usr/bin/timeout --signal=TERM --kill-after=2s 30s \
  /operator/tools/vol -f /operator/capture/memory.lime \
  -s /operator/symbols -r json linux.pslist.PsList \
  > /operator/results/pslist-01/stdout.json \
  2> /operator/results/pslist-01/stderr.txt
```

Retain the exit status and independently check the expected artifact before making
any forensic-throughput claim. A timeout, dependency/symbol mismatch or memory
limit is a failed/unverified case. Reuse exactly the same tool, plugin, arguments,
dump and symbols for application/direct comparisons. Larger limits/workloads need
an explicit resource review; do not silently relax limits, substitute synthetic
success, install tools or accept licenses. No performance optimization is justified
until a reproducible bottleneck is measured.
