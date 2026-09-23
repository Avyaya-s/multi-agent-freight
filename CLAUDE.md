# CLAUDE.md

Standing decisions for the freight marketplace simulation. Do not relitigate
these without the user's explicit sign-off. See `README.md` for the full data
model, event list, and current experimental results -- this file is a short
index of decisions and findings that must survive a fresh session, not a
duplicate of the README.

## Standing design decisions

- **Three-tier visibility, enforced at the agent boundary.** `Truck.public`
  (static vehicle facts, visible to everyone), `Truck.platform` (dynamic
  state -- location, status, spare capacity -- visible to the marketplace
  agent only, never to competing truckers), `Truck.private` (costs,
  reservation price, personal constraints -- visible only to the truck's own
  agent). `Load.public` / `Load.private` mirrors this on the shipper side.
  The event log is a separate, omniscient concern and may carry private
  fields (it's the simulator's own record, not a channel between agents) --
  the tiers matter for what an *agent* can read, not what gets logged.
  Guarded behaviorally, not just by naming convention, by
  `tests/test_privacy.py` (a tripwire object that raises if a private field
  is ever read by code that shouldn't see it -- currently covers
  `propose_candidates` and `verify_match`; extend it whenever Stage 2 adds a
  new agent-facing function).

- **Veto reason is an enum, not free text.** `VetoReason` = `TIME_WINDOW |
  CAPACITY | DETOUR_LIMIT | HOME_DEADLINE`. The optimiser
  (`matching.verify_match`) is the only place a match can be vetoed; the
  marketplace's coarse screen (`matching.propose_candidates`) only ever
  proposes, using public/platform info only.

- **The event log is the sole source of truth.** All metrics are computed
  from the log (`metrics.py`, `zone_metrics.py`), never from live simulator
  state. Every event carries a monotonic `seq` (multiple events can share a
  `sim_time`) and a `schema_version`, so later stages can replay Stage 0 logs.

- **`Deal` holds final agreed terms only.** Negotiation history (Stage 2+)
  will live in the event log under a `negotiation_id`, not on `Deal` itself
  -- keeps the schema stable as later stages add real back-and-forth.

- **Report relative differences and sensitivity, not absolute figures.**
  Goal is structural realism, not numeric accuracy -- the project's own
  stated premise. Never claim absolute rupee savings; always report a
  comparison against a baseline plus a sensitivity sweep (seeds, and the
  parameter actually being tested), not a single point estimate. A single
  seed or a single sweep statistic is not a result on its own -- see findings
  below.

## Findings that should shape Stage 1+ (don't rediscover these)

- **A single aggregate metric can hide the effect you're testing for.**
  Sweeping the demand generator's imbalance ratio from 50:50 to 80:20 left
  the *fleet-wide mean* empty-km reduction flat (43.6% -> 38.0% -> 38.8% ->
  42.0%). That looked like the marketplace's benefit had nothing to do with
  directional imbalance. It didn't: a *zone-level* breakdown
  (`zone_metrics.py`, `scripts/run_zone_imbalance_sweep.py`) showed the
  backhaul-success-rate gap between a net-exporter zone (Peenya) and a
  net-importer zone (Whitefield) widening from 3.0pp at 50:50 to 73.9pp at
  80:20, exactly as predicted before running. The fleet-wide mean was flat
  because exporter-zone and importer-zone trucks are affected in opposite,
  largely canceling directions -- not because the simulator is insensitive to
  imbalance. **Whenever a fleet-wide aggregate looks suspiciously flat or
  suspiciously clean, check the distribution by a relevant grouping (zone,
  truck type, home base) before concluding the effect isn't there.**

- **Single-seed and single-setting results are unreliable; always sweep.**
  Across 20 seeds, empty-km reduction had real spread (double-digit
  percentage-point range) around the mean. Report mean ± spread, not a
  single run.

- **A loose coarse filter costs real compute for nothing, and it's easy not
  to notice.** The marketplace's coarse candidate screen used a flat
  straight-line distance cap that didn't account for how much real roads
  inflate over straight-line distance in this network (measured at 1.3-1.9x).
  Result: `DETOUR_LIMIT` accounted for 77% of all vetoes, and vetoes
  outnumbered successful matches ~2:1, before any real (Stage 1) feasibility
  engine even existed. Recalibrating the screen's threshold using the
  measured inflation factor (still a legitimate network-wide, public
  calibration -- it uses no truck's private threshold) cut total vetoes by
  75%. Separately, widening the trucks' own private `max_detour_km` range
  did *not* fix this and does not need to change -- once the coarse filter
  was fixed, the truck-level threshold was no longer the binding constraint.
  **Before adding a real (e.g. OR-Tools) feasibility engine in Stage 1,
  check that whatever proposes candidates to it isn't already routinely
  proposing pairs the real engine was never going to accept -- that cost is
  much higher once "real" means an actual solver call.**

- **This project now has two separate headline results, not one.** The
  aggregate empty-km reduction (~40% mean) is an *efficiency* story and is
  roughly imbalance-independent. The zone-level backhaul-success gap is an
  *equity* story and is highly imbalance-dependent: as imbalance grows, the
  efficiency gain concentrates on trucks based in exporter zones while
  importer-zone trucks are left behind. Don't collapse these into one number
  in a future report -- they answer different questions, and the second one
  is what connects to the coalition/fairness stages later in the plan.

- **Prediction on record before Stage 1 runs (check this, don't skip it):**
  real (OR-Tools) feasibility checking is expected to reduce the ~40%
  headline somewhat, since Stage 0's arithmetic `verify_match` is a more
  permissive approximation than an actual routing engine with time-window
  propagation. If Stage 1's number doesn't move at all, treat that as a sign
  the new feasibility layer isn't binding on anything (a bug or a no-op
  integration), not as a result to report.

## Where to look for more detail

- `README.md` -- data model, event types, full results tables, and the
  reasoning behind every finding above
- `scripts/run_stage0_diagnostics.py` -- reproduces every check in this file
  in one process
