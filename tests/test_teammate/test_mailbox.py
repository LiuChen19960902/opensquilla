"""Mailbox unit tests — file inbox read/unread/persistence."""

from __future__ import annotations

from pathlib import Path

from opensquilla.teammate.mailbox import Mailbox


def test_send_and_peek(tmp_path: Path) -> None:
    mb = Mailbox.open(tmp_path, "researcher")
    mb.send("team-lead", {"type": "task_assignment", "subject": "do X"})

    msgs = mb.peek()
    assert len(msgs) == 1
    assert msgs[0].from_name == "team-lead"
    assert msgs[0].read is False
    assert "task_assignment" in msgs[0].text


def test_read_unread_marks_read(tmp_path: Path) -> None:
    mb = Mailbox.open(tmp_path, "researcher")
    mb.send("team-lead", {"type": "message", "text": "hello"})

    unread = mb.read_unread()
    assert len(unread) == 1
    assert unread[0].read is True  # snapshot already marked
    assert mb.count_unread() == 0
    assert mb.read_unread() == []


def test_multiple_unread_ordering(tmp_path: Path) -> None:
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message", "seq": 1})
    mb.send("peer", {"type": "message", "seq": 2})

    unread = mb.read_unread()
    assert [m.from_name for m in unread] == ["team-lead", "peer"]


def test_persistence_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "inboxes" / "writer.json"
    mb1 = Mailbox(path)
    mb1.ensure()
    mb1.send("team-lead", {"type": "message", "text": "persist me"})

    mb2 = Mailbox(path)
    msgs = mb2.peek()
    assert len(msgs) == 1
    assert msgs[0].from_name == "team-lead"


def test_clear(tmp_path: Path) -> None:
    mb = Mailbox.open(tmp_path, "worker")
    mb.send("team-lead", {"type": "message"})
    mb.clear()
    assert mb.peek() == []
    assert mb.count_unread() == 0
