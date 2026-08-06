"""Session key tests for the teammate key namespace."""

from __future__ import annotations

from opensquilla.session.keys import (
    build_teammate_session_key,
    is_subagent_key,
    is_teammate_key,
)


def test_build_teammate_session_key() -> None:
    key = build_teammate_session_key("main", "abc123", "researcher")
    assert key == "agent:main:teammate:abc123:researcher"


def test_is_teammate_key_canonical() -> None:
    assert is_teammate_key("agent:main:teammate:abc123:researcher")
    assert is_teammate_key("agent:main:teammate:abc123:spec-doc-auditor")


def test_is_teammate_key_rejects_others() -> None:
    assert not is_teammate_key("agent:main:subagent:xyz")
    assert not is_teammate_key("agent:main:main")
    assert not is_teammate_key("cron:foo:run:1")
    assert not is_teammate_key("")


def test_teammate_not_subagent_and_vice_versa() -> None:
    tm_key = build_teammate_session_key("main", "abc", "worker")
    assert is_teammate_key(tm_key)
    assert not is_subagent_key(tm_key)

    sub_key = "agent:main:subagent:xyz"
    assert is_subagent_key(sub_key)
    assert not is_teammate_key(sub_key)
