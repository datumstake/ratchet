#!/usr/bin/env python3
"""Runnable demo: the ratchet burns TODO markers out of a file, one verified move
at a time.

    python examples/demo.py

It builds a tiny throwaway workload in a temp directory — a file with three TODO
lines, a proposer that removes one per move, a metric that counts them, a rollback
that restores the last state — then runs the real loop. Watch the COMMIT lines:
each one is a move that the measurement, not the proposer, approved. The demo also
plants one SABOTAGED move (the proposer breaks the file instead of fixing it) so
you can see a REJECT + rollback happen for real.
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

PROPOSER = r'''
import sys
from pathlib import Path
state = Path("state.txt")
lines = state.read_text().splitlines()
todos = [i for i, l in enumerate(lines) if "TODO" in l]
if not todos:
    sys.exit(3)  # out of moves
Path("state.bak").write_text("\n".join(lines) + "\n")
count = Path("count.txt")
n = int(count.read_text()) if count.exists() else 0
count.write_text(str(n + 1))
if n == 1:  # the sabotaged second move: "fix" by trashing the file
    state.write_text("TODO TODO TODO TODO\n")
    print("sabotaged-move")
else:
    del lines[todos[0]]
    state.write_text("\n".join(lines) + "\n")
    print(f"remove-todo-line-{todos[0]}")
'''

ROLLBACK = r'''
from pathlib import Path
Path("state.txt").write_text(Path("state.bak").read_text())
print("restored")
'''

METRIC = r'''
# Count OCCURRENCES, not lines-containing. The first draft of this demo counted
# lines and the sabotaged move (four TODOs crammed into one line) actually
# IMPROVED that metric while destroying the file - the loop committed it, as it
# should have: the ratchet is only as honest as its measurement. Metric design
# is part of the contract.
from pathlib import Path
print(Path("state.txt").read_text().count("TODO"))
'''


def main() -> int:
    from ratchet.ratchet import Contract, run_ratchet

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "state.txt").write_text(
            "keep this line\n"
            "TODO: fix the frobnicator\n"
            "keep this line too\n"
            "TODO: deflake the tests\n"
            "TODO: write the docs\n")
        (work / "propose.py").write_text(PROPOSER)
        (work / "rollback.py").write_text(ROLLBACK)
        (work / "metric.py").write_text(METRIC)
        py = json.dumps(sys.executable)[1:-1]
        contract = Contract(
            goal="burn the TODO count to zero",
            metric_cmd=f'"{py}" metric.py',
            propose_cmd=f'"{py}" propose.py',
            rollback_cmd=f'"{py}" rollback.py',
            skill="demo-todo-burner",
            cwd=str(work),
        )
        run = run_ratchet(contract, max_moves=10)
        print(f"\nended: {run.ended} — {run.committed} committed, "
              f"{len(run.outcomes) - run.committed} rejected")
        final = (work / "state.txt").read_text()
        assert "TODO" not in final, "demo invariant broken"
        assert "keep this line" in final, "sabotage survived - metric was gamed"
        print("final state.txt has zero TODOs; the sabotaged move was rejected "
              "and rolled back, never trusted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
