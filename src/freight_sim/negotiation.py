"""The negotiation protocol and the two strategies that can drive either side
of it.

One `NegotiationStrategy` interface serves both trucker and shipper: a
trucker wants the rate at or above its reservation (a "floor"), a shipper
wants it at or below its own private ceiling (a "ceiling") -- `direction` on
`NegotiationContext` is the only thing that differs, so one implementation
of each strategy serves both roles rather than duplicating logic per side.

`RuleBasedStrategy` is Stage 1's deterministic logic (rate threshold +
detour limit + home deadline), now expressed as a negotiation decision
instead of a single accept/reject gate -- and now the LLM's fallback, never
a deliberate experimental condition (see CLAUDE.md, direction change).

`LLMStrategy` wraps an `LLMBackend` + per-model `RateLimiter`: schema
failures and rate-limit hits are retried up to `retry_attempts`, and the
whole thing falls back to `RuleBasedStrategy` on persistent failure -- the
negotiation must never hang or crash on a bad or throttled response.
Schema-failure rate and fallback rate are tracked as separate counts on
`DecisionResult`, because a schema failure a retry then fixes never causes a
fallback at all; pooling the two would hide that distinction.

Free-text reasoning is part of what a strategy returns, but it is the
orchestrator's job (not this module's) to log it as its own event and never
feed it into the other party's prompt -- see `build_user_prompt`, which never
includes it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ValidationError

from freight_sim.events import NegotiationMessageType, NegotiationOutcome, NegotiationParty
from freight_sim.feasibility import FeasibilityResult
from freight_sim.llm_backend import LLMBackend, LLMCallError, LLMRateLimitedError, LLMSchemaError, LLMUsage
from freight_sim.models import Load, Truck
from freight_sim.rate_limiter import RateLimitBudgetExhausted, RateLimiter

SYSTEM_PROMPT = (
    "You are negotiating a freight rate on behalf of one party in an automated "
    "logistics marketplace. Reply with a SINGLE JSON object and nothing else, "
    'matching exactly: {"message_type": "offer"|"counter"|"accept"|"reject"|'
    '"withdraw", "rate_per_km": <number or null>, "reasoning": "<one short '
    'sentence>"}. For accept/reject/withdraw, set rate_per_km to the rate you '
    "are accepting (or null). Keep reasoning to one short sentence -- it is "
    "never shown to the other party. Do not accept terms that violate your "
    "own hard limits."
)


class NegotiationTurnSchema(BaseModel):
    message_type: Literal["offer", "counter", "accept", "reject", "withdraw"]
    rate_per_km: float | None = None
    reasoning: str


@dataclass
class NegotiationTurn:
    message_type: NegotiationMessageType
    rate_per_km: float | None
    reasoning: str


@dataclass
class NegotiationContext:
    party: NegotiationParty
    reservation_rate_per_km: float
    direction: Literal["floor", "ceiling"]  # trucker: floor (wants >=); shipper: ceiling (wants <=)
    max_rounds: int
    counterpart_summary: str  # short text for the LLM prompt; built from PUBLIC data only
    physical_summary: str  # detour/ETA summary, shared because it's derived from public data
    # Hard private limits the trucker side reasons about; always None for the
    # shipper (it has no detour/home-deadline concept). Precomputed once
    # before negotiation starts, not re-derived mid-negotiation.
    detour_km: float | None = None
    max_detour_km: float | None = None
    home_deadline_ok: bool | None = None


@dataclass
class DecisionResult:
    turn: NegotiationTurn
    used_llm: bool
    used_fallback: bool
    schema_failures: int = 0
    was_rate_limited: bool = False
    usage: LLMUsage | None = None
    latency_ms: float | None = None


class NegotiationStrategy(Protocol):
    def decide(self, context: NegotiationContext, round_number: int, incoming: NegotiationTurn | None) -> DecisionResult: ...


def _meets_threshold(rate: float, context: NegotiationContext) -> bool:
    return rate >= context.reservation_rate_per_km if context.direction == "floor" else rate <= context.reservation_rate_per_km


def _hard_veto(context: NegotiationContext) -> NegotiationTurn | None:
    """Detour and home-deadline limits are structural vetoes, not negotiation
    positions -- checked here, outside any strategy, so neither the rule
    fallback nor an LLM call can be the only thing standing between a
    violation and an ACCEPT. An LLM told the threshold in its prompt can
    still choose to ignore it (seen in testing: both counterparty and
    visitor models accepted a deal with the detour over the truck's own
    max_detour_km once, every time, when this check didn't exist yet) --
    these two limits are hard business rules, not something to leave to a
    cooperative model's discretion."""
    if context.max_detour_km is not None and context.detour_km is not None and context.detour_km > context.max_detour_km:
        return NegotiationTurn(NegotiationMessageType.WITHDRAW, None, "Detour exceeds my limit.")
    if context.home_deadline_ok is False:
        return NegotiationTurn(NegotiationMessageType.WITHDRAW, None, "Would miss my home deadline.")
    return None


class RuleBasedStrategy:
    """Deterministic. The Stage 2 fallback -- see module docstring."""

    def decide(self, context: NegotiationContext, round_number: int, incoming: NegotiationTurn | None) -> DecisionResult:
        veto = _hard_veto(context)
        if veto is not None:
            return DecisionResult(veto, used_llm=False, used_fallback=False)

        if incoming is None:
            turn = NegotiationTurn(NegotiationMessageType.OFFER, context.reservation_rate_per_km, "Opening at my own threshold.")
            return DecisionResult(turn, used_llm=False, used_fallback=False)

        if incoming.rate_per_km is not None and _meets_threshold(incoming.rate_per_km, context):
            turn = NegotiationTurn(NegotiationMessageType.ACCEPT, incoming.rate_per_km, "Meets my threshold.")
            return DecisionResult(turn, used_llm=False, used_fallback=False)

        if round_number >= context.max_rounds:
            turn = NegotiationTurn(NegotiationMessageType.REJECT, None, "Out of rounds; doesn't meet my threshold.")
        else:
            turn = NegotiationTurn(NegotiationMessageType.COUNTER, context.reservation_rate_per_km, "Countering at my threshold.")
        return DecisionResult(turn, used_llm=False, used_fallback=False)


def build_user_prompt(context: NegotiationContext, round_number: int, incoming: NegotiationTurn | None) -> str:
    """Only ever built from this party's own context and the counterpart's
    structured message -- never from the counterpart's reasoning text, which
    this function has no access to in the first place."""
    threshold_phrase = "at or above" if context.direction == "floor" else "at or below"
    lines = [
        f"Your rate threshold: you need the rate {threshold_phrase} {context.reservation_rate_per_km:.2f} rupees/km.",
        context.counterpart_summary,
        context.physical_summary,
        f"Round {round_number} of {context.max_rounds}.",
    ]
    if incoming is None:
        # Observed live: the model's default move here is "accept" far more
        # often than makes sense -- there's no offer yet to accept or
        # reject, and LLMStrategy now retries/falls back on that (see
        # negotiation.py), but a retry is still a real API call. Spelling
        # out "offer" cuts those wasted retries rather than just absorbing
        # the cost of catching them after the fact.
        lines.append("This is your opening move: there is no offer on the table yet, so respond with offer (not accept or reject).")
    else:
        lines.append(f"The other party just sent: {incoming.message_type.value} at {incoming.rate_per_km}.")
    lines.append("Respond with your next move as JSON.")
    return "\n".join(lines)


class LLMStrategy:
    def __init__(
        self,
        backend: LLMBackend,
        rate_limiter: RateLimiter,
        model: str,
        timeout_seconds: float,
        retry_attempts: int,
        fallback: NegotiationStrategy | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ):
        self._backend = backend
        self._rate_limiter = rate_limiter
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = retry_attempts
        self._fallback = fallback or RuleBasedStrategy()
        self._sleep_fn = sleep_fn or time.sleep

    def decide(self, context: NegotiationContext, round_number: int, incoming: NegotiationTurn | None) -> DecisionResult:
        veto = _hard_veto(context)
        if veto is not None:
            # Short-circuits before spending an LLM call: a structural
            # limit, not a negotiation position. Not a "fallback" in the
            # schema-failure/rate-limit sense -- the LLM was never asked.
            return DecisionResult(veto, used_llm=False, used_fallback=False)

        user_prompt = build_user_prompt(context, round_number, incoming)
        schema_failures = 0
        was_rate_limited = False
        backoff_seconds = 1.0

        max_attempts = self._retry_attempts + 1
        for attempt in range(max_attempts):
            try:
                self._rate_limiter.wait_if_needed(estimated_tokens=len(SYSTEM_PROMPT) // 4 + len(user_prompt) // 4 + 64)
            except RateLimitBudgetExhausted:
                # The daily cap, not a transient burst -- waiting it out isn't
                # an option within a session, so fall back immediately rather
                # than burn remaining retries on something that won't clear.
                was_rate_limited = True
                break

            try:
                response = self._backend.complete(SYSTEM_PROMPT, user_prompt, self._model, self._timeout_seconds)
            except LLMRateLimitedError:
                # A 429 despite our own pacing means local and server counters
                # have drifted -- back off for real before retrying, per the
                # Stage 1 retry-storm lesson: a rate-limited API that gets
                # hammered immediately becomes a stalled session, not just a
                # noisy log. No point sleeping after the last attempt, though
                # -- there's no retry left to wait for, just a fallback.
                was_rate_limited = True
                if attempt < max_attempts - 1:
                    self._sleep_fn(backoff_seconds)
                    backoff_seconds *= 2
                continue
            except LLMSchemaError:
                schema_failures += 1
                continue
            except LLMCallError:
                continue

            self._rate_limiter.record_request(response.usage.total_tokens)
            self._rate_limiter.record_headers(response.headers)

            try:
                validated = NegotiationTurnSchema.model_validate(response.parsed)
            except ValidationError:
                schema_failures += 1
                continue

            if incoming is None and validated.message_type in ("accept", "reject"):
                # There is nothing on the table yet on an opening move -- an
                # LLM that answers "accept" here (seen live: the trucker
                # accepted the posted rate as its very first move, forming a
                # "deal" the shipper's agent never actually participated in)
                # is a malformed response in the same sense a schema mismatch
                # is, not a valid negotiation position. Retry like any other
                # schema failure rather than let it through.
                schema_failures += 1
                continue

            turn = NegotiationTurn(
                message_type=NegotiationMessageType(validated.message_type),
                rate_per_km=validated.rate_per_km,
                reasoning=validated.reasoning,
            )
            return DecisionResult(
                turn,
                used_llm=True,
                used_fallback=False,
                schema_failures=schema_failures,
                was_rate_limited=was_rate_limited,
                usage=response.usage,
                latency_ms=response.latency_ms,
            )

        fallback_result = self._fallback.decide(context, round_number, incoming)
        fallback_result.used_fallback = True
        fallback_result.schema_failures = schema_failures
        fallback_result.was_rate_limited = was_rate_limited
        return fallback_result


@dataclass
class NegotiationResult:
    outcome: NegotiationOutcome
    rounds_used: int
    final_rate_per_km: float | None


EmitFn = Callable[[NegotiationParty, DecisionResult, int], None]


def run_negotiation(
    trucker_context: NegotiationContext,
    trucker_strategy: NegotiationStrategy,
    shipper_context: NegotiationContext,
    shipper_strategy: NegotiationStrategy,
    max_rounds: int,
    emit: EmitFn,
) -> NegotiationResult:
    """Drives OFFER/COUNTER/ACCEPT/REJECT/WITHDRAW to a terminal outcome or
    `max_rounds`. `emit` is called once per turn with (party, decision,
    round_number) so the caller can log NEGOTIATION_MESSAGE + AGENT_REASONING
    (and schema-failure/fallback metadata) however its event log works --
    this module has no EventLog dependency so it stays testable in isolation.
    """
    parties = {
        NegotiationParty.TRUCKER: (trucker_context, trucker_strategy),
        NegotiationParty.SHIPPER: (shipper_context, shipper_strategy),
    }
    current = NegotiationParty.TRUCKER
    incoming: NegotiationTurn | None = None

    for round_number in range(1, max_rounds + 1):
        context, strategy = parties[current]
        result = strategy.decide(context, round_number, incoming)
        emit(current, result, round_number)

        if result.turn.message_type == NegotiationMessageType.ACCEPT:
            return NegotiationResult(NegotiationOutcome.ACCEPTED, round_number, result.turn.rate_per_km)
        if result.turn.message_type == NegotiationMessageType.REJECT:
            return NegotiationResult(NegotiationOutcome.REJECTED, round_number, None)
        if result.turn.message_type == NegotiationMessageType.WITHDRAW:
            return NegotiationResult(NegotiationOutcome.WITHDRAWN, round_number, None)

        incoming = result.turn
        current = NegotiationParty.SHIPPER if current == NegotiationParty.TRUCKER else NegotiationParty.TRUCKER

    return NegotiationResult(NegotiationOutcome.MAX_ROUNDS_EXCEEDED, max_rounds, None)


def build_trucker_context(
    truck: Truck,
    load: Load,
    physical: FeasibilityResult,
    is_backhaul: bool,
    home_deadline_ok: bool | None,
    max_rounds: int,
) -> NegotiationContext:
    """Reads truck.private (this is the trucker's own agent) and load.public
    only -- load.private (the shipper's true reservation price) must never
    appear here. home_deadline_ok is precomputed by the caller (it needs
    `network`, which this module deliberately has no dependency on)."""
    threshold = (
        truck.private.reservation_rate_per_km * (1 - truck.private.return_leg_discount)
        if is_backhaul
        else truck.private.reservation_rate_per_km
    )
    counterpart_summary = (
        f"Load: {load.public.weight_kg:.0f}kg / {load.public.volume_m3:.1f}m3, "
        f"posted at {load.public.posted_rate_per_km:.2f} rupees/km."
    )
    physical_summary = (
        f"Detour to pickup: {physical.detour_km:.1f}km. "
        f"Pickup ETA {physical.pickup_eta.strftime('%H:%M')}, delivery ETA {physical.delivery_eta.strftime('%H:%M')}."
    )
    return NegotiationContext(
        party=NegotiationParty.TRUCKER,
        reservation_rate_per_km=threshold,
        direction="floor",
        max_rounds=max_rounds,
        counterpart_summary=counterpart_summary,
        physical_summary=physical_summary,
        detour_km=physical.detour_km,
        max_detour_km=truck.private.max_detour_km,
        home_deadline_ok=home_deadline_ok,
    )


def build_shipper_context(load: Load, truck: Truck, physical: FeasibilityResult, max_rounds: int) -> NegotiationContext:
    """Reads load.private (this is the shipper's own agent) and truck.public
    only -- truck.private (the trucker's reservation price, detour limit,
    home deadline) must never appear here."""
    counterpart_summary = f"Truck capacity: {truck.public.capacity_weight_kg:.0f}kg / {truck.public.capacity_volume_m3:.1f}m3."
    physical_summary = f"Pickup ETA {physical.pickup_eta.strftime('%H:%M')}, delivery ETA {physical.delivery_eta.strftime('%H:%M')}."
    return NegotiationContext(
        party=NegotiationParty.SHIPPER,
        reservation_rate_per_km=load.private.reservation_rate_per_km,
        direction="ceiling",
        max_rounds=max_rounds,
        counterpart_summary=counterpart_summary,
        physical_summary=physical_summary,
    )
