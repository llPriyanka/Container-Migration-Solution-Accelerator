# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for :mod:`libs.logging.bootstrap`.

These tests cover the Azure Monitor / OpenTelemetry bootstrap that the
processor runs at startup so token-usage custom events actually reach
Application Insights. They use a ``sys.modules`` fake for
``azure.monitor.opentelemetry`` so the real Azure Monitor SDK is never
imported at test time — keeping the suite hermetic, fast, and free of
network/export side effects.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest

from libs.logging import bootstrap


@pytest.fixture(autouse=True)
def _reset_latch():
    """Reset the module-level success latch before each test."""
    bootstrap.reset_for_tests()
    yield
    bootstrap.reset_for_tests()


@pytest.fixture
def fake_azure_monitor(monkeypatch: pytest.MonkeyPatch):
    """Provide a fake ``azure.monitor.opentelemetry`` module.

    Returns the fake ``configure_azure_monitor`` function so tests can
    assert how often / with what arguments it was called.
    """
    calls: list[dict[str, Any]] = []

    def fake_configure(**kwargs: Any) -> None:
        calls.append(kwargs)

    fake_module = types.ModuleType("azure.monitor.opentelemetry")
    fake_module.configure_azure_monitor = fake_configure  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", fake_module)
    return calls


def test_no_op_when_connection_string_unset(
    monkeypatch: pytest.MonkeyPatch,
    fake_azure_monitor: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
):
    """Unset env -> skip; do NOT call configure; do NOT latch."""
    monkeypatch.delenv(bootstrap.APP_INSIGHTS_CONN_STRING_ENV, raising=False)

    with caplog.at_level(logging.INFO, logger=bootstrap.__name__):
        result = bootstrap.configure_azure_monitor_if_enabled()

    assert result is False
    assert fake_azure_monitor == []
    assert bootstrap._azure_monitor_configured is False
    assert any(
        "APPLICATIONINSIGHTS_CONNECTION_STRING not set" in rec.getMessage()
        for rec in caplog.records
    )


def test_whitespace_only_connection_string_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
    fake_azure_monitor: list[dict[str, Any]],
):
    """Blank-but-set env is treated identically to unset."""
    monkeypatch.setenv(bootstrap.APP_INSIGHTS_CONN_STRING_ENV, "   ")

    result = bootstrap.configure_azure_monitor_if_enabled()

    assert result is False
    assert fake_azure_monitor == []
    assert bootstrap._azure_monitor_configured is False


def test_configures_azure_monitor_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
    fake_azure_monitor: list[dict[str, Any]],
):
    """Env set -> configure_azure_monitor called once with the value + live metrics."""
    monkeypatch.setenv(
        bootstrap.APP_INSIGHTS_CONN_STRING_ENV,
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )

    result = bootstrap.configure_azure_monitor_if_enabled()

    assert result is True
    assert len(fake_azure_monitor) == 1
    assert (
        fake_azure_monitor[0]["connection_string"]
        == "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    )
    assert fake_azure_monitor[0]["enable_live_metrics"] is True
    assert bootstrap._azure_monitor_configured is True


def test_idempotent_after_success(
    monkeypatch: pytest.MonkeyPatch,
    fake_azure_monitor: list[dict[str, Any]],
):
    """Two successive successful calls only invoke configure once."""
    monkeypatch.setenv(
        bootstrap.APP_INSIGHTS_CONN_STRING_ENV,
        "InstrumentationKey=abc",
    )

    bootstrap.configure_azure_monitor_if_enabled()
    bootstrap.configure_azure_monitor_if_enabled()
    bootstrap.configure_azure_monitor_if_enabled()

    assert len(fake_azure_monitor) == 1


def test_does_not_latch_on_missing_env(
    monkeypatch: pytest.MonkeyPatch,
    fake_azure_monitor: list[dict[str, Any]],
):
    """A first failed (env-unset) call does not block a later successful call."""
    monkeypatch.delenv(bootstrap.APP_INSIGHTS_CONN_STRING_ENV, raising=False)
    assert bootstrap.configure_azure_monitor_if_enabled() is False

    monkeypatch.setenv(
        bootstrap.APP_INSIGHTS_CONN_STRING_ENV,
        "InstrumentationKey=later",
    )
    assert bootstrap.configure_azure_monitor_if_enabled() is True
    assert len(fake_azure_monitor) == 1


def test_does_not_latch_on_sdk_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A configure_azure_monitor exception is swallowed and does NOT latch."""
    calls: list[dict[str, Any]] = []

    def raising_then_ok(**kwargs: Any) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("simulated SDK boom")

    fake_module = types.ModuleType("azure.monitor.opentelemetry")
    fake_module.configure_azure_monitor = raising_then_ok  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", fake_module)
    monkeypatch.setenv(
        bootstrap.APP_INSIGHTS_CONN_STRING_ENV,
        "InstrumentationKey=boom",
    )

    with caplog.at_level(logging.ERROR, logger=bootstrap.__name__):
        first = bootstrap.configure_azure_monitor_if_enabled()

    assert first is False
    assert bootstrap._azure_monitor_configured is False
    assert any(
        "configure_azure_monitor raised" in rec.getMessage()
        for rec in caplog.records
    )

    # Second call should retry (no latch) and succeed.
    second = bootstrap.configure_azure_monitor_if_enabled()
    assert second is True
    assert len(calls) == 2


def test_handles_missing_sdk_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """ImportError on the SDK is caught and logged; no latch."""
    monkeypatch.setenv(
        bootstrap.APP_INSIGHTS_CONN_STRING_ENV,
        "InstrumentationKey=xyz",
    )

    # Force an ImportError by inserting a sentinel that raises on access.
    class _RaisingModule(types.ModuleType):
        def __getattr__(self, name: str) -> Any:
            raise ImportError(f"simulated missing attr {name!r}")

    monkeypatch.setitem(
        sys.modules,
        "azure.monitor.opentelemetry",
        _RaisingModule("azure.monitor.opentelemetry"),
    )

    with caplog.at_level(logging.WARNING, logger=bootstrap.__name__):
        result = bootstrap.configure_azure_monitor_if_enabled()

    assert result is False
    assert bootstrap._azure_monitor_configured is False
