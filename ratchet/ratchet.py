"""The self-verifying ratchet — a workload-agnostic propose/verify/commit loop.

The problem this solves: automation (a script, a small model, an agent) proposing
changes it cannot be trusted to grade. The ratchet moves the trust to an objective
measurement:

    propose one move -> measure -> keep only if the metric VERIFIABLY improved
    -> else roll back and record why.

A workload is an **objective contract** — four commands in a JSON file:

    {
      "goal":        "drive the project's undefined symbols to zero",
      "metric_cmd":  "bash metric.sh",     # prints ONE number; lower is better
      "propose_cmd": "python propose.py",  # prints move id + does the APPLY
      "rollback_cmd":"python rollback.py", # undoes the last apply
      "skill":       "my-port"             # name this capability files under
    }

`propose_cmd` prints the move's identifier on stdout (first line) and applies it;
exit 0 with output means "a move was made", exit 3 means "no moves left". The
ratchet then re-runs `metric_cmd`: strictly better keeps the move, anything else
runs `rollback_cmd` and records the move as rejected.

The loop is honest about its endings and about measurement itself:
  - an unreadable metric is never progress (a broken measure must not look like 0)
  - a failed rollback stops loudly (the tree needs a person)
  - consecutive rejects stall the run (more attempts are the same attempt)

Every run both CONSUMES prior art and FILES its outcome through a pluggable
journal (a JSONL file by default): a solved goal hands the next similar goal its
solver, and a stalled one hands it the dead end. Capability accumulates in tools
and records — not in any model's weights.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

#: A move may not run longer than this; a stuck proposer must not hang the loop.
MOVE_TIMEOUT_S = 1800

#: Stop after this many consecutive rejected moves — the proposer is out of
#: ideas that survive verification, and more attempts are the same attempt.
MAX_CONSECUTIVE_REJECTS = 3

#: Where runs are journaled unless the caller chooses elsewhere.
DEFAULT_JOURNAL = Path("ratchet-journal.jsonl")


@dataclass
class Contract:
    """The objective contract: everything workload-specific, in four commands."""

    goal: str
    metric_cmd: str
    propose_cmd: str
    rollback_cmd: str
    skill: str = "unnamed-skill"
    cwd: str = "."
    # True when a HIGHER metric is better (a score); default is lower-is-better
    # (an error count), because error counts are what ratchets usually burn down.
    higher_is_better: bool = False

    @classmethod
    def load(cls, path: Path | str) -> "Contract":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(data) - {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        if unknown:
            raise ValueError(f"unknown contract field(s): {sorted(unknown)}")
        missing = {"goal", "metric_cmd", "propose_cmd", "rollback_cmd"} - set(data)
        if missing:
            raise ValueError(f"contract missing: {sorted(missing)}")
        return cls(**data)


@dataclass
class Outcome:
    """One move's result — the unit the log, the journal and the report share."""

    move: str
    before: float
    after: float | None
    kept: bool
    reason: str
    seconds: float = 0.0

    def line(self) -> str:
        arrow = f"{self.before:g} -> {self.after:g}" if self.after is not None else f"{self.before:g} -> ?"
        return f"{'COMMIT' if self.kept else 'REJECT'} {self.move}: {arrow} ({self.reason})"


@dataclass
class RatchetRun:
    contract: Contract
    outcomes: list[Outcome] = field(default_factory=list)
    ended: str = ""

    def improved(self, before: float, after: float) -> bool:
        return after > before if self.contract.higher_is_better else after < before

    @property
    def committed(self) -> int:
        return sum(1 for o in self.outcomes if o.kept)


def _run(cmd: str, cwd: str, timeout: int = MOVE_TIMEOUT_S) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"


def read_metric(contract: Contract) -> float | None:
    """The objective, measured. None when the metric itself failed — a run must
    never mistake 'could not measure' for a score."""
    code, out = _run(contract.metric_cmd, contract.cwd)
    if code != 0:
        return None
    for tok in reversed(out.split()):
        try:
            return float(tok)
        except ValueError:
            continue
    return None


def run_ratchet(contract: Contract, max_moves: int = 25,
                log=print) -> RatchetRun:
    """The loop. Deterministic, ground-truthed, honest about its ending."""
    run = RatchetRun(contract)
    before = read_metric(contract)
    if before is None:
        run.ended = "metric unreadable at start"
        log(f"ratchet refused: {run.ended}")
        return run
    log(f"ground truth: {before:g}  (goal: {contract.goal})")
    rejects_in_a_row = 0
    while run.committed < max_moves:
        t0 = time.time()
        code, out = _run(contract.propose_cmd, contract.cwd)
        move = (out.strip().splitlines() or ["?"])[0][:120]
        if code == 3:
            run.ended = "out of moves"
            log("proposer: no moves left")
            break
        if code != 0:
            run.ended = f"proposer failed (exit {code})"
            log(f"proposer failed (exit {code}): {out[:200]}")
            break
        after = read_metric(contract)
        kept = after is not None and run.improved(before, after)
        if kept:
            reason = "improved"
            rejects_in_a_row = 0
        else:
            reason = "metric unreadable" if after is None else "did not improve"
            rb_code, rb_out = _run(contract.rollback_cmd, contract.cwd)
            if rb_code != 0:
                run.ended = f"ROLLBACK FAILED for {move} — stopping, tree needs a person"
                run.outcomes.append(Outcome(move, before, after, False,
                                            reason + "; rollback failed",
                                            time.time() - t0))
                log(run.ended + f": {rb_out[:200]}")
                break
            rejects_in_a_row += 1
        o = Outcome(move, before, after, kept, reason, time.time() - t0)
        run.outcomes.append(o)
        log(o.line())
        if kept:
            before = after  # type: ignore[assignment]
        if rejects_in_a_row >= MAX_CONSECUTIVE_REJECTS:
            run.ended = f"stalled ({rejects_in_a_row} consecutive rejects)"
            log(run.ended)
            break
    if not run.ended:
        run.ended = "max moves reached" if run.committed >= max_moves else "done"
    return run


# -- the journal: runs consume prior art and file their outcome ---------------


def file_skill(run: RatchetRun, journal: Path | str = DEFAULT_JOURNAL,
               contract_path: str = "") -> None:
    """Append the run to the journal. What gets remembered is the CAPABILITY —
    the contract that worked, where it lives, what it achieved — so the next
    goal that sounds like this one starts from a recalled solver instead of a
    blank page. A run that committed nothing is still filed: 'this was tried,
    it stalled, here is why' is exactly what stops the next attempt re-deriving
    a dead end."""
    c = run.contract
    first = run.outcomes[0].before if run.outcomes else None
    last = next((o.after for o in reversed(run.outcomes) if o.kept), first)
    span = (f"{first:g} -> {last:g}" if first is not None and last is not None
            else "no moves made")
    record = {
        "skill": c.skill, "goal": c.goal, "contract": contract_path or "(inline)",
        "cwd": c.cwd, "metric": c.metric_cmd, "committed": run.committed,
        "span": span, "ended": run.ended,
        "outcomes": [o.line() for o in run.outcomes[-10:]],
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    p = Path(journal)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def recall_skills(goal: str, journal: Path | str = DEFAULT_JOURNAL,
                  limit: int = 5) -> list[dict]:
    """What the journal already knows about goals like this one — crude term
    overlap, newest first. Swap in your own retrieval by writing a different
    journal reader; the loop does not care."""
    p = Path(journal)
    if not p.is_file():
        return []
    terms = {t for t in re.findall(r"[a-z0-9]+", goal.lower()) if len(t) > 2}
    hits = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rec_terms = set(re.findall(r"[a-z0-9]+", str(rec.get("goal", "")).lower()))
        if terms & rec_terms:
            hits.append(rec)
    return hits[-limit:][::-1]


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="ratchet")
    ap.add_argument("contract", help="path to the objective-contract JSON")
    ap.add_argument("--max", type=int, default=25)
    ap.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    ap.add_argument("--recall", action="store_true",
                    help="print prior journal entries matching this goal, then exit")
    args = ap.parse_args()

    contract = Contract.load(args.contract)
    if args.recall:
        for rec in recall_skills(contract.goal, args.journal):
            print(json.dumps(rec, indent=1))
        return 0
    prior = recall_skills(contract.goal, args.journal)
    if prior:
        print(f"journal: {len(prior)} prior run(s) for goals like this — "
              f"latest ended: {prior[0].get('ended')}")
    run = run_ratchet(contract, max_moves=args.max)
    file_skill(run, args.journal, contract_path=str(args.contract))
    print(f"filed to {args.journal}")
    return 0 if run.committed > 0 or run.ended == "out of moves" else 1


if __name__ == "__main__":
    raise SystemExit(main())
