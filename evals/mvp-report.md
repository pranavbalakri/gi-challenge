# EnvMaker MVP evaluation report

- model: `gpt-4o-mini`
- prompt_version: `1`
- eval_root: `runs/eval/20260802-060818-d7c216ff`
- cells: 12

## Aggregate by variant

| variant | n | program execution rate | hard-valid rate | traversal success rate | mean repair turns | mean latency (s) |
| --- | --- | --- | --- | --- | --- | --- |
| loop | 6 | 0.83 | 0.33 | 0.33 | 6.83 | 14.25 |
| oneshot | 6 | 0.50 | 0.00 | 0.00 | n/a | 3.15 |

Means exclude provider_error/harness_error rows; turns count every provider turn including the initial code turn.
Loop-variant program execution is derived from observed compile events; a run that never compiled reports program=False.

## Per-run outcomes

| prompt_id | variant | seed | terminal_state | accepted | turns_used | wall_seconds | error |
| --- | --- | --- | --- | --- | --- | --- | --- |
| jungle_valley | oneshot | 7 | rejected_after_budget | False | 1 | 3.38 | no landmark probe available |
| jungle_valley | loop | 7 | rejected_after_budget | False | 8 | 14.25 | turn or wall budget exhausted |
| frozen_village | oneshot | 7 | rejected_after_budget | False | 1 | 2.91 | no landmark probe available |
| frozen_village | loop | 7 | rejected_after_budget | False | 8 | 15.45 | turn or wall budget exhausted |
| desert_canyon | oneshot | 7 | rejected_after_budget | False | 1 | 2.76 | static validation failed |
| desert_canyon | loop | 7 | rejected_after_budget | False | 8 | 17.28 | turn or wall budget exhausted |
| industrial_yard | oneshot | 7 | rejected_after_budget | False | 1 | 2.94 | static validation failed |
| industrial_yard | loop | 7 | accepted | True | 3 | 9.69 |  |
| island_settlement | oneshot | 7 | rejected_after_budget | False | 1 | 2.72 | static validation failed |
| island_settlement | loop | 7 | rejected_after_budget | False | 8 | 15.05 | turn or wall budget exhausted |
| maze_garden | oneshot | 7 | rejected_after_budget | False | 1 | 4.19 | no landmark probe available |
| maze_garden | loop | 7 | accepted | True | 6 | 13.79 |  |

## Failed examples

- `jungle_valley` / `oneshot` / seed 7: first failing stages `unknown` (run_dir=runs/eval/20260802-060818-d7c216ff/jungle_valley-oneshot-s7)
- `jungle_valley` / `loop` / seed 7: first failing stages `controller, navigation` (run_dir=runs/eval/20260802-060818-d7c216ff/jungle_valley-loop-s7)
- `frozen_village` / `oneshot` / seed 7: first failing stages `unknown` (run_dir=runs/eval/20260802-060818-d7c216ff/frozen_village-oneshot-s7)
- `frozen_village` / `loop` / seed 7: first failing stages `controller` (run_dir=runs/eval/20260802-060818-d7c216ff/frozen_village-loop-s7)
- `desert_canyon` / `oneshot` / seed 7: first failing stages `program` (run_dir=runs/eval/20260802-060818-d7c216ff/desert_canyon-oneshot-s7)
- `desert_canyon` / `loop` / seed 7: first failing stages `scene` (run_dir=runs/eval/20260802-060818-d7c216ff/desert_canyon-loop-s7)
- `industrial_yard` / `oneshot` / seed 7: first failing stages `program` (run_dir=runs/eval/20260802-060818-d7c216ff/industrial_yard-oneshot-s7)
- `island_settlement` / `oneshot` / seed 7: first failing stages `program` (run_dir=runs/eval/20260802-060818-d7c216ff/island_settlement-oneshot-s7)
- `island_settlement` / `loop` / seed 7: first failing stages `program` (run_dir=runs/eval/20260802-060818-d7c216ff/island_settlement-loop-s7)
- `maze_garden` / `oneshot` / seed 7: first failing stages `unknown` (run_dir=runs/eval/20260802-060818-d7c216ff/maze_garden-oneshot-s7)

Provider/harness errors are reported separately from hard-validation failures (0 system-error cell(s) in this run).

## Limitations

- Sample size: six prompts, one seed, two variants; rates are directional evidence for the loop-vs-oneshot comparison, not statistics.
- Base model: the default is weak (`gpt-4o-mini`) and acceptance is sensitive to harness affordances (nudges); the frozen same-model comparison is what the claim rests on.
- Budgets: 8 provider turns / 600 s wall per run; runs one repair short of acceptance count as rejected, and budgets are never extended.
- Strict baseline: the one-shot variant gets the identical prompt but no tool execution and no feedback; the measured gap is the value of typed feedback, not prompt engineering.
- Loop stage attribution derives from observed compile events in the runlog; a run that never re-compiled reports its last known stages.
- Prompt compliance is human-scored via the YAML checklists, not a hard validator stage; runtime scope is a single walkable plane with box/cylinder primitives, verified on macOS arm64.
