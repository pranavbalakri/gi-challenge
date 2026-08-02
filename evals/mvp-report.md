# EnvMaker MVP evaluation report

- model: `gpt-4o-mini`
- prompt_version: `1`
- eval_root: `runs/eval/20260801-233206-33e507f7`
- cells: 12

## Aggregate by variant

| variant | n | program execution rate | hard-valid rate | traversal success rate | mean repair turns | mean latency (s) |
| --- | --- | --- | --- | --- | --- | --- |
| loop | 6 | 0.83 | 0.33 | 0.33 | 7.00 | 11.47 |
| oneshot | 6 | 0.33 | 0.00 | 0.00 | n/a | 3.16 |

Means exclude provider_error/harness_error rows; turns count every provider turn including the initial code turn.
Loop-variant program execution is derived from observed compile events; a run that never compiled reports program=False.

## Per-run outcomes

| prompt_id | variant | seed | terminal_state | accepted | turns_used | wall_seconds | error |
| --- | --- | --- | --- | --- | --- | --- | --- |
| jungle_valley | oneshot | 7 | rejected_after_budget | False | 1 | 2.67 | static validation failed |
| jungle_valley | loop | 7 | rejected_after_budget | False | 8 | 8.68 | turn or wall budget exhausted |
| frozen_village | oneshot | 7 | rejected_after_budget | False | 1 | 3.17 | no landmark probe available |
| frozen_village | loop | 7 | rejected_after_budget | False | 8 | 13.29 | turn or wall budget exhausted |
| desert_canyon | oneshot | 7 | rejected_after_budget | False | 1 | 2.84 | static validation failed |
| desert_canyon | loop | 7 | rejected_after_budget | False | 8 | 15.15 | turn or wall budget exhausted |
| industrial_yard | oneshot | 7 | rejected_after_budget | False | 1 | 2.84 | static validation failed |
| industrial_yard | loop | 7 | accepted | True | 3 | 8.31 |  |
| island_settlement | oneshot | 7 | rejected_after_budget | False | 1 | 3.95 | static validation failed |
| island_settlement | loop | 7 | rejected_after_budget | False | 8 | 10.05 | turn or wall budget exhausted |
| maze_garden | oneshot | 7 | rejected_after_budget | False | 1 | 3.51 | no landmark probe available |
| maze_garden | loop | 7 | accepted | True | 7 | 13.35 |  |

## Failed examples

- `jungle_valley` / `oneshot` / seed 7: first failing stages `program` (run_dir=runs/eval/20260801-233206-33e507f7/jungle_valley-oneshot-s7)
- `jungle_valley` / `loop` / seed 7: first failing stages `unknown` (run_dir=runs/eval/20260801-233206-33e507f7/jungle_valley-loop-s7)
- `frozen_village` / `oneshot` / seed 7: first failing stages `unknown` (run_dir=runs/eval/20260801-233206-33e507f7/frozen_village-oneshot-s7)
- `frozen_village` / `loop` / seed 7: first failing stages `controller` (run_dir=runs/eval/20260801-233206-33e507f7/frozen_village-loop-s7)
- `desert_canyon` / `oneshot` / seed 7: first failing stages `program` (run_dir=runs/eval/20260801-233206-33e507f7/desert_canyon-oneshot-s7)
- `desert_canyon` / `loop` / seed 7: first failing stages `program` (run_dir=runs/eval/20260801-233206-33e507f7/desert_canyon-loop-s7)
- `industrial_yard` / `oneshot` / seed 7: first failing stages `program` (run_dir=runs/eval/20260801-233206-33e507f7/industrial_yard-oneshot-s7)
- `island_settlement` / `oneshot` / seed 7: first failing stages `program` (run_dir=runs/eval/20260801-233206-33e507f7/island_settlement-oneshot-s7)
- `island_settlement` / `loop` / seed 7: first failing stages `controller` (run_dir=runs/eval/20260801-233206-33e507f7/island_settlement-loop-s7)
- `maze_garden` / `oneshot` / seed 7: first failing stages `unknown` (run_dir=runs/eval/20260801-233206-33e507f7/maze_garden-oneshot-s7)

Provider/harness errors are reported separately from hard-validation failures (0 system-error cell(s) in this run).

## Limitations

- **Sample size.** Six prompts, one seed, two variants (12 runs). Rates are directional
  evidence for the loop-vs-oneshot comparison, not statistics; no confidence intervals are
  claimed and none should be inferred.
- **Base model.** `gpt-4o-mini` was chosen for cost and is the weakest link: it narrates
  instead of acting without deterministic harness nudges, and acceptance moved from 1/6 to
  2/6 between evaluation attempts purely from one added nudge. A stronger model would
  likely raise both variants; the frozen comparison (same model, same budgets, tools on/off)
  is what the claim rests on.
- **Budget.** 8 provider turns / 600 s wall per run. Two of the four failed loop cells ended
  at the controller stage — one repair short of acceptance — and are counted as rejected;
  the harness never extends budgets retroactively.
- **Strict baseline.** The oneshot variant receives the identical system prompt and seed but
  no tools and no feedback; its dominant failures ("static validation failed", "no landmark")
  are exactly the failures the loop repairs. The measured gap is the value of typed feedback,
  not of prompt engineering.
- **"unknown" first-failing stages.** Loop rows derive stage outcomes from observed compile
  events in the runlog; a run whose final patch was never re-compiled reports its last known
  stage picture (see the aggregate-table footnote).
- **Prompt compliance.** The per-prompt human-audit checklists in `evals/mvp.yaml` are
  recorded but not yet human-scored; internal semantic validity (component-graph coherence)
  is machine-checked, prompt faithfulness is not.
- **Runtime scope.** Single walkable plane, box/cylinder primitives, isometric/top-down
  renders, macOS-first (windowed off-screen capture is a macOS occlusion workaround;
  `GODOT_BIN` overrides the vendored binary elsewhere). Accepted runs persist a sealed,
  canonically fingerprinted `environment-definition.json`; all failed runs keep their full
  traces under the eval root.
