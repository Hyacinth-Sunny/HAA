# HAA — Hyacinth Automated Analyzer

**Hyacinth Automated Analyzer** — An AI-agent based multi-function toolkit for scientific research in the realm of computer science & artificial intelligence (CS/AI).

An automated research and paper-writing tool, modelled on **HM-Pro** and
informed by AI Scientist v1/v2 and Agent Laboratory. A **code-driven state
machine** runs a single LLM call per stage, then returns control to code — the
model never knows which step it is on.

## Pipeline

```
Research Brief → SEEK → NOVELTY → SCREEN → DESIGN ⇄ VERIFY → GRADE → WRITE
                                                       ↓            ↓
                                            (trivial/loophole →   REVIEW ⇄ REFINE → PUBLISHED
                                             next candidate)      (physically blind, max 3 rounds)
```

## Design principles (from HM-Pro lessons)

1. **Code is the state machine.** One stage = one model call; code picks the next stage.
2. **Dual-layer + pre-deduct budget.** Per-campaign cap + global cap, deducted *before* the call and persisted to SQLite so a crash can't overspend. Budget gates only the repeatable loops (SEEK/SCREEN, DESIGN/VERIFY) — **never** GRADE/WRITE (Lesson 5).
3. **Rework carries full context.** VERIFY→DESIGN checkpoints include `verify_findings` (Lesson 1).
4. **VERIFY passes on "no model-internal counterexample",** not "every proof obligation resolved" (Lesson 2).
5. **Snapshots + rollback.** Best snapshot saved after each review round; rollback before the next refine if the score regressed (Lesson 3).
6. **Dead candidate → advance the queue,** terminate only when the queue is exhausted (Lesson 4).
7. **Structured output schemas have complete `required` fields** (Lesson 6).
8. **Every LLM call has a 300s timeout** (Lesson 7).

## Quick start

```bash
conda activate haa            # Python 3.12+
pip install -e ".[dev]"
haa init                      # create the SQLite DB
haa list                      # list campaigns
haa report                    # show aggregate stats
pytest                        # run tests
```

See `DEV_BRIEF.md` for the full specification.
