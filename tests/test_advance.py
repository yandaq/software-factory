"""The stage graph `advance()`: pure transitions, table-driven."""

import pytest

from sf.run import RunState
from sf.workflow import CapReached, Finished, Next, StageResult, advance


def state(**over) -> RunState:
    return RunState(run_id="r", target="/t", trunk="main", branch="sf/r", **over)


OK = StageResult("ok", "summary")
VALIDATED = StageResult("validated", "passed", {"validation_report": "PASS"})
SKIPPED = StageResult("validation_skipped", "skipped", {"validation_report": "SKIPPED"})
INVALID = StageResult("validation_failed", "failed", {"feedback": "validation log"})
APPROVE = StageResult("approve", "approved", {"feedback": "none"})
CHANGES = StageResult("changes_requested", "changes", {"feedback": "findings"})
MERGED = StageResult("merged", "merged sf/r into main")
CONFLICT = StageResult("conflict", "merge conflict: x", {"feedback": "conflict details"})
MERGE_INVALID = StageResult("validation_failed", "merge validation failed", {"feedback": "merge log"})


@pytest.mark.parametrize(("st", "stage", "result", "expected"), [
    # happy path
    (state(), None, None, Next("branch")),
    (state(), "branch", OK, Next("prompt", completes=("branch", "summary"))),
    (state(), "prompt", OK, Next("plan", completes=("prompt", "summary"))),
    (state(), "plan", OK, Next("implement", {"loop": 1, "feedback": ""}, completes=("plan", "summary"))),
    (state(loop=1), "implement", VALIDATED, Next("review", {"loop": 1, "validation_report": "PASS"})),
    (state(loop=1), "implement", SKIPPED, Next("review", {"loop": 1, "validation_report": "SKIPPED"})),
    (state(loop=1), "review", APPROVE, Next("document", completes=("implement", "approved in loop 1"))),
    (state(), "document", OK, Next("merge", completes=("document", "summary"))),
    (state(), "merge", MERGED, Next("improve", completes=("merge", "merged sf/r into main"))),
    (state(), "improve", OK, Finished("done", "run done", completes=("improve", "summary"))),
    # implement ⇄ review loop: feedback is the next implement's input, up to the cap
    (state(loop=2), "review", CHANGES, Next("implement", {"loop": 3, "feedback": "findings"})),
    (state(loop=2), "implement", INVALID, Next("implement", {"loop": 3, "feedback": "validation log"})),
    (state(loop=4), "review", CHANGES, Next("implement", {"loop": 5, "feedback": "findings"})),
    # cap exhaustion
    (state(loop=5), "review", CHANGES, CapReached(5, Next("implement", {"loop": 6, "feedback": "findings"}))),
    (state(loop=5), "implement", INVALID,
     CapReached(5, Next("implement", {"loop": 6, "feedback": "validation log"}))),
    (state(loop=7, max_loops=7), "review", CHANGES,
     CapReached(7, Next("implement", {"loop": 8, "feedback": "findings"}))),
    # merge conflict / merge validation failure: one more implement⇄review pass, even at the cap
    (state(loop=5, merge_conflicts=1), "merge", CONFLICT,
     Next("implement", {"loop": 6, "feedback": "conflict details"})),
    (state(loop=2, merge_conflicts=1), "merge", MERGE_INVALID,
     Next("implement", {"loop": 3, "feedback": "merge log"})),
    (state(loop=5, merge_conflicts=1), "review", CHANGES, Next("implement", {"loop": 6, "feedback": "findings"})),
    (state(loop=6, merge_conflicts=1), "review", CHANGES,
     CapReached(6, Next("implement", {"loop": 7, "feedback": "findings"}))),
    (state(loop=6, merge_conflicts=1), "review", APPROVE,
     Next("document", completes=("implement", "approved in loop 6"))),
    (state(loop=6, merge_conflicts=2), "merge", CONFLICT, Finished("aborted", "merge conflict: x")),
])
def test_transitions(st, stage, result, expected):
    before = st.model_copy(deep=True)
    assert advance(st, stage, result) == expected
    assert st == before  # pure: the state is never mutated


def test_plan_approval_is_rejected_for_queued_runs():
    assert advance(state(approve_plan=True), None, None) == Finished(
        "failed", "plan approval (--approve-plan) is not supported for queued runs in v1")
    assert advance(state(approve_plan=True), None, None, human=True) == Next("branch")


def test_unknown_transition_fails_clearly():
    with pytest.raises(ValueError, match="no transition from stage 'merge' with outcome 'weird'"):
        advance(state(), "merge", StageResult("weird", "?"))
