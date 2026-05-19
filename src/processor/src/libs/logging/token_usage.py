# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Token-usage extraction + accumulation + emission for processor agents.

This module is the single place that knows how to:

1. Pull ``(prompt_tokens, completion_tokens, total_tokens)`` out of the
   many possible shapes an ``agent_framework`` streaming update can take
   (``Content.usage_details``, ``raw_representation.usage`` on the
   underlying OpenAI SDK object, ``additional_properties.usage`` on a
   ``ChatMessage``, etc.).
2. Accumulate per-agent, per-model, and overall totals across the
   lifetime of a single workflow run.
3. Emit Application Insights ``customEvents`` (``LLMTokenUsageSummary``,
   ``LLMAgentTokenUsage``, ``LLMModelTokenUsage``) at workflow
   completion, gated by
   :func:`libs.logging.event_utils.track_event_if_configured`.

Privacy posture
---------------
By design this tracker stamps ``session_id`` and ``process_id`` on every
emitted event but does NOT stamp ``upn``, ``email``, ``oid``, or any
other user-identifying field. ``session_id`` / ``process_id`` is the
per-request opaque identifier this app already mints per migration run
and is the closest practical proxy for "per user" without ingesting
user PII into Application Insights.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping

from libs.logging.event_utils import track_event_if_configured

logger = logging.getLogger(__name__)

# Custom-event names. These string literals are also referenced verbatim
# by the KQL queries in ``infra/dashboards/token-usage-queries.kql``;
# changing them here without updating the queries will silently break
# the dashboard.
EVENT_TOKEN_USAGE_SUMMARY = "LLMTokenUsageSummary"
EVENT_AGENT_TOKEN_USAGE = "LLMAgentTokenUsage"
EVENT_MODEL_TOKEN_USAGE = "LLMModelTokenUsage"

# Environment fallback for "what model is this processor talking to" when
# the orchestrator doesn't pass one explicitly. The processor reads this
# via ``agent_framework_helper.settings.get_service_config('default').chat_deployment_name``,
# which in turn is sourced from this same env var, so falling back here
# keeps the dimension populated even when callers forget to thread it.
_MODEL_ENV_FALLBACKS = (
    "AZURE_OPENAI_DEPLOYMENT_NAME",
    "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME",
    "AZURE_OPENAI_MODEL",
)


def _resolve_default_model() -> str:
    for key in _MODEL_ENV_FALLBACKS:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _extract_from_usage_dict(d: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """Coerce a usage-shaped mapping into ``(prompt, completion, total)``."""
    if not isinstance(d, Mapping):
        return None
    inp = (
        d.get("input_token_count")
        or d.get("prompt_tokens")
        or d.get("input_tokens")
        or 0
    )
    out = (
        d.get("output_token_count")
        or d.get("completion_tokens")
        or d.get("output_tokens")
        or 0
    )
    tot = (
        d.get("total_token_count")
        or d.get("total_tokens")
        or (int(inp) + int(out))
    )
    try:
        inp_i, out_i, tot_i = int(inp), int(out), int(tot)
    except (TypeError, ValueError):
        return None
    if tot_i <= 0:
        return None
    return inp_i, out_i, tot_i


def _extract_from_usage_object(obj: Any) -> tuple[int, int, int] | None:
    """Coerce a usage-shaped *attribute* object (e.g. OpenAI SDK's ``Usage``)."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return _extract_from_usage_dict(obj)
    inp = (
        getattr(obj, "prompt_tokens", None)
        or getattr(obj, "input_token_count", None)
        or getattr(obj, "input_tokens", None)
        or 0
    )
    out = (
        getattr(obj, "completion_tokens", None)
        or getattr(obj, "output_token_count", None)
        or getattr(obj, "output_tokens", None)
        or 0
    )
    tot = (
        getattr(obj, "total_tokens", None)
        or getattr(obj, "total_token_count", None)
        or (int(inp or 0) + int(out or 0))
    )
    try:
        inp_i, out_i, tot_i = int(inp or 0), int(out or 0), int(tot or 0)
    except (TypeError, ValueError):
        return None
    if tot_i <= 0:
        return None
    return inp_i, out_i, tot_i


def extract_usage_from_update(event_data: Any) -> tuple[int, int, int] | None:
    """Best-effort extraction of token usage from a streaming update.

    Returns ``None`` if no usage data is present (which is the common
    case for plain text-delta chunks). Callers should treat repeated
    ``None`` returns as normal and only act on tuples.

    Search order — covers every shape we've seen in the wild:

    1. ``event_data.contents[i].usage_details`` (agent_framework ``Content``
       carrying a usage block).
    2. ``event_data.contents[i]`` as a plain dict with ``usage_details``
       or top-level usage keys.
    3. ``event_data.raw_representation.usage`` (raw OpenAI SDK response
       object that the agent_framework forwards through).
    4. ``event_data.raw_representation['usage']`` (dict form).
    5. ``event_data.additional_properties['usage']`` (some ``ChatMessage``
       subclasses surface usage there).
    6. ``event_data.usage`` directly (defensive last-resort).
    """
    if event_data is None:
        return None

    # 1+2: contents iterable
    contents = getattr(event_data, "contents", None)
    if contents:
        for item in contents:
            usage_details = getattr(item, "usage_details", None)
            result = _extract_from_usage_object(usage_details)
            if result:
                return result

            if isinstance(item, Mapping):
                if "usage_details" in item:
                    result = _extract_from_usage_dict(item["usage_details"])
                    if result:
                        return result
                # Top-level usage keys directly on the dict
                if any(
                    k in item
                    for k in (
                        "input_token_count",
                        "total_token_count",
                        "prompt_tokens",
                        "total_tokens",
                    )
                ):
                    result = _extract_from_usage_dict(item)
                    if result:
                        return result

    # 3+4: raw_representation
    raw = getattr(event_data, "raw_representation", None)
    if raw is not None:
        result = _extract_from_usage_object(getattr(raw, "usage", None))
        if result:
            return result
        if isinstance(raw, Mapping) and "usage" in raw:
            result = _extract_from_usage_object(raw["usage"])
            if result:
                return result

    # 5: additional_properties
    addl = getattr(event_data, "additional_properties", None)
    if isinstance(addl, Mapping) and "usage" in addl:
        result = _extract_from_usage_object(addl["usage"])
        if result:
            return result

    # 6: bare .usage
    result = _extract_from_usage_object(getattr(event_data, "usage", None))
    if result:
        return result

    return None


# ---------------------------------------------------------------------------
# Accumulation + emission
# ---------------------------------------------------------------------------


@dataclass
class _UsageBucket:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    invocations: int = 0
    model_deployment_name: str = ""

    def add(self, inp: int, out: int, tot: int, model: str = "") -> None:
        self.input_tokens += inp
        self.output_tokens += out
        self.total_tokens += tot
        self.invocations += 1
        # Preserve the first non-empty model name we see, but allow late
        # arrivals to overwrite "" so we don't lose the dimension entirely
        # when the first update doesn't carry it.
        if model and not self.model_deployment_name:
            self.model_deployment_name = model


@dataclass
class TokenUsageTracker:
    """Per-workflow accumulator + App Insights emitter.

    One instance per :class:`GroupChatOrchestrator` ``run_stream`` call.
    Construct it at the start of the run, call :meth:`record` for every
    streaming update, and call :meth:`emit` exactly once when the run
    ends (success or failure).

    Parameters
    ----------
    team:
        Logical "team" / orchestrator name. In this repo we map the
        ``GroupChatOrchestrator.name`` straight onto this dimension, e.g.
        ``"analysis_workflow"``, ``"design_workflow"``,
        ``"convert_workflow"``, ``"documentation_workflow"``.
    session_id:
        Stable per-request identifier used as the user-proxy dimension.
        In this repo this is the ``process_id`` minted by the queue
        service for each migration run.
    process_id:
        Carried separately for KQL queries that want to correlate token
        usage with the existing ``ProcessStatus`` documents in Cosmos.
        Identical to ``session_id`` for this app.
    default_model:
        Fallback model deployment name when an individual update doesn't
        carry one. Sourced from the relevant ``AZURE_OPENAI_*`` env var
        when not passed in.
    """

    team: str
    session_id: str
    process_id: str = ""
    default_model: str = ""
    _by_agent: MutableMapping[str, _UsageBucket] = field(default_factory=dict)
    _by_model: MutableMapping[str, _UsageBucket] = field(default_factory=dict)
    _cumulative: _UsageBucket = field(default_factory=_UsageBucket)
    _emitted: bool = False

    def __post_init__(self) -> None:
        if not self.process_id:
            self.process_id = self.session_id
        if not self.default_model:
            self.default_model = _resolve_default_model()

    # -- Mutation -----------------------------------------------------------

    def record(
        self,
        event_data: Any,
        agent_name: str,
        model_deployment_name: str | None = None,
    ) -> tuple[int, int, int] | None:
        """Extract usage from ``event_data`` and accumulate it.

        Returns the extracted tuple (or ``None`` if no usage was found),
        which the orchestrator may use for structured logging.
        """
        usage = extract_usage_from_update(event_data)
        if not usage:
            return None
        inp, out, tot = usage
        model = (model_deployment_name or self.default_model or "").strip()

        agent_bucket = self._by_agent.setdefault(agent_name, _UsageBucket())
        agent_bucket.add(inp, out, tot, model)

        if model:
            model_bucket = self._by_model.setdefault(model, _UsageBucket())
            model_bucket.add(inp, out, tot, model)

        self._cumulative.add(inp, out, tot, model)

        logger.debug(
            "[TOKEN] agent=%s model=%s +%d/+%d/+%d (cumulative %d/%d/%d)",
            agent_name,
            model or "<unknown>",
            inp,
            out,
            tot,
            self._cumulative.input_tokens,
            self._cumulative.output_tokens,
            self._cumulative.total_tokens,
        )
        return usage

    # -- Inspection (for tests / debug logging) -----------------------------

    @property
    def cumulative(self) -> tuple[int, int, int]:
        c = self._cumulative
        return c.input_tokens, c.output_tokens, c.total_tokens

    @property
    def per_agent(self) -> Mapping[str, _UsageBucket]:
        return dict(self._by_agent)

    @property
    def per_model(self) -> Mapping[str, _UsageBucket]:
        return dict(self._by_model)

    def is_empty(self) -> bool:
        return self._cumulative.total_tokens <= 0

    # -- Emission -----------------------------------------------------------

    def emit(self) -> None:
        """Emit summary / per-agent / per-model custom events.

        Idempotent: subsequent calls after the first are no-ops. Safe to
        call from a ``finally`` block. Never raises.
        """
        if self._emitted:
            return
        self._emitted = True

        if self.is_empty():
            logger.debug(
                "[TOKEN] No usage recorded for session_id=%s; skipping emit.",
                self.session_id,
            )
            return

        base_dims: dict[str, str] = {
            "team": self.team or "",
            "session_id": self.session_id or "",
            "process_id": self.process_id or "",
        }

        try:
            track_event_if_configured(
                EVENT_TOKEN_USAGE_SUMMARY,
                {
                    **base_dims,
                    "model": self._cumulative.model_deployment_name
                    or self.default_model
                    or "",
                    "prompt_tokens": str(self._cumulative.input_tokens),
                    "completion_tokens": str(self._cumulative.output_tokens),
                    "total_tokens": str(self._cumulative.total_tokens),
                    "agent_count": str(len(self._by_agent)),
                    "model_count": str(len(self._by_model)),
                    "invocations": str(self._cumulative.invocations),
                },
            )
        except Exception:
            logger.exception("[TOKEN] Failed to emit summary event.")

        for agent_name, bucket in self._by_agent.items():
            try:
                track_event_if_configured(
                    EVENT_AGENT_TOKEN_USAGE,
                    {
                        **base_dims,
                        "agent": agent_name,
                        "model": bucket.model_deployment_name
                        or self.default_model
                        or "",
                        "prompt_tokens": str(bucket.input_tokens),
                        "completion_tokens": str(bucket.output_tokens),
                        "total_tokens": str(bucket.total_tokens),
                        "invocations": str(bucket.invocations),
                    },
                )
            except Exception:
                logger.exception(
                    "[TOKEN] Failed to emit per-agent event (agent=%s).", agent_name
                )

        for model_name, bucket in self._by_model.items():
            try:
                track_event_if_configured(
                    EVENT_MODEL_TOKEN_USAGE,
                    {
                        **base_dims,
                        "model": model_name,
                        "prompt_tokens": str(bucket.input_tokens),
                        "completion_tokens": str(bucket.output_tokens),
                        "total_tokens": str(bucket.total_tokens),
                        "invocations": str(bucket.invocations),
                    },
                )
            except Exception:
                logger.exception(
                    "[TOKEN] Failed to emit per-model event (model=%s).", model_name
                )

        logger.info(
            "[TOKEN SUMMARY] team=%s session_id=%s total=%d/%d/%d agents=%d models=%d",
            self.team,
            self.session_id,
            self._cumulative.input_tokens,
            self._cumulative.output_tokens,
            self._cumulative.total_tokens,
            len(self._by_agent),
            len(self._by_model),
        )
