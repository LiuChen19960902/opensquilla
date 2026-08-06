"""Team registry unit tests — team/member persistence and guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.teammate.registry import TeamRegistry


def test_create_and_get_team(tmp_path: Path) -> None:
    reg = TeamRegistry(tmp_path)
    team = reg.create_team(name="alpha", lead_agent_id="team-lead")
    assert team.id
    assert team.lead_agent_id == "team-lead"

    loaded = reg.get_team(team.id)
    assert loaded is not None
    assert loaded.name == "alpha"


def test_add_member_and_persist(tmp_path: Path) -> None:
    reg = TeamRegistry(tmp_path)
    team = reg.create_team(name="alpha", lead_agent_id="team-lead")
    info = reg.add_member(
        team.id, "researcher", model="deepseek-v4-flash-0731", prompt="you research"
    )
    assert info.agent_id == f"researcher@{team.id}"
    assert info.model == "deepseek-v4-flash-0731"

    # survives reload
    reg2 = TeamRegistry(tmp_path)
    member = reg2.get_member(team.id, "researcher")
    assert member is not None
    assert member.prompt == "you research"


def test_duplicate_member_rejected(tmp_path: Path) -> None:
    reg = TeamRegistry(tmp_path)
    team = reg.create_team(name="alpha", lead_agent_id="lead")
    reg.add_member(team.id, "researcher")
    with pytest.raises(ValueError):
        reg.add_member(team.id, "researcher")


def test_update_member_status(tmp_path: Path) -> None:
    reg = TeamRegistry(tmp_path)
    team = reg.create_team(name="alpha", lead_agent_id="lead")
    reg.add_member(team.id, "worker")
    reg.update_member_status(team.id, "worker", "idle")
    assert reg.get_member(team.id, "worker").status == "idle"


def test_delete_team_guard_active_members(tmp_path: Path) -> None:
    reg = TeamRegistry(tmp_path)
    team = reg.create_team(name="alpha", lead_agent_id="lead")
    reg.add_member(team.id, "worker")  # status spawning (active)
    assert reg.delete_team(team.id) is False
    assert reg.get_team(team.id) is not None

    reg.update_member_status(team.id, "worker", "done")
    assert reg.delete_team(team.id) is True
    assert reg.get_team(team.id) is None


def test_list_teams(tmp_path: Path) -> None:
    reg = TeamRegistry(tmp_path)
    t1 = reg.create_team(name="alpha", lead_agent_id="lead")
    t2 = reg.create_team(name="beta", lead_agent_id="lead")
    ids = {t.id for t in reg.list_teams()}
    assert ids == {t1.id, t2.id}
