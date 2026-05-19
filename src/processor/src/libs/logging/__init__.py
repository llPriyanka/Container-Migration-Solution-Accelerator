# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Application-level logging and telemetry helpers.

This subpackage hosts the small Application Insights / OpenTelemetry
integration helpers used by the processor application:

- ``event_utils`` — a tiny wrapper around
  ``azure.monitor.events.extension.track_event`` that no-ops when the
  ``APPLICATIONINSIGHTS_CONNECTION_STRING`` environment variable is not
  configured. Callers can therefore emit structured events from
  any router/service without conditionally guarding each call site.

Nothing in this subpackage imports Azure SDKs at module-import time, so
it is safe to import from contexts where the App Insights SDK may not be
fully wired up yet (e.g. application bootstrap before
``configure_azure_monitor`` has run).
"""

from libs.logging.event_utils import (  # noqa: F401
    APP_INSIGHTS_CONN_STRING_ENV,
    reset_unconfigured_warning_for_tests,
    track_event_if_configured,
)
from libs.logging.token_usage import (  # noqa: F401
    EVENT_AGENT_TOKEN_USAGE,
    EVENT_MODEL_TOKEN_USAGE,
    EVENT_TOKEN_USAGE_SUMMARY,
    TokenUsageTracker,
    extract_usage_from_update,
)
