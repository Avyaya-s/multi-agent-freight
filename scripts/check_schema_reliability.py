"""Check 1 before scaling past one negotiation: schema reliability.

~20 raw calls to the counterparty (small) model against the negotiation
schema, report the failure rate. If it's more than a few percent, move the
fleet up a model size -- a high fallback rate means we're demoing
rule-based agents in costume, not LLM negotiators. See CLAUDE.md.

Requires GROQ_API_KEY. Costs ~20 real API calls against the daily budget.
"""

import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import load_config
from freight_sim.llm_backend import GroqBackend, LLMCallError, LLMSchemaError
from freight_sim.negotiation import SYSTEM_PROMPT, NegotiationTurnSchema
from freight_sim.rate_limiter import RateLimiter

ROOT = Path(__file__).resolve().parent.parent
NUM_CALLS = 20

SAMPLE_PROMPTS = [
    "Your rate threshold: you need the rate at or above 12.00 rupees/km.\n"
    "Load: 5000kg / 15.0m3, posted at 13.00 rupees/km.\n"
    "Detour to pickup: 5.0km. Pickup ETA 09:00, delivery ETA 11:00.\n"
    "Round 1 of 4.\nThis is your opening move.\nRespond with your next move as JSON.",
    "Your rate threshold: you need the rate at or below 14.00 rupees/km.\n"
    "Truck capacity: 8000kg / 25.0m3.\nPickup ETA 09:00, delivery ETA 11:00.\n"
    "Round 2 of 4.\nThe other party just sent: offer at 12.0.\nRespond with your next move as JSON.",
]


def main() -> None:
    config, _ = load_config(ROOT / "config" / "default.yaml")
    backend = GroqBackend()
    model = config.llm.counterparty.model
    limiter = RateLimiter(
        requests_per_minute=config.llm.counterparty.requests_per_minute,
        requests_per_day=config.llm.counterparty.requests_per_day,
        tokens_per_minute=config.llm.counterparty.tokens_per_minute,
    )

    print(f"Model under test: {model}\n")

    failures = 0
    errors = 0
    for i in range(NUM_CALLS):
        prompt = SAMPLE_PROMPTS[i % len(SAMPLE_PROMPTS)]
        limiter.wait_if_needed(estimated_tokens=200)
        try:
            response = backend.complete(SYSTEM_PROMPT, prompt, model, config.llm.timeout_seconds)
        except LLMSchemaError:
            failures += 1
            print(f"  call {i + 1}: SCHEMA FAILURE (invalid JSON)")
            continue
        except LLMCallError as e:
            errors += 1
            print(f"  call {i + 1}: API ERROR ({e})")
            continue

        limiter.record_request(response.usage.total_tokens)
        limiter.record_headers(response.headers)

        try:
            NegotiationTurnSchema.model_validate(response.parsed)
            print(f"  call {i + 1}: OK -- {response.parsed}")
        except ValidationError as e:
            failures += 1
            print(f"  call {i + 1}: SCHEMA FAILURE (shape mismatch): {e}")

    print(f"\n{NUM_CALLS} calls: {failures} schema failures ({failures / NUM_CALLS:.1%}), {errors} API errors")
    if failures / NUM_CALLS > 0.05:
        print("Failure rate > 5% -- consider moving the counterparty fleet up a model size.")
    else:
        print("Failure rate acceptable for the counterparty fleet model as configured.")


if __name__ == "__main__":
    main()
