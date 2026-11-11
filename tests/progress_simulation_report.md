# Controlled interim-message simulation — 2026-11-11

Two synthetic task scenarios, six genuine Anthropic Claude CLI calls, 11:44:27–11:44:55 UTC. All calls reported `claude-haiku-5-5` / firstParty. No actual task execution or message delivery occurred; no production files/configuration were modified. Full prompts, exact outputs, metadata and per-call UTC timestamps are in `progress_simulation_results.json`; harness is `progress_simulation.py`.

## Design

Read Mecha's actual `INTERIM_SYSTEM` and `_judge_interim` from `<remote-host>:~/.hermes/plugins/imessage-plus/__init__.py` (SHA-256 `14397a83867aff1cd3f09e3a9dc68d789a627aab6e04dae71a4d738840229063`). The judge receives **only the candidate text**, as in the actual implementation; its prompt requests `SKIP` on no and rewritten prose on yes. One model call drafts the candidate from identical synthetic task evidence, then an independently prompted single-assistant decision call gets that same candidate and original context. A separate model call applies Mecha's exact judge system prompt to that candidate. This asymmetric context is deliberate and reflects production's judge boundary, but prevents attributing differences solely to decision architecture. Calls used Anthropic CLI, not Mecha's actual configured auxiliary provider/runtime; CLI defaults and token settings do not reproduce Mecha's `max_tokens=80` or 30-second timeout. Thus this is a model-substituted simulation, not a full production replay.

For each judge response we additionally executed the actual read-only extracted `_judge_interim` function locally with a mocked LLM returning the real response and a minimal strip stub; no send function ran. In these two plain-text outputs that stub preserves the relevant strings. The gateway's other filters and delivery behavior were not replayed.

## Results

| Case and expected decision | Candidate generated from evidence | Single-assistant output | Judge output and actual gate result |
|---|---|---|---|
| Laptop comparison: first page opened, two models unchecked; expected SKIP (routine narration) | “Quick update on the laptop comparison: I've pulled up the first manufacturer page and it loaded fine. I still need to check the other two models before I can give you a recommendation, so I'll keep going.” | `SKIP` | `NO` → **`NO` passes through**. Mecha's gate suppresses only empty output or text starting with `SKIP`, not `NO`. This would expose a nonsensical decision token if it reached that gate and subsequent delivery succeeded. |
| Friday appointment: 10 AM or 2 PM available, no choice selected or booking made; expected SEND asking user | “Quick update: I found Friday's openings on the booking page, 10:00 AM and 2:00 PM, and nothing is booked yet. Which time would you like, 10:00 AM or 2:00 PM?” | “Found Friday's openings on the booking page: 10:00 AM and 2:00 PM. Nothing's booked yet. Which one do you want?” | “Found Friday openings: 10:00 AM or 2:00 PM, both still free. Which one do you want?” → passed through. |

## Assessment

Against the predeclared expectations, single-assistant outcomes were 2/2 and judge-gate outcomes 1/2: the judge apparently intended to reject narration but violated its output protocol, and the gate treated `NO` as message text. This is one observed protocol failure, not an estimate of failure rate. On the blocker both communicated the needed choice; the single explicitly stated no booking had been made, while the judge omitted that reassurance and added “still free,” a present-tense availability assertion not freshly verified beyond the supplied page observation. No definite invented booking or time was observed. Neither output was evaluated by a blinded independent rater.

Only two hand-authored scenarios and one sample per condition; different prompts, different information access, shared model family, and sequential non-deterministic calls limit causal or general reliability claims. This experiment does not establish live behavior, delivery success, user preference, production model performance, or superiority overall.
