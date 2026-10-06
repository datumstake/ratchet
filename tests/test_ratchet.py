"""The generic self-verifying ratchet (ratchet/ratchet.py).

The engine's promise: a move is kept only when the objective metric verifiably
improved; anything else rolls back; 'could not measure' is never progress.
These drive the real loop with tiny python one-liner commands, so the contract
semantics (exit 3 = out of moves, nonzero metric = unreadable, consecutive
rejects = stall) are pinned by execution, not by reading the code.
"""
from __future__ import annotations

import json
import sys

import pytest

from ratchet.ratchet import Contract, Outcome, RatchetRun, run_ratchet

PY = sys.executable


def write_contract(tmp_path, **kw):
    base = dict(goal="test goal", skill="test-skill", cwd=str(tmp_path))
    base.update(kw)
    p = tmp_path / "contract.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


def test_contract_load_rejects_missing_and_unknown(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"goal": "g"}), encoding="utf-8")
    with pytest.raises(ValueError):
        Contract.load(p)
    p.write_text(json.dumps({"goal": "g", "metric_cmd": "m", "propose_cmd": "p",
                             "rollback_cmd": "r", "bogus": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        Contract.load(p)


def test_commits_while_metric_falls_and_stops_out_of_moves(tmp_path):
    # metric = number of lines in state.txt; propose deletes one line per move
    # and exits 3 when its budget is spent. Script FILES, not -c one-liners:
    # shell=True is cmd.exe on this box and it mangles multi-line -c strings.
    state = tmp_path / "state.txt"
    state.write_text("a\nb\nc\n", encoding="utf-8")
    moves = tmp_path / "moves.txt"
    moves.write_text("2", encoding="utf-8")
    prop = tmp_path / "propose.py"
    prop.write_text(
        "import sys\n"
        f"moves = open(r'{moves}')\n"
        "n = int(moves.read())\n"
        "if n == 0:\n    sys.exit(3)\n"
        f"open(r'{moves}', 'w').write(str(n - 1))\n"
        f"lines = open(r'{state}').read().splitlines()\n"
        f"open(r'{state}', 'w').write('\\n'.join(lines[:-1]) + '\\n')\n"
        "print('drop-line')\n", encoding="utf-8")
    metric = f'"{PY}" -c "print(sum(1 for _ in open(r\'{state}\')))"'
    c = Contract(goal="shrink file", metric_cmd=metric,
                 propose_cmd=f'"{PY}" "{prop}"',
                 rollback_cmd=f'"{PY}" -c "pass"', cwd=str(tmp_path))
    run = run_ratchet(c, max_moves=10, log=lambda *_: None)
    assert run.committed == 2
    assert run.ended == "out of moves"
    assert [o.kept for o in run.outcomes] == [True, True]
    assert run.outcomes[0].before == 3 and run.outcomes[0].after == 2


def test_non_improving_move_is_rolled_back_and_stall_ends_run(tmp_path):
    # propose "succeeds" but never changes the metric -> every move rejected,
    # rollback runs, and the run ends stalled after MAX_CONSECUTIVE_REJECTS.
    rb = tmp_path / "rollbacks.txt"
    rb.write_text("0", encoding="utf-8")
    c = Contract(
        goal="noop goal",
        metric_cmd=f'"{PY}" -c "print(7)"',
        propose_cmd=f'"{PY}" -c "print(\'useless-move\')"',
        rollback_cmd=(f'"{PY}" -c "n=int(open(r\'{rb}\').read());'
                      f'open(r\'{rb}\',\'w\').write(str(n+1))"'),
        cwd=str(tmp_path))
    run = run_ratchet(c, max_moves=10, log=lambda *_: None)
    assert run.committed == 0
    assert "stalled" in run.ended
    assert int(rb.read_text()) == len(run.outcomes)  # every reject rolled back


def test_unreadable_metric_is_never_progress(tmp_path):
    # metric exits nonzero after the first move (compile broke): the move must
    # be rejected with "metric unreadable", not committed.
    flag = tmp_path / "broken.txt"
    metric = (f'"{PY}" -c "import sys,os;'
              f'sys.exit(1) if os.path.exists(r\'{flag}\') else print(5)"')
    propose = f'"{PY}" -c "open(r\'{flag}\',\'w\').write(\'x\'); print(\'break-it\')"'
    rollback = f'"{PY}" -c "import os; os.remove(r\'{flag}\')"'
    c = Contract(goal="g", metric_cmd=metric, propose_cmd=propose,
                 rollback_cmd=rollback, cwd=str(tmp_path))
    run = run_ratchet(c, max_moves=1, log=lambda *_: None)
    assert run.committed == 0
    assert run.outcomes[0].reason == "metric unreadable"


def test_failed_rollback_stops_the_run_loudly(tmp_path):
    c = Contract(
        goal="g",
        metric_cmd=f'"{PY}" -c "print(7)"',
        propose_cmd=f'"{PY}" -c "print(\'move\')"',
        rollback_cmd=f'"{PY}" -c "import sys; sys.exit(1)"',
        cwd=str(tmp_path))
    run = run_ratchet(c, max_moves=5, log=lambda *_: None)
    assert run.committed == 0
    assert "ROLLBACK FAILED" in run.ended


def test_higher_is_better_scores(tmp_path):
    run = RatchetRun(Contract(goal="g", metric_cmd="m", propose_cmd="p",
                              rollback_cmd="r", higher_is_better=True))
    assert run.improved(10, 11) and not run.improved(10, 9)
    run2 = RatchetRun(Contract(goal="g", metric_cmd="m", propose_cmd="p",
                               rollback_cmd="r"))
    assert run2.improved(10, 9) and not run2.improved(10, 11)


def test_journal_files_every_run_and_recall_finds_it(tmp_path):
    # A run that never got a move off still files — "this was tried, here is
    # how it ended" — and a later goal with overlapping terms recalls it.
    from ratchet.ratchet import file_skill, recall_skills
    c = Contract(goal="shrink the widget error count", metric_cmd="m",
                 propose_cmd="p", rollback_cmd="r", cwd=str(tmp_path))
    run = RatchetRun(c, outcomes=[], ended="out of moves")
    journal = tmp_path / "journal.jsonl"
    file_skill(run, journal)
    hits = recall_skills("widget error cleanup", journal)
    assert len(hits) == 1
    assert hits[0]["ended"] == "out of moves"
    assert hits[0]["span"] == "no moves made"
    # An unrelated goal recalls nothing.
    assert recall_skills("bake sourdough", journal) == []


def test_outcome_line_reads_like_the_ledger():
    assert Outcome("X", 10, 9, True, "improved").line() == \
        "COMMIT X: 10 -> 9 (improved)"
    assert "REJECT" in Outcome("Y", 10, None, False, "metric unreadable").line()
