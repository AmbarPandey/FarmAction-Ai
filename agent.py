"""
FarmAction AI — Coordinator Agent (final, Task 10 release)
==========================================================
Agentverse-hosted coordinator with tool-backed internal specialist modules
(NOT separately deployed agents):

  - WeatherIntelligence: Open-Meteo geocoding + 5-day forecast (live, no key)
  - CropIntelligence: symptom analysis with structured reasoning
  - Per-session conversation memory (facts, unknowns, contradiction handling)
  - Structured, prioritized farmer action plans; graceful specialist failure

Task 8: official Agentverse Chat Protocol (ACP) with published manifest.
Task 9: reliability patches P-1 (explicit geocode error status), P-2 (guarded
        specialist calls with degraded-mode disclosure), P-3 (malformed
        forecast payload validation).

Primary scenario: "My tomato plants have yellowing leaves and are growing slowly."
"""

import os
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents.setup import fund_agent_if_low
from uagents_core.contrib.protocols.chat import (
    chat_protocol_spec,
    ChatMessage,
    ChatAcknowledgement,
    TextContent,
    StartSessionContent,
    EndSessionContent,
)

from openai import AsyncOpenAI

llm_client = AsyncOpenAI(
    base_url=os.environ.get("ASI1_BASE_URL", "https://api.asi1.ai/v1"),
    api_key=os.environ.get("ASI1_API_KEY", ""),
)

# Persistent identity: keep the SAME seed across redeploys so the address
# does not change.
agent = Agent(
    name="FarmAction AI",
    seed="farmaction-ai-seed-CHANGE-ME",
    port=8000,
    mailbox=True,
)
fund_agent_if_low(agent.wallet.address())

REQUEST_TIMEOUT = 15

WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
    55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snowfall", 73: "Moderate snowfall", 75: "Heavy snowfall",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


# ============================================================================
# SPECIALIST MODULE 1 — WEATHER INTELLIGENCE (real tool execution)
# ============================================================================

class WeatherIntelligence:
    """Tool-backed weather specialist. Real Open-Meteo calls; never fabricates."""

    SOURCE = "Open-Meteo (https://open-meteo.com/en/docs)"
    GEOCODE_SOURCE = "Open-Meteo Geocoding (https://open-meteo.com/en/docs/geocoding-api)"

    @staticmethod
    def _http_json(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as r:
            return json.loads(r.read().decode())

    @classmethod
    def geocode(cls, name: str):
        """Returns (status, places). status in {ok, no_result, error}.
        P-1: a network/API failure is reported explicitly as 'error', never
        silently as 'no_result'."""
        if not name or not name.strip():
            return ("no_result", [])
        try:
            url = ("https://geocoding-api.open-meteo.com/v1/search?name="
                   + urllib.parse.quote(name) + "&count=5&language=en&format=json")
            results = cls._http_json(url).get("results") or []
            places = [{
                "name": r.get("name", name),
                "admin1": r.get("admin1", ""),
                "country": r.get("country", ""),
                "latitude": r["latitude"],
                "longitude": r["longitude"],
            } for r in results]
            return ("ok", places) if places else ("no_result", [])
        except Exception:
            return ("error", [])          # P-1 applied

    @staticmethod
    def label(p: dict) -> str:
        return ", ".join(x for x in [p["name"], p.get("admin1", ""), p.get("country", "")] if x)

    @classmethod
    def forecast(cls, lat: float, lon: float) -> dict:
        """Normalized 5-day forecast dict; only fields actually received are
        returned. P-3: malformed payloads are reported, never normalized into
        fabricated values."""
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
            "precipitation_probability_max,wind_speed_10m_max,weather_code"
            "&timezone=auto&forecast_days=5"
        )
        try:
            payload = cls._http_json(url)
        except Exception as e:
            return {"location_ok": True,
                    "missing": [f"forecast unavailable ({type(e).__name__})"],
                    "fabricated": False}

        daily = payload.get("daily", {})
        # P-3 applied: validate structure before normalization
        if not isinstance(daily, dict) or not daily.get("time"):
            return {"location_ok": True,
                    "missing": ["malformed forecast payload"],
                    "fabricated": False}

        out_days = []
        times = daily.get("time", [])
        for i, day in enumerate(times):
            code = daily.get("weather_code", [None] * len(times))[i]
            out_days.append({
                "date": day,
                "t_max_c": daily.get("temperature_2m_max", [None] * len(times))[i],
                "t_min_c": daily.get("temperature_2m_min", [None] * len(times))[i],
                "precip_mm": daily.get("precipitation_sum", [None] * len(times))[i],
                "precip_prob_pct": daily.get("precipitation_probability_max", [None] * len(times))[i],
                "wind_max_kmh": daily.get("wind_speed_10m_max", [None] * len(times))[i],
                "condition": WEATHER_CODES.get(code, f"code {code}"),
            })
        return {"location_ok": True, "daily": out_days,
                "missing": [k for k, v in (("precip_prob_pct",
                            daily.get("precipitation_probability_max")),) if v is None],
                "fabricated": False}

    @classmethod
    def summarize(cls, fc: dict, place_label: str) -> str:
        """Farmer-facing weather brief; discloses unavailability explicitly."""
        if not fc or not fc.get("location_ok"):
            return ("Weather: live location could not be confirmed for "
                    f"{place_label or 'the given location'} — no weather claims made.")
        if fc.get("missing") and not fc.get("daily"):
            return ("Weather: live forecast could not be retrieved right now "
                    f"({'; '.join(fc['missing'])}). No forecast values are assumed — "
                    "plan from the crop evidence only.")
        lines = [f"Weather for {place_label} (source: {cls.SOURCE}, CC BY 4.0):"]
        for d in fc["daily"]:
            lines.append(
                f"  {d['date']}: {d['condition']}, {d['t_min_c']}–{d['t_max_c']} °C, "
                f"precip {d['precip_mm']} mm ({d['precip_prob_pct']}%), "
                f"wind up to {d['wind_max_kmh']} km/h")
        if fc.get("missing"):
            lines.append("Missing data: " + "; ".join(fc["missing"]))
        lines.append("Attribution: weather data by Open-Meteo (CC BY 4.0). "
                     "Do not present weather as a diagnosis.")
        return "\n".join(lines)


def extract_place(text: str):
    """Heuristic: 'X village/district' phrases, or a capitalized place after in/near/from/at."""
    m = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(village|district|taluk|tehsil|town|city)",
                  text, re.IGNORECASE)
    if m:
        return m.group(1) + " " + m.group(2).lower()
    m = re.search(r"\b(?:in|near|from|at)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)", text)
    return m.group(1) if m else None


# ============================================================================
# SPECIALIST MODULE 2 — CROP INTELLIGENCE (internal module)
# ============================================================================

class CropIntelligence:
    """Symptom-based crop assessment. Separates observations from hypotheses;
    never issues a definitive diagnosis from insufficient evidence; never
    invents chemical dosages."""

    CROP_KEYWORDS = {
        "tomato": "tomato", "tamatar": "tomato", "wheat": "wheat", "gehun": "wheat",
        "rice": "rice", "paddy": "rice", "chawal": "rice", "maize": "maize",
        "corn": "maize", "chilli": "chilli", "pepper": "chilli",
        "onion": "onion", "potato": "potato", "aloo": "potato",
        "okra": "okra", "bhindi": "okra",
        "cabbage": "cabbage", "cauliflower": "cauliflower",
        "brinjal": "brinjal", "eggplant": "brinjal",
        "mango": "mango", "banana": "banana", "grape": "grapes",
        "cucumber": "cucumber", "beans": "beans",
    }
    SYMPTOM_KEYWORDS = {
        "yellow": "yellowing leaves", "yellowing": "yellowing leaves",
        "pale": "pale leaves",
        "wilt": "wilting", "wilting": "wilting", "droop": "wilting",
        "spot": "leaf spots", "lesion": "leaf spots",
        "curl": "leaf curling", "burn": "leaf burn/tip burn",
        "stunt": "stunted growth", "slow": "slow growth",
        "rot": "rotting", "mold": "mold/mildew", "mildew": "mold/mildew",
    }
    STAGE_KEYWORDS = {
        "seedling": "seedling", "vegetative": "vegetative", "flower": "flowering",
        "fruit": "fruiting", "mature": "mature",
    }

    @classmethod
    def parse(cls, text: str, memory: dict) -> dict:
        low = text.lower()
        negated = re.findall(r"(?:not|didn'?t|no)\s+(?:\w+\s+){0,2}(irrigat\w*|water\w*)", low)
        if "irrigat" in low or "water" in low:
            if any("irrigat" in n or "water" in n for n in negated):
                memory["irrigation"] = "not irrigated recently"
            elif "yesterday" in low:
                memory["irrigation"] = "irrigated yesterday"
        if "soil" in low and any(w in low for w in ["wet", "dry", "moist", "damp"]):
            for w, v in [("wet", "wet"), ("damp", "wet"), ("moist", "moist"), ("dry", "dry")]:
                if w in low:
                    memory["soil_condition"] = v
                    break
        for k, v in cls.CROP_KEYWORDS.items():
            if k in low and ("my " + k in low or k in low):
                memory["crop"] = v
                break
        if "older" in low or "lower" in low:
            memory["leaf_position"] = "older/lower leaves"
        elif "younger" in low or "upper" in low or "new" in low:
            memory["leaf_position"] = "younger/upper leaves"
        for k, v in cls.STAGE_KEYWORDS.items():
            if k in low:
                memory["growth_stage"] = v
        for k, v in cls.SYMPTOM_KEYWORDS.items():
            if k in low and v not in memory.get("symptoms", []):
                memory.setdefault("symptoms", []).append(v)
        return memory

    @classmethod
    def assess(cls, memory: dict) -> dict:
        """Structured assessment: observations vs hypotheses; missing info;
        prioritized next steps; what to observe; expert referral."""
        crop = memory.get("crop") or "unknown crop"
        symptoms = memory.get("symptoms", [])
        stage = memory.get("growth_stage") or "unknown"
        leaf = memory.get("leaf_position") or "unknown"
        soil = memory.get("soil_condition") or "unknown"
        irrigation = memory.get("irrigation") or "unknown"

        observations = [
            f"Crop: {crop} (stage: {stage})",
            f"Reported symptoms: {', '.join(symptoms) if symptoms else 'none recorded'}",
            f"Affected leaves: {leaf}",
            f"Soil condition: {soil}; irrigation: {irrigation}",
        ]

        hypotheses, missing = [], []
        blob = " ".join(symptoms).lower()
        if "yellowing" in blob:
            if "older/lower" in leaf:
                if soil == "wet" or "irrigated" in irrigation:
                    hypotheses.append("OVERWATERING / poor drainage → root stress "
                                      "(consistent: yellowing older leaves + wet soil + "
                                      "recent irrigation). HYPOTHESIS, not confirmed.")
                else:
                    hypotheses.append("Nitrogen deficiency (older/lower leaves yellowing "
                                      "first). HYPOTHESIS — needs confirmation.")
            elif "younger/upper" in leaf:
                hypotheses.append("Iron/sulfur deficiency or vascular issue "
                                  "(younger leaves affected first). HYPOTHESIS.")
            else:
                hypotheses.append("Nutrient deficiency or early fungal issue — leaf "
                                  "position not yet known, cannot narrow further.")
            missing.append("Pattern of yellowing (uniform, interveinal, edges) and any "
                           "spotting — changes which cause is likely")
        if "slow growth" in blob or "stunted" in blob:
            hypotheses.append("Root stress (waterlogging, compaction) or nutrient "
                              "shortfall. HYPOTHESIS.")
            missing.append("Recent fertilizer application and dosage")
        if "wilting" in blob:
            if soil == "wet":
                hypotheses.append("Root rot / waterlogging (wilting despite wet soil "
                                  "points to roots, not drought). HYPOTHESIS.")
            else:
                hypotheses.append("Drought stress or early vascular wilt. HYPOTHESIS.")
            missing.append("Whether wilting recovers overnight")
        if not symptoms:
            missing.append("Which symptoms exactly (color, pattern, parts affected)")

        next_steps = [
            "1. Confirm watering: check soil 5 cm deep — irrigate only when it feels dry.",
            "2. If soil is waterlogged: stop irrigation, open drainage channels, let the "
            "root zone breathe before any nutrient application.",
            "3. Inspect 3–4 affected plants closely: roots (brown/soft?), stem base, "
            "leaf undersides (pests?), and note the yellowing pattern.",
            "4. Do NOT apply fertilizer or pesticide until cause is narrowed — wrong "
            "treatment on waterlogged roots worsens damage.",
        ]
        observe = [
            "New yellow leaves appearing per day (rate of spread)",
            "Whether wilting recovers by morning",
            "Any new spots, curling, or pest presence on leaf undersides",
        ]
        expert = ("If symptoms spread rapidly, plants collapse, or the pattern is "
                  "unclear after these checks, have a local agriculture extension "
                  "officer / agronomist inspect the field — this is decision support, "
                  "not a confirmed diagnosis.")

        return {
            "observations": observations,
            "hypotheses": hypotheses,
            "missing_information": missing,
            "next_steps": next_steps,
            "what_to_observe": observe,
            "expert_referral": expert,
            "diagnosis_status": "HYPOTHESES ONLY — no confirmed diagnosis from this evidence",
        }

    @classmethod
    def brief(cls, a: dict) -> str:
        lines = ["CROP ASSESSMENT (decision support — not a confirmed diagnosis):"]
        lines += ["Observed: " + o for o in a["observations"]]
        lines.append("Possible causes (hypotheses):")
        lines += ["  - " + h for h in a["hypotheses"]] or ["  - Not enough evidence yet."]
        if a["missing_information"]:
            lines.append("Information that could change this assessment: "
                         + "; ".join(a["missing_information"]))
        lines.append("Do now:")
        lines += ["  " + s for s in a["next_steps"]]
        lines.append("Watch for:")
        lines += ["  - " + o for o in a["what_to_observe"]]
        lines.append(a["expert_referral"])
        return "\n".join(lines)


# ============================================================================
# SESSION MEMORY (per-session; no cross-session inheritance)
# ============================================================================

def new_session(session_id: str) -> dict:
    return {"id": session_id, "memory": {}, "history": []}


# ============================================================================
# COORDINATOR PIPELINE (internal orchestration — one agent, tool-backed modules)
# ============================================================================

def run_weather(session: dict) -> dict:
    """Geocode → forecast. Ambiguous matches are surfaced, never silently picked.
    P-1-aware: 'error' status is disclosed, not treated as 'place unknown'."""
    mem = session["memory"]
    place = mem.get("location")
    if not place:
        return {"status": "no_location"}
    status, places = WeatherIntelligence.geocode(place)
    if status == "error":
        return {"status": "error",
                "note": "live weather service unreachable — disclosed to farmer"}
    if status == "no_result" or not places:
        return {"status": "unresolved", "place": place}
    if len(places) > 1:
        return {"status": "ambiguous",
                "options": [WeatherIntelligence.label(p) for p in places]}
    fc = WeatherIntelligence.forecast(places[0]["latitude"], places[0]["longitude"])
    return {"status": "ok", "place_label": WeatherIntelligence.label(places[0]),
            "forecast": fc}


def handle_message(session: dict, text: str) -> str:
    """Full coordinator turn: memory update → specialists (guarded, P-2) →
    reconciliation → one coherent action plan."""
    mem = session["memory"]
    session["history"].append({"farmer": text})
    CropIntelligence.parse(text, mem)

    if "location" not in mem:
        p = extract_place(text)
        if p:
            mem["location"] = p

    low = text.lower()

    # Out-of-scope requests: decline, no tool calls
    if any(k in low for k in ["book me a flight", "flight ticket", "book a hotel"]):
        reply = ("I'm FarmAction AI, an agricultural assistant — I can't book travel. "
                 "I can help with crop health, weather-aware planning, and field "
                 "decisions. What's happening on your farm?")
        session["history"][-1]["agent"] = reply
        return reply

    # Chemical dosage safety gate: never invent dosages
    if any(k in low for k in ["pesticide", "spray", "chemical", "dose", "dosage"]) \
            and any(k in low for k in ["how much", "per litre", "per liter", "which chemical",
                                       "dose", "dosage", "quantity"]):
        if not (mem.get("crop") and mem.get("symptoms") and mem.get("pest_identified")):
            reply = ("I can't recommend a pesticide, dose, or chemical without enough "
                     "information — a wrong chemical or dose can harm the crop, you, "
                     "and beneficial insects. Please tell me: which crop, what pest or "
                     "disease you actually see (a photo-level description helps), the "
                     "growth stage, and whether the problem is confirmed. If a pest is "
                     "confirmed, follow the product label exactly or consult your local "
                     "agriculture extension officer before spraying.")
            session["history"][-1]["agent"] = reply
            return reply

    # Greeting / unsupported short messages
    if len(low.strip()) < 8 and "?" not in text and not any(
            k in low for k in ["hi", "hello", "namaste", "hey"]):
        reply = ("I'm FarmAction AI. Tell me what's happening with your crop — "
                 "symptoms, crop, and village — and I'll help you work out the next step.")
        session["history"][-1]["agent"] = reply
        return reply

    # --- Specialist invocations (P-2: guarded; valid results preserved) ---
    degraded = []
    assessment = None
    try:
        assessment = CropIntelligence.assess(mem)
    except Exception as e:
        degraded.append(f"crop analysis unavailable ({type(e).__name__})")

    weather = None
    crop_health_request = bool(mem.get("symptoms")) or bool(mem.get("crop"))
    if crop_health_request:
        try:
            weather = run_weather(session)
        except Exception as e:
            weather = None
            degraded.append(f"weather unavailable ({type(e).__name__})")

    # --- Reconciliation: one final plan ---
    parts = []
    if degraded:
        parts.append("Note: " + "; ".join(degraded) +
                     " — the plan below uses the evidence that is still valid.")

    asked = []
    if weather and weather.get("status") == "ambiguous":
        asked.append("Which " + mem.get("location", "") + " do you mean? I found: "
                     + " / ".join(weather["options"]) +
                     " — please confirm so I don't give you the wrong area's forecast.")
    elif weather and weather.get("status") == "unresolved":
        asked.append(f"I couldn't find '{weather['place']}' in the gazetteer — "
                     "which nearby town or district is your village in?")
    elif weather and weather.get("status") == "error":
        parts.append("Live weather data could not be retrieved right now, so I won't "
                     "assume any forecast values.")

    if not mem.get("crop") and crop_health_request:
        asked.append("Which crop is affected?")
    if not asked and not mem.get("symptoms"):
        asked.append("What exactly are you seeing on the plants (color, spots, wilting, "
                     "which leaves)?")

    if assessment:
        parts.append(CropIntelligence.brief(assessment))
    if weather and weather.get("status") == "ok":
        parts.append(WeatherIntelligence.summarize(weather["forecast"],
                                                   weather["place_label"]))
    if asked:
        parts.append("To narrow this down: " + " ".join(asked))

    reply = "\n\n".join(parts)
    session["history"][-1]["agent"] = reply
    return reply


# ============================================================================
# ACP — AGENT CHAT PROTOCOL (official chat_protocol_spec, manifest published)
# ============================================================================

chat_protocol = Protocol(spec=chat_protocol_spec)


@chat_protocol.on_message(model=ChatMessage, replies={ChatAcknowledgement})
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))

    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return

    starts = any(isinstance(c, StartSessionContent) for c in msg.content)
    ends = any(isinstance(c, EndSessionContent) for c in msg.content)
    if starts or msg.msg_id not in _SESSIONS:
        _SESSIONS[msg.msg_id] = new_session(msg.msg_id)
    session = _SESSIONS[msg.msg_id]

    try:
        reply_text = handle_message(session, text)
    except Exception as e:
        ctx.logger.error(f"coordinator error: {e}")
        reply_text = ("Something went wrong while processing that — I did not "
                      "complete the analysis. Please try rephrasing your message.")

    reply = ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=reply_text)],
    )
    if ends:
        reply.content.append(EndSessionContent(type="end-session"))
        _SESSIONS.pop(msg.msg_id, None)
    await ctx.send(sender, reply)


@chat_protocol.on_message(model=ChatAcknowledgement, replies=set())
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"ACK from {sender}: {msg.acknowledged_msg_id}")


agent.include(chat_protocol, publish_manifest=True)

# In-memory session store (per-conversation; reset on restart — deliberate:
# sessions must not inherit each other's data)
_SESSIONS: dict = {}


@agent.on_event("startup")
async def report_identity(ctx: Context):
    ctx.logger.info("=" * 60)
    ctx.logger.info(f"FarmAction AI agent address: {agent.address}")
    ctx.logger.info("Copy this address into README §5 and the Agentverse profile.")
    ctx.logger.info("=" * 60)


if __name__ == "__main__":
    agent.run()
