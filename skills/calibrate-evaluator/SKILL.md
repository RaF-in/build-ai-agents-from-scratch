---
name: calibrate-evaluator
description: Review recent evaluator verdicts, correct ones you disagree with, and turn each correction into a few-shot example so the evaluator's judgment matches yours
---

# Calibrate Evaluator

Run the article's calibration loop (prompt calibration, **not** weight fine-tuning):
review recent evaluator verdicts → mark the ones you disagree with → each
disagreement becomes a new few-shot example that steers future grading. Then
re-measure agreement against the frozen regression set to confirm the change
helped.

This is capability-agnostic. It defaults to `deep_research`; pass another
capability name as the argument to calibrate a different one.

All commands run through the generic backend. Use `uv run python` (per project
convention), from the repo root.

## Steps

1. **Show the review queue** — recent verdicts not yet human-labeled:
   ```
   uv run python -m capabilities.shared.calibration pending deep_research
   ```
   Each line shows the verdict id, run, pass/fail, weighted score, per-criterion
   scores, and the path to the snapshotted report. Read a report's artifact file
   when you need to judge whether the evaluator was right.

2. **Ask the user which verdicts they disagree with.** For each disagreement,
   gather: the correct pass/fail call, the corrected per-criterion scores, and
   (optionally) a short label. Do not invent corrections — these are the user's
   judgments.

3. **Record each correction.** This both labels the logged row and appends a new
   few-shot exemplar into the capability's `eval_examples/`:
   ```
   uv run python -m capabilities.shared.calibration label deep_research <id> \
       --passed 0 --scores "coverage=0.3,citation_quality=0.8,source_quality=0.4,synthesis=0.5" \
       --label "missed-uncited-claims"
   ```
   Use `--passed 1` for a verdict that should have passed.

4. **Measure the effect.** Re-run the objective agreement rate against the frozen
   calibration set ("eval the evaluator"):
   ```
   uv run python -m capabilities.shared.calibration agreement deep_research
   ```
   Report the agreement rate and any MISS cases. Optionally pass
   `--tolerance 0.2` to require per-criterion score agreement, not just the same
   pass/fail call. Each run is logged against a fingerprint of the exact evaluator
   prompt; show the trend across prompt versions with:
   ```
   uv run python -m capabilities.shared.calibration agreement deep_research --history
   ```

## Optional: refine the instruction text (not just few-shot examples)

Few-shot examples (steps 1–3) steer by example. To also tune the evaluator's
*instruction rules*, use the human-gated refiner. It edits **only** the fenced
`<!-- BEGIN:INSTRUCTIONS -->`…`<!-- END:INSTRUCTIONS -->` region of the evaluator
prompt — never the criteria, few-shot slot, or `SubmitVerdict` contract.

5. **Propose** an instruction edit drafted from the cases the evaluator got wrong:
   ```
   uv run python -m capabilities.shared.calibration refine deep_research
   ```
   This writes nothing. It prints the MISS cases, a **unified diff** of the
   proposed instruction block, and a saved proposal path. **Show the diff to the
   user and get explicit approval before applying.**

6. **Apply** the approved proposal — writes it between the fences, re-scores, and
   prints the before/after agreement delta:
   ```
   uv run python -m capabilities.shared.calibration refine deep_research --apply <proposal_path>
   ```
   If agreement **drops**, the command prints a one-line revert. Run it:
   ```
   uv run python -m capabilities.shared.calibration refine deep_research --revert
   ```

## Rules

- Never fabricate corrected scores or pass/fail calls — they come from the user.
- A correction is only "done" once it appears as a new file in `eval_examples/`
  (step 3 writes it) — confirm the printed path.
- Run the agreement check (step 4) after corrections so the prompt change has a
  measurable before/after delta.
- If the review queue is empty, say so and stop.
- For instruction refinement (steps 5–6): never apply a proposed diff without the
  user's explicit approval, and never edit outside the fenced INSTRUCTIONS region.
  The frozen calibration set is the objective gate — if `--apply` lowers agreement,
  revert.
