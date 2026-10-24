![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)

![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# FarmAction AI

A Fetch.ai agent that helps farmers investigate crop-health problems. The farmer
describes symptoms in plain language; FarmAction AI gathers the missing details
(crop, growth stage, leaf position, irrigation), retrieves live weather data for
the farm's location when available, and returns one coherent, prioritized action
plan built from coordinated Crop Intelligence and Weather Intelligence
capabilities.

**Target users:** smallholder farmers who need a next-step decision — not a
lecture — when their crop looks unhealthy.

**Practical outcome:** a short, evidence-based action plan (what to check, what
to do, what to watch for, and when to call a local expert). This is decision
support; it is not a guaranteed diagnosis and it does not claim improved yields
or commercial deployment.

## Problem statement

A farmer sees yellowing leaves. Is it nutrient deficiency, overwatering, a
fungus, or normal senescence? The answer depends on crop, growth stage, which
leaves are affected, soil moisture, recent irrigation, and the coming weather —
facts the farmer usually has to volunteer one at a time. A plain chatbot that
answers once and forgets forces the farmer to repeat everything and cannot fold
in weather risk. FarmAction AI combines agricultural reasoning with live data
tools and conversation memory so the diagnosis narrows as evidence accumulates.

## Current capabilities

| Capability | Status |
|---|---|
| Farmer-facing conversational assistance | **Verified** (code + test suite) |
| Context retention across multiple messages (per-session) | **Verified** (test suite M1–M4) |
| Crop-health assessment with uncertainty communication | **Verified** (test C1) |
| Live weather retrieval (Open-Meteo geocoding + 5-day forecast) | **Verified** (Task 6 live call; test W1) |
| Structured action-plan generation | **Verified** (code) |
| Safety refusal of unsupported chemical-dosage advice | **Verified** (test C2) |
| Failure handling: missing/ambiguous location, API outage, malformed tool output, specialist failure | **Verified** (tests F1–F10, O2 — after Task 9 patches) |
| ACP (Agent Chat Protocol) message handling + manifest publication | **Verified in code**; live platform exchange **not yet verified** |
| Agentverse registration & hosting | **Owner action pending** — not yet verified |
| Direct ASI:One conversation | **Not yet verified** (requires activated Agentverse agent) |
| Separately deployed specialist agents communicating agent-to-agent | **Not implemented by design** — specialists are internal modules (see Architecture) |
| Agricultural-reference retrieval (external databases) | **In progress** — not yet implemented; no external citations are claimed |

## Architecture

FarmAction AI is **one coordinator agent** (`agent.py`) with two **internal,
tool-backed specialist modules** — not separately deployed agents, and there is
no agent-to-agent protocol between them. We state this explicitly:

- **FarmAction AI (coordinator)** — receives ACP `ChatMessage`s, maintains
  per-session memory, decides what is known vs. missing, invokes specialists,
  reconciles their results, and emits one final action plan (plus ACP
  `ChatAcknowledgement` and EndSession lifecycle).
- **Crop Intelligence (internal module)** — symptom/crop/stage/leaf-position
  analysis; separates observations from hypotheses; flags missing information;
  prioritizes next steps; recommends expert inspection when warranted.
- **Weather Intelligence (internal module)** — real Open-Meteo calls:
  geocoding of the farm location, then a 5-day daily forecast; discloses
  unavailability instead of fabricating values.

**Data flow:** farmer message → coordinator session memory (facts updated;
contradictions acknowledged) → Crop Intelligence assessment → Weather
Intelligence (geocode → forecast) when a location is known → coordinator
reconciliation → single final plan. If one specialist fails, the coordinator
keeps the other's valid results and labels the gap.

## Fetch.ai integration

| Item | Value |
|---|---|
| Agent name | `FarmAction AI` |
| Agentverse address | **To be filled by owner** (printed at agent startup; pending registration) |
| Profile URL | **To be filled by owner** (pending Agentverse registration) |
| Hosting | Agentverse Hosted Agent (single hosted agent; no duplicates) |
| ACP | Official `uagents_core.contrib.protocols.chat` Chat Protocol; manifest published via `agent.include(chat_protocol, publish_manifest=True)` — code verified; live exchange not yet verified |
| ASI:One | Discovery/chat test pending activation (T1–T5 procedure in `docs/testing.md`) |

## Tools and data sources

- **Open-Meteo Forecast API** — 5-day daily forecast (temperature, precipitation
  and probability, wind, weather codes) used to qualify crop-action timing.
  Docs: https://open-meteo.com/en/docs
- **Open-Meteo Geocoding API** — resolves the farm place name to coordinates;
  ambiguous matches are surfaced to the farmer for clarification.
  Docs: https://open-meteo.com/en/docs/geocoding-api
- **Attribution:** weather data by Open-Meteo, licensed CC BY 4.0. No API key
  required for Open-Meteo (non-commercial use per their terms).
- **Agricultural references:** no external agricultural database is currently
  retrieved; recommendations come from the agent's built-in assessment logic.
  We do not cite external verification we do not have.
- **LLM:** ASI:One API (`ASI1_BASE_URL`, key via `ASI1_API_KEY`) for natural-
  language coordination.

## Setup and configuration

**Requirements:** Python 3.10+, pip.

```bash
pip install -r requirements.txt
cp .env.example .env   # then edit with your real ASI:One API key (never commit .env)
```

**Environment variables:**

| Variable | Purpose |
|---|---|
| `ASI1_API_KEY` | Your ASI:One API key (from asi1.ai) — keep secret |
| `ASI1_BASE_URL` | Optional override; defaults to `https://api.asi1.ai/v1` |

**Run locally:**

```bash
python agent.py
# the startup log prints the agent's real address — record it for README §5
```

**Deploy:** paste `agent.py` into your **existing** Agentverse Hosted Agent
(same seed → same address), then activate it in the dashboard. Hosted agents
read the injected ASI1 environment variables; do not hardcode keys.

## Testing instructions and results

Full procedure in `docs/testing.md`. Quick start:

```bash
python test_farmaction.py            # live Open-Meteo tests included
python test_farmaction.py --offline  # skip network-dependent tests
# evidence is written to task9_evidence.json
```

Result summary (Stage 9):

- **Live tests (real API calls):** Pune 5-day forecast (geocode → forecast,
  actual HTTP calls counted), Puhana geocoding. Weather data credited to
  Open-Meteo.
- **Mocked tests (clearly labelled, not proof of live execution):** API-outage
  disclosure, ambiguous-location clarification, malformed tool output,
  specialist-timeout degradation.
- **Memory tests:** 4-turn tomato dialogue retention, unknowns stay unknown,
  session isolation, contradiction update.
- **Safety:** pesticide-dosage refusal with insufficient evidence.
- **ACP/ASI:One:** code-level verified only; live platform tests (T1–T5) are a
  documented manual procedure pending Agentverse activation.

Known defects found in Stage 9 were fixed by minimal patches P-1/P-2/P-3
(explicit geocode error status; guarded specialist calls with graceful
degradation; malformed-forecast-payload validation). Regression reruns are part
of `docs/testing.md`.

## Safety and limitations

- Crop-health output is **decision-support guidance, not a guaranteed
  diagnosis**. Symptom-based assessments carry real uncertainty; identical
  symptoms can have multiple causes.
- Weather data is a 5-day public forecast and can be wrong; the agent discloses
  when live data is unavailable rather than guessing.
- For high-impact or uncertain cases the agent recommends inspection by a local
  agricultural expert or extension officer.
- The agent does **not** invent chemical dosages or treatment instructions; it
  asks for missing evidence and defers to product labels and experts.
- Known limitations: no external agricultural-reference retrieval yet;
  Agentverse registration/ASI:One discovery not yet verified; specialists are
  internal modules, not independent deployed agents.

## Example workflow (verified behavior)

```
Farmer:  My tomato plants have yellowing leaves and are growing slowly. What should I do?
Agent:   Identifies a crop-health request; asks concise follow-ups
         (village/location, growth stage, which leaves are affected).

Farmer:  Village Puhana, growth stage flowering, older/lower leaves.
Agent:   Records crop=tomato, symptoms, location, stage, leaf position; does NOT
         re-ask any of these; geocodes Puhana and asks for clarification if
         multiple places match.

Farmer:  The soil seems wet, and I irrigated yesterday.
Agent:   Adds soil/irrigation facts without losing earlier answers; flags
         overwatering/waterlogging as a leading hypothesis for yellowing
         lower leaves (hypothesis, not confirmed diagnosis).

Farmer:  What should I do now?
Agent:   One action plan: hold irrigation and check drainage, inspect roots,
         watch the 5-day forecast for rain before any treatment, list what to
         observe next, and when to involve a local expert.
```

Transcript retention, non-repetition, and session isolation are asserted by the
test suite (tests M1–M4).

## Remaining work

- Complete Agentverse registration/activation and record the real agent address
  + profile URL above.
- Run and record the live ACP/ASI:One verification (T1–T5, `docs/testing.md`).
- Agricultural-reference retrieval (in progress, not yet implemented).
- Stage 11: demo preparation and official submission.

Fetch-A-Thon hackpack: https://www.fetch.ai/events/hackathons/fetch-a-thon-3/hackpack

**FarmAction AI is not fully production-ready.** It is a working decision-support
agent with verified core capabilities and documented, honest limitations.
