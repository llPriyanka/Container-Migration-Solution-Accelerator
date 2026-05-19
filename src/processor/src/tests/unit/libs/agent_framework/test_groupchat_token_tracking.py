# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Coverage for the TokenUsageTracker wiring inside GroupChatOrchestrator.

The full ``run_stream`` path requires a live GroupChat runtime, but the
hooks added for token tracking can be tested in isolation:

- ``_new_token_tracker()`` reads team and session_id from the orchestrator
- ``_record_token_usage_if_present()`` extracts usage and forwards to the
  active tracker
- ``_record_token_usage_if_present()`` is silent when no tracker is set
  (i.e. outside of an active ``run_stream``)
- Errors raised by the tracker do not propagate
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from libs.agent_framework.groupchat_orchestrator import GroupChatOrchestrator


def _make_orch(name: str = "analysis_workflow", process_id: str = "proc-1"):
    return GroupChatOrchestrator(
        name=name,
        process_id=process_id,
        participants={"Coordinator": object()},
        memory_client=None,
        coordinator_name="Coordinator",
    )


def _usage_event(agent_name: str, inp: int, out: int, tot: int):
    content = SimpleNamespace(
        usage_details={
            "input_token_count": inp,
            "output_token_count": out,
            "total_token_count": tot,
        }
    )
    data = SimpleNamespace(contents=[content])
    return SimpleNamespace(executor_id=f"groupchat_agent:{agent_name}", data=data)


class TestTrackerFactory:
    def test_new_tracker_carries_orchestrator_identity(self) -> None:
        orch = _make_orch(name="convert_workflow", process_id="proc-7")
        tracker = orch._new_token_tracker()
        assert tracker.team == "convert_workflow"
        assert tracker.session_id == "proc-7"
        assert tracker.process_id == "proc-7"

    def test_new_tracker_starts_empty(self) -> None:
        orch = _make_orch()
        tracker = orch._new_token_tracker()
        assert tracker.is_empty()
        assert tracker.cumulative == (0, 0, 0)


class TestRecordTokenUsageIfPresent:
    def test_records_when_tracker_active(self) -> None:
        orch = _make_orch()
        orch._token_tracker = orch._new_token_tracker()
        event = _usage_event("ChiefArchitect", 10, 4, 14)

        orch._record_token_usage_if_present(event, "ChiefArchitect")

        assert orch._token_tracker.cumulative == (10, 4, 14)
        assert "ChiefArchitect" in orch._token_tracker.per_agent

    def test_silent_when_no_tracker(self) -> None:
        orch = _make_orch()
        assert orch._token_tracker is None
        event = _usage_event("ChiefArchitect", 10, 4, 14)

        # Must not raise.
        orch._record_token_usage_if_present(event, "ChiefArchitect")

        assert orch._token_tracker is None

    def test_silent_when_no_usage_on_event(self) -> None:
        orch = _make_orch()
        orch._token_tracker = orch._new_token_tracker()
        event = SimpleNamespace(
            executor_id="groupchat_agent:Coordinator",
            data=SimpleNamespace(contents=[], raw_representation=None),
        )

        orch._record_token_usage_if_present(event, "Coordinator")

        assert orch._token_tracker.is_empty()

    def test_swallows_tracker_exceptions(self) -> None:
        """A misbehaving extractor must never break the workflow loop."""
        orch = _make_orch()
        tracker = MagicMock()
        tracker.record.side_effect = RuntimeError("simulated parser bug")
        orch._token_tracker = tracker  # type: ignore[assignment]

        event = _usage_event("A", 1, 1, 2)
        # Must not raise.
        orch._record_token_usage_if_present(event, "A")

        tracker.record.assert_called_once()
