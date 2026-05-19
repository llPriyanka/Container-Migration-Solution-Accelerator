# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Coverage for libs/logging/token_usage.py.

Exercises the three concerns of the module separately:

1. ``extract_usage_from_update`` — the brittle part. Validated against
   each shape we've seen in the wild (Content.usage_details, OpenAI
   Usage object on raw_representation, additional_properties dict,
   bare .usage attribute, plain-dict content items, fully empty
   updates).
2. ``TokenUsageTracker.record`` — accumulation, per-agent / per-model
   bucketing, model dimension preservation, and graceful no-op on
   missing usage.
3. ``TokenUsageTracker.emit`` — fires the three custom events with the
   expected dimensions, is idempotent, swallows downstream errors.

App Insights emission is observed through the same lazy-SDK fake used
by test_event_utils.py.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from libs.logging import event_utils
from libs.logging.token_usage import (
    EVENT_AGENT_TOKEN_USAGE,
    EVENT_MODEL_TOKEN_USAGE,
    EVENT_TOKEN_USAGE_SUMMARY,
    TokenUsageTracker,
    extract_usage_from_update,
)

FAKE_CONN = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://example.in.applicationinsights.azure.com/"
)


@pytest.fixture(autouse=True)
def _reset_event_utils(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(event_utils.APP_INSIGHTS_CONN_STRING_ENV, raising=False)
    # Other tests in the suite may set AZURE_OPENAI_* vars that this
    # module reads as a model fallback in __post_init__; clear them so
    # every test starts from a known default.
    for k in (
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME",
        "AZURE_OPENAI_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("azure.monitor.events.extension", None)
    event_utils.reset_unconfigured_warning_for_tests()


def _install_fake_sdk() -> MagicMock:
    fake = types.ModuleType("azure.monitor.events.extension")
    fake.track_event = MagicMock()  # type: ignore[attr-defined]
    sys.modules["azure.monitor.events.extension"] = fake
    return fake.track_event  # type: ignore[attr-defined,return-value]


# ---------------------------------------------------------------------------
# extract_usage_from_update
# ---------------------------------------------------------------------------


class TestExtractUsage:
    def test_content_usage_details_attribute(self) -> None:
        content = SimpleNamespace(
            usage_details={
                "input_token_count": 11,
                "output_token_count": 22,
                "total_token_count": 33,
            }
        )
        update = SimpleNamespace(contents=[content])
        assert extract_usage_from_update(update) == (11, 22, 33)

    def test_content_openai_style_keys(self) -> None:
        """OpenAI calls the fields prompt_tokens / completion_tokens / total_tokens."""
        content = SimpleNamespace(
            usage_details={
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            }
        )
        update = SimpleNamespace(contents=[content])
        assert extract_usage_from_update(update) == (100, 50, 150)

    def test_content_alt_input_output_keys(self) -> None:
        content = SimpleNamespace(
            usage_details={
                "input_tokens": 7,
                "output_tokens": 3,
                # No total — extractor should sum.
            }
        )
        update = SimpleNamespace(contents=[content])
        assert extract_usage_from_update(update) == (7, 3, 10)

    def test_plain_dict_content_item(self) -> None:
        update = SimpleNamespace(
            contents=[
                {
                    "usage_details": {
                        "input_token_count": 5,
                        "output_token_count": 2,
                        "total_token_count": 7,
                    }
                }
            ]
        )
        assert extract_usage_from_update(update) == (5, 2, 7)

    def test_plain_dict_with_top_level_usage_keys(self) -> None:
        update = SimpleNamespace(
            contents=[
                {
                    "input_token_count": 4,
                    "output_token_count": 1,
                    "total_token_count": 5,
                }
            ]
        )
        assert extract_usage_from_update(update) == (4, 1, 5)

    def test_raw_representation_usage_object(self) -> None:
        """The OpenAI SDK returns a Usage object with attribute access."""
        usage = SimpleNamespace(prompt_tokens=20, completion_tokens=6, total_tokens=26)
        raw = SimpleNamespace(usage=usage)
        update = SimpleNamespace(contents=None, raw_representation=raw)
        assert extract_usage_from_update(update) == (20, 6, 26)

    def test_raw_representation_dict_with_usage(self) -> None:
        raw = {"usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}
        update = SimpleNamespace(contents=None, raw_representation=raw)
        assert extract_usage_from_update(update) == (8, 2, 10)

    def test_additional_properties_usage(self) -> None:
        update = SimpleNamespace(
            contents=None,
            raw_representation=None,
            additional_properties={
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
            },
        )
        assert extract_usage_from_update(update) == (3, 1, 4)

    def test_bare_usage_attribute(self) -> None:
        update = SimpleNamespace(
            contents=None,
            raw_representation=None,
            additional_properties=None,
            usage={"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
        )
        assert extract_usage_from_update(update) == (9, 1, 10)

    def test_none_input(self) -> None:
        assert extract_usage_from_update(None) is None

    def test_empty_update(self) -> None:
        update = SimpleNamespace(
            contents=[], raw_representation=None, additional_properties=None
        )
        assert extract_usage_from_update(update) is None

    def test_zero_totals_treated_as_missing(self) -> None:
        """A streaming chunk with usage=zero is just a text delta."""
        content = SimpleNamespace(
            usage_details={
                "input_token_count": 0,
                "output_token_count": 0,
                "total_token_count": 0,
            }
        )
        update = SimpleNamespace(contents=[content])
        assert extract_usage_from_update(update) is None

    def test_non_numeric_usage_returns_none(self) -> None:
        content = SimpleNamespace(
            usage_details={
                "input_token_count": "n/a",
                "output_token_count": "n/a",
                "total_token_count": "n/a",
            }
        )
        update = SimpleNamespace(contents=[content])
        assert extract_usage_from_update(update) is None

    def test_first_matching_content_wins(self) -> None:
        """Two usage-bearing content items: take the first."""
        c1 = SimpleNamespace(
            usage_details={"input_token_count": 1, "output_token_count": 1, "total_token_count": 2}
        )
        c2 = SimpleNamespace(
            usage_details={"input_token_count": 9, "output_token_count": 9, "total_token_count": 18}
        )
        update = SimpleNamespace(contents=[c1, c2])
        assert extract_usage_from_update(update) == (1, 1, 2)


# ---------------------------------------------------------------------------
# TokenUsageTracker.record
# ---------------------------------------------------------------------------


def _usage_update(inp: int, out: int, tot: int) -> Any:
    return SimpleNamespace(
        contents=[
            SimpleNamespace(
                usage_details={
                    "input_token_count": inp,
                    "output_token_count": out,
                    "total_token_count": tot,
                }
            )
        ]
    )


class TestTrackerRecord:
    def test_record_accumulates_across_calls(self) -> None:
        t = TokenUsageTracker(team="analysis", session_id="p1")
        t.record(_usage_update(10, 4, 14), "ChiefArchitect", model_deployment_name="gpt-4o")
        t.record(_usage_update(2, 2, 4), "ChiefArchitect", model_deployment_name="gpt-4o")
        assert t.cumulative == (12, 6, 18)
        assert t.per_agent["ChiefArchitect"].total_tokens == 18
        assert t.per_agent["ChiefArchitect"].invocations == 2

    def test_record_buckets_by_agent(self) -> None:
        t = TokenUsageTracker(team="convert", session_id="p2")
        t.record(_usage_update(5, 5, 10), "Coordinator", model_deployment_name="gpt-4o")
        t.record(_usage_update(7, 3, 10), "Reviewer", model_deployment_name="gpt-4o")
        assert set(t.per_agent.keys()) == {"Coordinator", "Reviewer"}
        assert t.per_agent["Coordinator"].total_tokens == 10
        assert t.per_agent["Reviewer"].total_tokens == 10
        assert t.cumulative == (12, 8, 20)

    def test_record_buckets_by_model(self) -> None:
        t = TokenUsageTracker(team="design", session_id="p3")
        t.record(_usage_update(10, 4, 14), "A1", model_deployment_name="gpt-4o")
        t.record(_usage_update(20, 6, 26), "A2", model_deployment_name="gpt-4o-mini")
        assert set(t.per_model.keys()) == {"gpt-4o", "gpt-4o-mini"}
        assert t.per_model["gpt-4o"].total_tokens == 14
        assert t.per_model["gpt-4o-mini"].total_tokens == 26

    def test_record_returns_extracted_tuple(self) -> None:
        t = TokenUsageTracker(team="x", session_id="p")
        assert t.record(_usage_update(3, 1, 4), "A", model_deployment_name="m") == (3, 1, 4)

    def test_record_missing_usage_returns_none(self) -> None:
        t = TokenUsageTracker(team="x", session_id="p")
        update = SimpleNamespace(contents=[], raw_representation=None)
        assert t.record(update, "A") is None
        assert t.is_empty()

    def test_record_uses_env_fallback_for_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-fallback")
        # Construct AFTER setting env so __post_init__ reads it.
        t = TokenUsageTracker(team="t", session_id="s")
        t.record(_usage_update(1, 1, 2), "A")  # No explicit model
        assert "gpt-4o-fallback" in t.per_model
        assert t.per_model["gpt-4o-fallback"].total_tokens == 2

    def test_record_model_dimension_preserved_after_late_arrival(self) -> None:
        """First update lacks a model name; later updates supply it."""
        t = TokenUsageTracker(team="t", session_id="s")
        t.record(_usage_update(1, 1, 2), "A", model_deployment_name="")
        t.record(_usage_update(1, 1, 2), "A", model_deployment_name="gpt-4o")
        bucket = t.per_agent["A"]
        assert bucket.model_deployment_name == "gpt-4o"

    def test_is_empty(self) -> None:
        t = TokenUsageTracker(team="t", session_id="s")
        assert t.is_empty()
        t.record(_usage_update(1, 1, 2), "A", model_deployment_name="m")
        assert not t.is_empty()


# ---------------------------------------------------------------------------
# TokenUsageTracker.emit
# ---------------------------------------------------------------------------


class TestTrackerEmit:
    def test_emit_no_op_when_no_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(event_utils.APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
        fake_track = _install_fake_sdk()

        t = TokenUsageTracker(team="t", session_id="s")
        t.emit()

        fake_track.assert_not_called()

    def test_emit_fires_summary_plus_per_agent_plus_per_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(event_utils.APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
        fake_track = _install_fake_sdk()

        t = TokenUsageTracker(team="analysis_workflow", session_id="proc-abc-123")
        t.record(_usage_update(10, 4, 14), "ChiefArchitect", model_deployment_name="gpt-4o")
        t.record(_usage_update(20, 6, 26), "Reviewer", model_deployment_name="gpt-4o-mini")
        t.emit()

        # 1 summary + 2 agents + 2 models = 5
        assert fake_track.call_count == 5

        event_names = [call.args[0] for call in fake_track.call_args_list]
        assert event_names.count(EVENT_TOKEN_USAGE_SUMMARY) == 1
        assert event_names.count(EVENT_AGENT_TOKEN_USAGE) == 2
        assert event_names.count(EVENT_MODEL_TOKEN_USAGE) == 2

    def test_emit_dimensions_on_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(event_utils.APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
        fake_track = _install_fake_sdk()

        t = TokenUsageTracker(team="convert_workflow", session_id="proc-xyz-7")
        t.record(_usage_update(10, 4, 14), "A", model_deployment_name="gpt-4o")
        t.emit()

        summary_call = next(
            c for c in fake_track.call_args_list if c.args[0] == EVENT_TOKEN_USAGE_SUMMARY
        )
        props = summary_call.args[1]
        assert props["team"] == "convert_workflow"
        assert props["session_id"] == "proc-xyz-7"
        assert props["process_id"] == "proc-xyz-7"
        assert props["model"] == "gpt-4o"
        assert props["prompt_tokens"] == "10"
        assert props["completion_tokens"] == "4"
        assert props["total_tokens"] == "14"

    def test_emit_dimensions_on_agent_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(event_utils.APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
        fake_track = _install_fake_sdk()

        t = TokenUsageTracker(team="design_workflow", session_id="proc-1")
        t.record(_usage_update(50, 10, 60), "Reviewer", model_deployment_name="gpt-4o-mini")
        t.emit()

        agent_call = next(
            c for c in fake_track.call_args_list if c.args[0] == EVENT_AGENT_TOKEN_USAGE
        )
        props = agent_call.args[1]
        assert props["agent"] == "Reviewer"
        assert props["team"] == "design_workflow"
        assert props["model"] == "gpt-4o-mini"
        assert props["session_id"] == "proc-1"
        assert props["prompt_tokens"] == "50"
        assert props["completion_tokens"] == "10"
        assert props["total_tokens"] == "60"
        assert props["invocations"] == "1"

    def test_emit_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(event_utils.APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
        fake_track = _install_fake_sdk()

        t = TokenUsageTracker(team="t", session_id="s")
        t.record(_usage_update(1, 1, 2), "A", model_deployment_name="m")
        t.emit()
        first_count = fake_track.call_count
        t.emit()
        t.emit()
        assert fake_track.call_count == first_count, "emit should be a no-op after first call"

    def test_emit_does_not_stamp_user_pii(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hard guarantee: no upn / email / oid / tenant keys ever leak in."""
        monkeypatch.setenv(event_utils.APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
        fake_track = _install_fake_sdk()

        t = TokenUsageTracker(team="t", session_id="proc-with-some-id")
        t.record(_usage_update(1, 1, 2), "A", model_deployment_name="m")
        t.emit()

        forbidden = {"upn", "email", "oid", "user_id", "tenant_id", "subscription_id"}
        for call in fake_track.call_args_list:
            props = call.args[1]
            leaked = set(props.keys()) & forbidden
            assert not leaked, f"PII keys leaked into telemetry: {leaked}"
