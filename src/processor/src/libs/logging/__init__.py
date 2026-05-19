"""Optional Application Insights / OpenTelemetry helpers for the processor.

All helpers in this package are *gated*: they no-op cleanly when the
``APPLICATIONINSIGHTS_CONNECTION_STRING`` environment variable is unset
or when the Azure Monitor events extension SDK is not importable.
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
