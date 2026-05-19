# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Azure Monitor bootstrap for the migration processor.

The processor emits Application Insights custom events from
:mod:`libs.logging.token_usage` via
:func:`libs.logging.event_utils.track_event_if_configured`. Those events
only land in App Insights if ``configure_azure_monitor`` has been
invoked first to set up the OpenTelemetry log/trace/metric exporter
pipeline. Backend-api does this in ``Application._configure_azure_monitor``;
the processor needs an equivalent linkage step.

This module provides that step as a single function
:func:`configure_azure_monitor_if_enabled` that is safe to call from
both processor entrypoints (``main.py`` for local development and
``main_service.py`` for the queue-based Docker container).

Behaviour
---------
* Reads ``APPLICATIONINSIGHTS_CONNECTION_STRING`` from the environment
  on every call (so callers may load ``.env`` / App Configuration
  between import time and the first invocation).
* If the variable is unset / whitespace, logs at ``INFO`` and returns
  without importing the Azure Monitor SDK. The token-usage tracker will
  then no-op as designed.
* If set, lazily imports ``azure.monitor.opentelemetry.configure_azure_monitor``
  and invokes it with ``enable_live_metrics=True``. The processor is
  NOT a FastAPI app, so we do NOT install FastAPIInstrumentor or the
  ASGI-specific span filters used by backend-api.
* Catches and logs any exception raised by the SDK so a misconfigured
  connection string can never crash startup. Telemetry must never break
  the queue worker.
* Idempotent: a module-level success-only latch ensures repeated calls
  (tests, multiple entrypoint paths, recursive imports) result in at
  most one successful configure. Failed attempts do NOT latch, so a
  later call with a corrected configuration can still succeed.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Environment variable that ``configure_azure_monitor`` keys off. Mirrors
# the constant in ``libs.logging.event_utils`` so the gating decision is
# made against the same source of truth on both sides.
APP_INSIGHTS_CONN_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"

# Module-level success-only latch. We deliberately do NOT latch on
# missing-env / import-error / SDK-exception paths so that a later call
# with a corrected configuration can still succeed.
_azure_monitor_configured: bool = False


def reset_for_tests() -> None:
    """Test-only helper: reset the idempotency latch."""
    global _azure_monitor_configured
    _azure_monitor_configured = False


def configure_azure_monitor_if_enabled() -> bool:
    """Initialise Azure Monitor OpenTelemetry exporter, if configured.

    Returns
    -------
    bool
        ``True`` if Azure Monitor was successfully configured (either by
        this call or a previous successful call in the same process),
        ``False`` if the bootstrap was skipped (env unset, import error,
        or SDK exception).

    Notes
    -----
    Safe to call from multiple entrypoints. Successful configuration is
    latched so calling ``configure_azure_monitor`` twice — which the
    Azure Monitor SDK does not support and which produces a noisy
    warning — is prevented. A failed attempt does not latch, so a later
    retry after fixing the configuration can succeed.
    """
    global _azure_monitor_configured

    if _azure_monitor_configured:
        return True

    connection_string = (
        os.environ.get(APP_INSIGHTS_CONN_STRING_ENV) or ""
    ).strip()
    if not connection_string:
        logger.info(
            "APPLICATIONINSIGHTS_CONNECTION_STRING not set; "
            "skipping Azure Monitor OpenTelemetry configuration. "
            "Token-usage events will not be emitted to Application Insights."
        )
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        logger.warning(
            "azure-monitor-opentelemetry is not installed; "
            "skipping Azure Monitor OpenTelemetry configuration."
        )
        return False

    try:
        configure_azure_monitor(
            connection_string=connection_string,
            enable_live_metrics=True,
        )
    except Exception:  # noqa: BLE001 — telemetry must never break startup
        logger.exception(
            "configure_azure_monitor raised; "
            "continuing without Application Insights export."
        )
        return False

    _azure_monitor_configured = True
    logger.info(
        "Azure Monitor OpenTelemetry configured for processor; "
        "token-usage events will be exported to Application Insights."
    )
    return True
