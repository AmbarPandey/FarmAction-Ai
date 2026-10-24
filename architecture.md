# FarmAction AI — Architecture

## Deployment shape

One Agentverse-hosted coordinator agent (`agent.py`). There are **no separately
deployed specialist agents** and no agent-to-agent protocol traffic. All
specialist capability runs as internal, tool-backed modules inside the
coordinator process. Documentation and tests report this as internal
orchestration — we do not describe internal functions as independent agents.

## Components

| Component | Kind | Responsibility |
|---|---|---|
| FarmAction AI coordinator | Agent (ACP endpoint) | Session memory, fact extraction, ambiguity handling, specialist invocation, reconciliation, final plan |
| Crop Intelligence | Internal module | Symptom parsing, observation/hypothesis separation, missing-info flags, prioritized steps, expert referral |
| Weather Intelligence | Internal module + real tool | Open-Meteo geocoding and 5-day forecast; explicit failure disclosure; never fabricates values |
| ASI:One LLM client | External service | Natural-language coordination (ASI:One API via OpenAI-compatible client) |
| ACP protocol | uAgents protocol (`chat_protocol_spec`) | ChatMessage/ChatAcknowledgement handling, manifest published on include() |

## Data flow

1. Farmer ACP `ChatMessage` → acknowledgement sent immediately.
2. Coordinator updates per-session memory (crop, symptoms, location, stage,
   leaf position, soil, irrigation — including negations like "did NOT
   irrigate"). Unknown values stay unknown; contradictions are acknowledged.
3. Crop Intelligence.assess(memory) → observations, hypotheses, missing info,
   next steps, what to observe, expert referral.
4. Weather Intelligence.run_weather: geocode location → (ambiguous? ask farmer)
   → forecast. `error` status is disclosed; nothing is assumed.
5. Coordinator reconciles both results into ONE plan; degraded specialists are
   announced and valid results from the other specialist are preserved.

## Failure handling

- Missing location → asks; no location claims.
- Ambiguous geocode → lists matches, asks farmer to choose.
- Weather API down / malformed payload → explicit disclosure; plan continues on
  crop evidence (patch P-1/P-3).
- Specialist exception → caught (patch P-2); response is qualified, never a
  false success claim.
- Unsupported requests (travel booking) and unsafe chemical-dosage requests →
  declined without tool calls.

## Constraints

- Session store is in-memory and resets on agent restart (deliberate: prevents
  cross-session inheritance). Long-term memory is out of scope for this stage.
- No external agricultural-reference database yet — recommendations are from
  built-in assessment logic, and no external citations are claimed.
