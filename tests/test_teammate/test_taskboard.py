"""Task board unit tests — create/claim/complete lifecycle."""

from __future__ import annotations

import pytest

from opensquilla.teammate.taskboard import TaskBoard, TaskStatus


def test_create_defaults_to_pending_without_owner() -> None:
    board = TaskBoard()
    task = board.create(title="Fix login bug", description="auth flow")
    assert task.status is TaskStatus.PENDING
    assert task.assignee is None
    assert task.created_by == ""


def test_claim_then_complete() -> None:
    board = TaskBoard()
    task = board.create(title="Write docs")
    board.claim(task.id, "writer")
    assert board.get(task.id).status is TaskStatus.IN_PROGRESS
    assert board.get(task.id).assignee == "writer"

    board.complete(task.id, "done in 3 files")
    done = board.get(task.id)
    assert done.status is TaskStatus.COMPLETED
    assert done.output == "done in 3 files"
    assert done.completed_at is not None


def test_cannot_claim_owned_task() -> None:
    board = TaskBoard()
    task = board.create(title="T", assignee="alice")
    with pytest.raises(ValueError):
        board.claim(task.id, "bob")


def test_cannot_claim_completed_task() -> None:
    board = TaskBoard()
    task = board.create(title="T")
    board.claim(task.id, "alice")
    board.complete(task.id)
    with pytest.raises(ValueError):
        board.claim(task.id, "bob")


def test_fail_and_block() -> None:
    board = TaskBoard()
    t1 = board.create(title="risky")
    board.claim(t1.id, "alice")
    board.fail(t1.id, "flaky test")
    assert board.get(t1.id).status is TaskStatus.FAILED

    t2 = board.create(title="blocked-by-x")
    board.block(t2.id)
    assert board.get(t2.id).status is TaskStatus.BLOCKED


def test_list_filters() -> None:
    board = TaskBoard()
    a = board.create(title="a")
    b = board.create(title="b")
    board.claim(a.id, "alice")
    board.complete(a.id)
    board.claim(b.id, "bob")

    pending = board.list(status=TaskStatus.PENDING)
    assert [t.title for t in pending] == []  # a completed, b claimed → both not pending

    in_progress = board.list(status="in_progress")
    assert [t.title for t in in_progress] == ["b"]

    by_assignee = board.list(assignee="alice")
    assert [t.title for t in by_assignee] == ["a"]


def test_missing_task_raises() -> None:
    board = TaskBoard()
    with pytest.raises(KeyError):
        board.complete("nope")


def test_persistence(tmp_path) -> None:
    path = tmp_path / "tasks.json"
    board1 = TaskBoard(path)
    t = board1.create(title="persisted task")
    board1.claim(t.id, "alice")

    board2 = TaskBoard(path)
    loaded = board2.get(t.id)
    assert loaded is not None
    assert loaded.title == "persisted task"
    assert loaded.status is TaskStatus.IN_PROGRESS
    assert loaded.assignee == "alice"
