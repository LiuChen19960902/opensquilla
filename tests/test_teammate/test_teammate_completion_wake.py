"""teammate 完成通知机制（pi-subagents 对齐）的 boot.py 侧测试。

覆盖验收点：
- 完成通知以 ``role="system"`` + ``teammate_completion`` provenance 进 lead 会话；
- 完成通知触发 ``task_runtime.send`` 唤醒（monkeypatch）；
- 30s 合并窗口行为（同一 lead 会话窗口内多条完成合并成一条唤醒）；
- 日常消息仍为 ``role="teammate"``（TUI-only）；
- 升级消息保持 ``role="user"``（回归保护）；
- stub 期（task_runtime 未接线）静默跳过；wire 热插拔。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opensquilla.gateway.boot import (
    TEAMMATE_COMPLETION_PROVENANCE,
    TeammateCompletionWakeManager,
    route_teammate_visible_message,
    wire_teammate_completion_wake,
)


class _FakeSessionManager:
    """记录 append_message 调用的假 SessionManager。"""

    def __init__(self) -> None:
        self.appends: list[tuple[str, str, str, dict | None]] = []

    async def append_message(
        self,
        session_key: str,
        role: str,
        content: str,
        provenance: dict | None = None,
    ) -> Any:
        self.appends.append((session_key, role, content, provenance))
        return None


class _FakeTaskRuntime:
    """记录 send 调用的假 TaskRuntime。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    async def send(
        self,
        session_key: str,
        message: str,
        provenance: dict | None = None,
    ) -> Any:
        self.sent.append((session_key, message, provenance))
        return None


@pytest.mark.asyncio
async def test_completion_notification_enters_lead_session_as_system_role() -> None:
    """验收 1：完成通知以 system role + teammate_completion provenance 进 lead 会话。"""
    sessions = _FakeSessionManager()
    wake = TeammateCompletionWakeManager(window_s=0.05)
    await route_teammate_visible_message(
        team_id="t1",
        from_name="alice",
        text="调研完成：结论见附件",
        notify_only=True,
        lead_session_key="lead-session",
        session_manager=sessions,
        completion_wake=wake,
    )
    assert sessions.appends, "完成通知应追加到 lead 会话"
    session_key, role, content, provenance = sessions.appends[0]
    assert session_key == "lead-session"
    assert role == "system", "完成通知必须用 system role 才能进入 LLM 上下文"
    assert content == "alice: 调研完成：结论见附件"
    assert provenance == TEAMMATE_COMPLETION_PROVENANCE
    await wake.close()


@pytest.mark.asyncio
async def test_completion_triggers_task_runtime_send_wake(monkeypatch: Any) -> None:
    """验收 2：完成通知触发 task_runtime.send 唤醒（monkeypatch send）。"""
    fake_runtime = _FakeTaskRuntime()
    sent: list[tuple[str, str, dict | None]] = []

    async def _send(session_key: str, message: str, provenance: dict | None = None) -> None:
        sent.append((session_key, message, provenance))

    monkeypatch.setattr(fake_runtime, "send", _send)

    wake = TeammateCompletionWakeManager(window_s=0.05)
    wake.set_task_runtime(fake_runtime)
    await route_teammate_visible_message(
        team_id="t1",
        from_name="bob",
        text="后端接口已完成",
        notify_only=True,
        lead_session_key="lead-session",
        session_manager=_FakeSessionManager(),
        completion_wake=wake,
    )
    assert sent == [], "合并窗口未到期前不应立即唤醒"
    await asyncio.sleep(0.15)  # 等窗口到期 flush
    assert len(sent) == 1, "窗口到期后应发出一条唤醒消息"
    session_key, message, provenance = sent[0]
    assert session_key == "lead-session"
    assert "bob: 后端接口已完成" in message
    assert provenance == TEAMMATE_COMPLETION_PROVENANCE
    await wake.close()


@pytest.mark.asyncio
async def test_merge_window_coalesces_multiple_completions() -> None:
    """验收 3a：同一 lead 会话 30s 窗口内的多条完成通知合并成一条唤醒。"""
    rt = _FakeTaskRuntime()
    wake = TeammateCompletionWakeManager(window_s=0.05)
    wake.set_task_runtime(rt)
    wake.submit("lead-session", "alice: 任务A完成")
    wake.submit("lead-session", "bob: 任务B完成")
    wake.submit("lead-session", "carol: 任务C完成")
    assert rt.sent == [], "窗口内不应立即唤醒"
    await asyncio.sleep(0.15)
    assert len(rt.sent) == 1, "窗口内 3 条完成应合并成一次唤醒"
    assert "alice: 任务A完成" in rt.sent[0][1]
    assert "bob: 任务B完成" in rt.sent[0][1]
    assert "carol: 任务C完成" in rt.sent[0][1]
    await wake.close()


@pytest.mark.asyncio
async def test_merge_window_separate_windows_do_not_merge() -> None:
    """验收 3b：窗口到期后的新完成通知另起新窗口（各自独立唤醒）。"""
    rt = _FakeTaskRuntime()
    wake = TeammateCompletionWakeManager(window_s=0.05)
    wake.set_task_runtime(rt)
    wake.submit("lead-session", "alice: 任务A完成")
    await asyncio.sleep(0.15)  # 第一个窗口到期 → 1 条唤醒
    assert len(rt.sent) == 1
    wake.submit("lead-session", "bob: 任务B完成")
    await asyncio.sleep(0.15)  # 第二个窗口到期 → 第 2 条唤醒
    assert len(rt.sent) == 2
    assert "任务B完成" in rt.sent[1][1]
    assert "任务A完成" not in rt.sent[1][1]
    await wake.close()


@pytest.mark.asyncio
async def test_merge_window_per_lead_session() -> None:
    """验收 3c：合并按 lead 会话维度（不同 lead 互不合并）。"""
    rt = _FakeTaskRuntime()
    wake = TeammateCompletionWakeManager(window_s=0.05)
    wake.set_task_runtime(rt)
    wake.submit("lead-session-1", "alice: A 完成")
    wake.submit("lead-session-2", "bob: B 完成")
    await asyncio.sleep(0.15)
    assert len(rt.sent) == 2
    assert {s[0] for s in rt.sent} == {"lead-session-1", "lead-session-2"}
    await wake.close()


@pytest.mark.asyncio
async def test_daily_message_keeps_teammate_role() -> None:
    """验收 4：日常消息（普通 teammate 回复）仍为 role="teammate"（TUI-only）。"""
    sessions = _FakeSessionManager()
    await route_teammate_visible_message(
        team_id="t1",
        from_name="alice",
        text="收到，稍后回复",
        notify_only=True,
        completion=False,
        lead_session_key="lead-session",
        session_manager=sessions,
    )
    assert sessions.appends
    session_key, role, content, provenance = sessions.appends[0]
    assert role == "teammate", "日常消息必须保持 teammate role（TUI-only，不进上下文）"
    assert provenance == {
        "kind": "teammate_reply",
        "from": "alice",
        "notify_only": True,
    }
    # 日常消息不触发唤醒（未提交到合并窗口）
    assert sessions.appends[0][0] == "lead-session"


@pytest.mark.asyncio
async def test_escalation_keeps_user_role() -> None:
    """回归：升级消息（plan_approval_request）保持 user role，直接提示 lead 决策。"""
    sessions = _FakeSessionManager()
    await route_teammate_visible_message(
        team_id="t1",
        from_name="alice",
        text="需要批准部署方案",
        notify_only=False,
        lead_session_key="lead-session",
        session_manager=sessions,
    )
    assert sessions.appends
    assert sessions.appends[0][1] == "user"
    assert sessions.appends[0][3] == {
        "kind": "teammate_reply",
        "from": "alice",
        "notify_only": False,
    }


@pytest.mark.asyncio
async def test_completion_wake_skipped_without_task_runtime() -> None:
    """stub 期：task_runtime 未接线 → 完成通知照常进 lead 会话但静默跳过唤醒。"""
    sessions = _FakeSessionManager()
    wake = TeammateCompletionWakeManager(window_s=0.05)  # 未 set_task_runtime
    await route_teammate_visible_message(
        team_id="t1",
        from_name="alice",
        text="任务完成",
        notify_only=True,
        lead_session_key="lead-session",
        session_manager=sessions,
        completion_wake=wake,
    )
    # 会话追加照常（system role + provenance）
    assert sessions.appends[0][1] == "system"
    assert sessions.appends[0][3] == TEAMMATE_COMPLETION_PROVENANCE
    await asyncio.sleep(0.15)
    # 无 task_runtime → 不唤醒也不报错
    assert wake.task_runtime is None
    await wake.close()


@pytest.mark.asyncio
async def test_tui_event_emitted_for_all_paths() -> None:
    """回归：session.event.teammate TUI 事件对完成通知与日常消息都保留。"""
    events: list[tuple[str, str, dict]] = []

    async def _emit(session_key: str, event_name: str, payload: dict) -> None:
        events.append((session_key, event_name, payload))

    sessions = _FakeSessionManager()
    # 完成通知
    await route_teammate_visible_message(
        team_id="t1",
        from_name="alice",
        text="完成",
        notify_only=True,
        lead_session_key="lead-session",
        session_manager=sessions,
        event_emit=_emit,
    )
    # 日常消息
    await route_teammate_visible_message(
        team_id="t1",
        from_name="bob",
        text="日常",
        notify_only=True,
        completion=False,
        lead_session_key="lead-session",
        session_manager=sessions,
        event_emit=_emit,
    )
    assert len(events) == 2
    assert all(name == "session.event.teammate" for _, name, _ in events)
    assert events[0][2]["from"] == "alice"
    assert events[0][2]["notify_only"] is True
    assert events[1][2]["from"] == "bob"


def test_wire_teammate_completion_wake_hot_swap() -> None:
    """wire_teammate_completion_wake：TaskRuntime 就绪后把唤醒通道热插拔。"""

    class _FakeSvc:
        def __init__(self) -> None:
            self._teammate_completion_wake = TeammateCompletionWakeManager(
                window_s=0.05
            )

    svc = _FakeSvc()
    rt = _FakeTaskRuntime()
    wire_teammate_completion_wake(svc, rt)
    assert svc._teammate_completion_wake.task_runtime is rt

    # task_runtime 不可用（standalone / 测试）→ 保持 stub，不炸
    svc2 = _FakeSvc()
    wire_teammate_completion_wake(svc2, None)
    assert svc2._teammate_completion_wake.task_runtime is None

    # 未挂载 wake manager → 静默跳过
    class _BareSvc:
        pass

    wire_teammate_completion_wake(_BareSvc(), rt)
