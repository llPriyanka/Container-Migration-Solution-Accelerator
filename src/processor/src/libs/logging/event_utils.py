# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Gated Application Insights custom-event helper.

``track_event_if_configured`` is a thin wrapper around
``azure.monitor.events.extension.track_event`` that is safe to call from
anywhere in the application:

* If ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is unset, the call is a
  no-op and a single warning is logged per process so operators notice the
  missing configuration without flooding the logs.
* If the optional ``azure-monitor-events-extension`` dependency is not
  installed, the call is also a no-op with a one-time warning. This lets
  the same helper live in containers that have and have not opted into
  custom-event emission.
* Any exception raised by the underlying SDK is swallowed: telemetry must
  never break a request.

The SDK is imported lazily on the first configured invocation so that
import-time cost is paid only when telemetry is actually used, and so
unit tests can replace ``azure.monitor.events.extension`` via
``sys.modules`` injection without monkey-patching the real package.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Env var that ``configure_azure_monitor`` itself keys off — reusing it as
# the single source of truth keeps Bicep, the runtime helper, and the SDK
# in lockstep.
APP_INSIGHTS_CONN_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"

# Module-level flags so the "not configured" / "SDK missing" warnings fire
# exactly once per process. The test helper resets them between tests.
_warned_unconfigured = False
_warned_sdk_missing = False


def reset_unconfigured_warning_for_tests() -> None:
    """Reset the per-process warning latches. Test-only."""
    global _warned_unconfigured, _warned_sdk_missing
    _warned_unconfigured = False
    _warned_sdk_missing = False


def track_event_if_configured(
    name: str, properties: Mapping[str, Any] | None = None
) -> None:
    """Emit an Application Insights custom event when configured.

    Parameters
    ----------
    name:
        The custom-event name. Conventionally ``CamelCase`` (e.g.
        ``LLMTokenUsage``).
    properties:
        Optional mapping of string-coercible custom dimensions. Keys with
        ``None`` or non-string values are passed through to the SDK as-is
        — callers are responsible for stringifying numeric counters when
        the App Insights schema expects strings.

    Notes
    -----
    This function is intentionally fire-and-forget: it never raises and
    never blocks the caller. Operators who depend on full delivery should
    verify the gating env var is set at container start.
    """
    global _warned_unconfigured, _warned_sdk_missing

    conn = os.environ.get(APP_INSIGHTS_CONN_STRING_ENV, "").strip()
    if not conn:
        if not _warned_unconfigured:
            logger.warning(
                "%s is not set; track_event_if_configured(name=%s) is a no-op.",
                APP_INSIGHTS_CONN_STRING_ENV,
                name,
            )
            _warned_unconfigured = True
        return

    try:
        # Lazy import — keeps cold-start cheap and avoids hard-failing the
        # process when the optional extension is missing.
        from azure.monitor.events.extension import track_event  # type: ignore[import-not-found]
    except ImportError:
        if not _warned_sdk_missing:
            logger.warning(
                "azure-monitor-events-extension is not installed; "
                "skipping track_event(name=%s).",
                name,
            )
            _warned_sdk_missing = True
        return

    try:
        track_event(name, dict(properties) if properties else {})
    except Exception:
        # Never break the caller for a telemetry failure.
        logger.exception(
            "Failed to publish Application Insights custom event name=%s.", name
        )
