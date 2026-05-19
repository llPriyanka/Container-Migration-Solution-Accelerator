# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Coverage for libs/logging/event_utils.py.

The helper is small but security-relevant (it's the gate that decides
whether anything is published to App Insights at all), so the tests
exercise every branch:

- not configured (no env var) — single warning, no SDK call
- not configured (env var set to whitespace) — treated as unset
- configured + SDK missing — single warning, no SDK call
- configured + SDK present — forwards args verbatim
- configured + SDK present but track_event raises — swallowed

The SDK is faked via ``sys.modules`` injection because the helper
imports it lazily inside the function; ``monkeypatch.setattr`` on the
helper module would not catch the late import.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

from libs.logging.event_utils import (
    APP_INSIGHTS_CONN_STRING_ENV,
    reset_unconfigured_warning_for_tests,
    track_event_if_configured,
)

FAKE_CONN = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://example.in.applicationinsights.azure.com/"
)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts from a clean module state.

    The helper caches "already warned" flags at module scope and the
    SDK is imported lazily, so we must clear both between tests or the
    second test would pass for the wrong reason.
    """
    monkeypatch.delenv(APP_INSIGHTS_CONN_STRING_ENV, raising=False)
    sys.modules.pop("azure.monitor.events.extension", None)
    reset_unconfigured_warning_for_tests()


def _install_fake_sdk() -> MagicMock:
    """Inject a fake ``azure.monitor.events.extension`` module."""
    fake = types.ModuleType("azure.monitor.events.extension")
    fake.track_event = MagicMock()  # type: ignore[attr-defined]
    sys.modules["azure.monitor.events.extension"] = fake
    return fake.track_event  # type: ignore[attr-defined,return-value]


# ---------------------------------------------------------------------------
# Gating: env var not set / blank
# ---------------------------------------------------------------------------


def test_no_op_when_env_var_unset(caplog: pytest.LogCaptureFixture) -> None:
    fake_track = _install_fake_sdk()

    with caplog.at_level(logging.WARNING, logger="libs.logging.event_utils"):
        track_event_if_configured("ShouldNotSend", {"k": "v"})

    fake_track.assert_not_called()
    assert any(
        APP_INSIGHTS_CONN_STRING_ENV in rec.message for rec in caplog.records
    ), "expected a one-time warning about the missing env var"


def test_no_op_when_env_var_is_whitespace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(APP_INSIGHTS_CONN_STRING_ENV, "   ")
    fake_track = _install_fake_sdk()

    with caplog.at_level(logging.WARNING, logger="libs.logging.event_utils"):
        track_event_if_configured("ShouldNotSend")

    fake_track.assert_not_called()


def test_unconfigured_warning_fires_only_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_fake_sdk()

    with caplog.at_level(logging.WARNING, logger="libs.logging.event_utils"):
        track_event_if_configured("First")
        track_event_if_configured("Second")
        track_event_if_configured("Third")

    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    # Filter to messages mentioning the env var — other warnings might
    # leak in from unrelated loggers in CI.
    matching = [r for r in warn_records if APP_INSIGHTS_CONN_STRING_ENV in r.message]
    assert len(matching) == 1, f"expected 1 warning, got {len(matching)}"


# ---------------------------------------------------------------------------
# Gating: env var set but SDK missing
# ---------------------------------------------------------------------------


def test_no_op_when_sdk_not_installed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)

    # Force the lazy import to fail by stuffing a placeholder module
    # that raises on attribute access. Easier path: inject a finder
    # that always raises ImportError for that specific module.
    class _Blocker:
        def find_spec(self, name, *args, **kwargs):  # noqa: D401
            if name == "azure.monitor.events.extension":
                raise ImportError("simulated missing SDK")
            return None

    monkeypatch.setattr(sys, "meta_path", [_Blocker(), *sys.meta_path])

    with caplog.at_level(logging.WARNING, logger="libs.logging.event_utils"):
        track_event_if_configured("ShouldNotSend")

    assert any(
        "azure-monitor-events-extension" in rec.message for rec in caplog.records
    ), "expected a one-time warning about the missing SDK"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_forwards_to_sdk_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
    fake_track = _install_fake_sdk()

    track_event_if_configured("MyEvent", {"foo": "bar", "n": "42"})

    fake_track.assert_called_once_with("MyEvent", {"foo": "bar", "n": "42"})


def test_forwards_empty_props_as_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
    fake_track = _install_fake_sdk()

    track_event_if_configured("NoProps")

    fake_track.assert_called_once_with("NoProps", {})


def test_swallows_sdk_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(APP_INSIGHTS_CONN_STRING_ENV, FAKE_CONN)
    fake = types.ModuleType("azure.monitor.events.extension")
    fake.track_event = MagicMock(side_effect=RuntimeError("network error"))  # type: ignore[attr-defined]
    sys.modules["azure.monitor.events.extension"] = fake

    # Should NOT raise even though the SDK does.
    with caplog.at_level(logging.ERROR, logger="libs.logging.event_utils"):
        track_event_if_configured("FailingEvent", {"k": "v"})

    fake.track_event.assert_called_once()  # type: ignore[attr-defined]
    assert any(
        "Failed to publish" in rec.message for rec in caplog.records
    ), "expected an error log when the SDK raised"
