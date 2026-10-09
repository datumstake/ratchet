# ratchet

[![ci](https://github.com/datumstake/ratchet/actions/workflows/ci.yml/badge.svg)](https://github.com/datumstake/ratchet/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-8%20passing-success)](tests)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![dependencies](https://img.shields.io/badge/runtime%20deps-0-success)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A workload-agnostic **propose → measure → commit-or-rollback** loop for automation
that cannot be trusted to grade its own work — scripts, small local models, agents.
The machine never gets to say "that worked"; the measurement does.

```
ground truth: 3  (goal: burn the TODO count to zero)
COMMIT remove-todo-line-1: 3 -> 2 (improved)
REJECT sabotaged-move: 2 -> 4 (did not improve)   <- rolled back, never trusted
COMMIT remove-todo-line-2: 2 -> 1 (improved)
COMMIT remove-todo-line-2: 1 -> 0 (improved)
```

## The idea

A workload is reduced to an **objective contract** — four commands in JSON:

```jsonc
{
  "goal":         "drive the project's undefined symbols to zero",
  "metric_cmd":   "bash metric.sh",      // prints ONE number; lower is better
  "propose_cmd":  "python propose.py",   // prints a move id + APPLIES it; exit 3 = out of moves
  "rollback_cmd": "python rollback.py",  // undoes the last apply
  "skill":        "my-port"
}
```

The loop proposes one move at a time, re-measures, and keeps the move **only** if
the metric verifiably improved — otherwise it rolls back and records the rejection.
The contract seam means the proposer can be anything: a deterministic script today,
an LLM tomorrow, without the loop changing at all.

Honesty is structural, not aspirational:

- **An unreadable metric is never progress.** A broken measure must not look like
  a zero. (Learned the hard way: a failed compile that empties the error log reads
  as "no errors" to a naive metric.)
- **A failed rollback stops loudly.** The tree needs a person; pretending otherwise
  compounds the damage.
- **Consecutive rejects stall the run.** When the proposer is out of ideas that
  survive verification, more attempts are the same attempt.
- **The ratchet is only as honest as its metric.** The demo deliberately includes a
  sabotaged move that *gamed* this demo's first metric draft (it improved the count
  while destroying the file). Metric design is part of the contract — the demo's
  final metric rejects it, and the repo keeps that lesson in the comments.

Every run **consumes prior art and files its outcome** through a pluggable journal
(JSONL by default): a solved goal hands the next similar goal its solver, and a
stalled one hands it the dead end. Capability accumulates in tools and records —
not in any model's weights.

## Why it exists (case study)

Extracted from a larger system where a small local model drove a real porting
effort — merging one codebase's features onto another's engine, ~1100 undefined
link symbols deep. The model can't verify its own changes, so the ratchet owned
that: each proposed port was ground-truthed by a full compile-and-link, kept only
when the symbol count verifiably fell, rolled back and recorded otherwise. Dozens
of commits landed with zero regressions merged, including honest rejects where a
plausible-looking port broke the build.

Pairs with [gapsmith](https://github.com/datumstake/gapsmith), the
rule-driven proposer from the same system: gapsmith decides *what is provably
safe to try*; the ratchet decides *what actually worked*.

## Try it

```
pip install -e ".[dev]"
python examples/demo.py    # the loop, live: commits, one sabotaged move, a reject
pytest                     # pins commit/rollback/stall/unreadable-metric semantics
```

Then point it at something real:

```
python -m ratchet.ratchet my-contract.json --max 10
```

No third-party runtime dependencies — standard library only.

## License

MIT. See [LICENSE](LICENSE).

---

Built by **[datumstake](https://github.com/datumstake)**. The rest of the set:

[gapsmith](https://github.com/datumstake/gapsmith) — resolve a whole class of porting gaps from rules that carry their own proof ·
[handle](https://github.com/datumstake/handle) — a logged-in Chrome, nine verbs ·
[focus-three](https://github.com/datumstake/focus-three) — three tasks, one timer, nothing else
