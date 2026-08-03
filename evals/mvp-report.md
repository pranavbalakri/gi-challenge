# EnvMaker MVP evaluation report

- model: `gpt-5.4-mini`
- prompt_version: `1`
- note: historical run under authoring prompt v1. The current prompt is v2 (the bounded visual-extension layer landed after this evaluation); the runner now refuses to execute when the config's prompt_version does not match the imported `PROMPT_VERSION`, so future reports cannot be mislabeled.
- eval_root: `runs/eval/20260802-065054-14e3ae07`
- cells: 12

## Aggregate by variant

| variant | n | program execution rate | hard-valid rate | traversal success rate | mean repair turns | mean latency (s) |
| --- | --- | --- | --- | --- | --- | --- |
| loop | 6 | 1.00 | 0.67 | 0.67 | 12.00 | 30.53 |
| oneshot | 6 | 0.67 | 0.50 | 0.50 | n/a | 9.25 |

Means exclude provider_error/harness_error rows; turns count every provider turn including the initial code turn.
Loop-variant program execution is derived from observed compile events; a run that never compiled reports program=False.

## Per-run outcomes

| prompt_id | variant | seed | terminal_state | accepted | turns_used | wall_seconds | error |
| --- | --- | --- | --- | --- | --- | --- | --- |
| jungle_valley | oneshot | 7 | accepted | True | 1 | 16.07 |  |
| jungle_valley | loop | 7 | rejected_after_budget | False | 16 | 36.75 | turn or wall budget exhausted |
| frozen_village | oneshot | 7 | rejected_after_budget | False | 1 | 4.77 | oneshot contract violated: model called a tool |
| frozen_village | loop | 7 | accepted | True | 8 | 18.76 |  |
| desert_canyon | oneshot | 7 | rejected_after_budget | False | 1 | 5.41 | static validation failed |
| desert_canyon | loop | 7 | accepted | True | 9 | 25.09 |  |
| industrial_yard | oneshot | 7 | accepted | True | 1 | 9.18 |  |
| industrial_yard | loop | 7 | accepted | True | 10 | 25.77 |  |
| island_settlement | oneshot | 7 | accepted | True | 1 | 12.75 |  |
| island_settlement | loop | 7 | accepted | True | 13 | 33.05 |  |
| maze_garden | oneshot | 7 | rejected_after_budget | False | 1 | 7.32 | runtime validation failed |
| maze_garden | loop | 7 | rejected_after_budget | False | 16 | 43.73 | turn or wall budget exhausted |

## Failed examples

- `jungle_valley` / `loop` / seed 7: first failing stages `navigation` (run_dir=runs/eval/20260802-065054-14e3ae07/jungle_valley-loop-s7)
- `frozen_village` / `oneshot` / seed 7: first failing stages `unknown` (run_dir=runs/eval/20260802-065054-14e3ae07/frozen_village-oneshot-s7)
- `desert_canyon` / `oneshot` / seed 7: first failing stages `program` (run_dir=runs/eval/20260802-065054-14e3ae07/desert_canyon-oneshot-s7)
- `maze_garden` / `oneshot` / seed 7: first failing stages `navigation` (run_dir=runs/eval/20260802-065054-14e3ae07/maze_garden-oneshot-s7)
- `maze_garden` / `loop` / seed 7: first failing stages `unknown` (run_dir=runs/eval/20260802-065054-14e3ae07/maze_garden-loop-s7)

Provider/harness errors are reported separately from hard-validation failures (0 system-error cell(s) in this run).

## Limitations

- Sample size: six prompts, one seed, two variants; rates are directional evidence for the loop-vs-oneshot comparison, not statistics.
- Base model: `gpt-5.4-mini` for both variants; acceptance is sensitive to harness affordances (nudges); the frozen same-model comparison is what the claim rests on.
- Budgets: 16 provider turns / 600 s wall per run; runs one repair short of acceptance count as rejected, and budgets are never extended.
- Strict baseline: the one-shot variant gets the identical prompt but no tool execution and no feedback; the measured gap is the value of typed feedback, not prompt engineering.
- Loop stage attribution derives from observed compile events in the runlog; a run that never re-compiled reports its last known stages.
- Prompt compliance is human-scored via the YAML checklists, not a hard validator stage; runtime scope is a single walkable plane with primitive geometry (boxes, cylinders, spheres, cones, spline ribbons), verified on macOS arm64.
