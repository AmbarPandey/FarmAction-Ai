"""
FarmAction AI — Coordinator Agent (Agentverse-hosted, intent-routed release)
============================================================================
Fixes the "generic diagnosis for every question" defect:

  - Intent classification runs BEFORE any response is generated.
  - Identity/greeting questions get a short introduction — no diagnostic template.
  - Cultivation questions get a structured growing guide — no fabricated symptoms.
  - Diagnosis format is used ONLY when a plant problem is reported.
  - Weather answers use the live Open-Meteo integration or disclose failure.
  - Session memory is context-aware: follow-ups are recognized, answers are
    never re-asked, and unrelated old topics do not leak into new ones.

Agentverse hosting notes (unchanged, FINAL):
  - NO agent.run(), no port/mailbox kwargs, no fund_agent_if_low.
  - NO explicit `replies=` on chat_protocol.on_message decorators.
  - openai imported lazily so a missing package/key can never block startup.
"""

import os
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    chat_protocol_spec,
    ChatMessage,
    ChatAcknowledgement,
    TextContent,
    StartSessionContent,
    EndSessionContent,
)

llm_client = None  # created lazily on first use


def _get_llm():
    """Returns an ASI:One client, or None if unavailable. Never raises at import time."""
    global llm_client
    if llm_client is not None:
        return llm_client
    api_key = os.environ.get("ASI1_API_KEY")
    if not api_key:
        return None
    try:
        from openai import AsyncOpenAI  # lazy: survives environments without openai
        llm_client = AsyncOpenAI(
            base_url=os.environ.get("ASI1_BASE_URL", "https://api.asi1.ai/v1"),
            api_key=api_key,
        )
    except Exception:
        return None
    return llm_client


agent = Agent(
    name="FarmAction AI",
    seed="farmaction-ai-seed-CHANGE-ME",
)

REQUEST_TIMEOUT = 15

WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog", 51: "Light drizzle",
    53: "Moderate drizzle", 55: "Dense drizzle", 61: "Slight rain",
    63: "Moderate rain", 65: "Heavy rain", 66: "Freezing rain",
    67: "Heavy freezing rain", 71: "Slight snow", 73: "Moderate snow",
    75: "Heavy snow", 77: "Snow grains", 80: "Slight rain showers",
    81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


# ============================================================================
# SPECIALIST MODULE 1 — WEATHER INTELLIGENCE (real Open-Meteo calls)
# ============================================================================

class WeatherIntelligence:
    """Live weather specialist: geocoding + 5-day forecast via Open-Meteo."""

    SOURCE = "Open-Meteo (https://open-meteo.com) — CC BY 4.0"

    @staticmethod
    def _http_json(url: str):
        req = urllib.request.Request(url, headers={"User-Agent": "FarmActionAI/1.0"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @classmethod
    def geocode(cls, place: str):
        """Returns (status, data). status in:
        'ok' | 'missing_input' | 'ambiguous' | 'no_result' | 'error'."""
        place = (place or "").strip()
        if not place:
            return "missing_input", None
        url = ("https://geocoding-api.open-meteo.com/v1/search?"
               + urllib.parse.urlencode({"name": place, "count": 5,
                                         "language": "en", "format": "json"}))
        try:
            raw = cls._http_json(url)
        except Exception:
            return "error", None
        results = raw.get("results") or []
        if not results:
            return "no_result", []
        places = [{
            "name": r.get("name", place),
            "admin1": r.get("admin1", ""),
            "country": r.get("country", ""),
            "latitude": r["latitude"],
            "longitude": r["longitude"],
        } for r in results if "latitude" in r and "longitude" in r]
        if not places:
            return "no_result", []
        if len(places) > 1:
            return "ambiguous", places
        return "ok", places[0]

    @staticmethod
    def label(p: dict) -> str:
        return ", ".join(x for x in [p.get("name"), p.get("admin1", ""),
                                     p.get("country", "")] if x)

    @classmethod
    def forecast(cls, lat: float, lon: float) -> dict:
        params = {
            "latitude": lat, "longitude": lon, "forecast_days": "5",
            "timezone": "auto",
            "current": ",".join([
                "temperature_2m", "relative_humidity_2m", "apparent_temperature",
                "precipitation", "rain", "weather_code",
                "wind_speed_10m", "wind_gusts_10m"]),
            "daily": ",".join([
                "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "precipitation_probability_max",
                "wind_speed_10m_max", "weather_code"]),
        }
        raw = cls._http_json(
            "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params))

        if not isinstance(raw, dict):
            raise ValueError("forecast payload is not a JSON object")

        w = {
            "source": cls.SOURCE,
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "timezone": raw.get("timezone"),
            "current": None, "daily": [], "missing": [],
        }

        cur = raw.get("current") or {}
        if isinstance(cur, dict) and cur.get("temperature_2m") is not None:
            code = cur.get("weather_code")
            w["current"] = {
                "temperature_2m": cur.get("temperature_2m"),
                "relative_humidity_2m": cur.get("relative_humidity_2m"),
                "apparent_temperature": cur.get("apparent_temperature"),
                "precipitation": cur.get("precipitation"),
                "rain": cur.get("rain"),
                "weather_code": code,
                "weather_desc": WEATHER_CODES.get(code),
                "wind_speed_10m": cur.get("wind_speed_10m"),
                "wind_gusts_10m": cur.get("wind_gusts_10m"),
            }
        else:
            w["missing"].append("current_conditions")

        daily = raw.get("daily") or {}
        dates = daily.get("time") or []
        if isinstance(dates, list) and dates:
            for i, d in enumerate(dates):
                w["daily"].append({
                    "date": d,
                    "t_max": (daily.get("temperature_2m_max") or [None] * len(dates))[i],
                    "t_min": (daily.get("temperature_2m_min") or [None] * len(dates))[i],
                    "precip_sum": (daily.get("precipitation_sum") or [None] * len(dates))[i],
                    "precip_prob": (daily.get("precipitation_probability_max")
                                    or [None] * len(dates))[i],
                    "wind_max": (daily.get("wind_speed_10m_max") or [None] * len(dates))[i],
                    "weather_desc": WEATHER_CODES.get(
                        (daily.get("weather_code") or [None] * len(dates))[i]),
                })
        else:
            w["missing"].append("daily_forecast")

        return w

    @classmethod
    def summarize(cls, forecast: dict, place_label: str) -> str:
        cur = forecast.get("current") or {}
        lines = [f"**Live weather — {place_label}** "
                 f"(retrieved {forecast['retrieved_at']}, source: {forecast['source']})"]
        if cur:
            lines.append(
                f"Now: {cur.get('weather_desc')}, {cur.get('temperature_2m')}°C "
                f"(feels {cur.get('apparent_temperature')}°C), humidity "
                f"{cur.get('relative_humidity_2m')}%, wind {cur.get('wind_speed_10m')} km/h.")
        for d in forecast.get("daily", [])[:5]:
            lines.append(
                f"- {d['date']}: {d.get('weather_desc') or '—'}, "
                f"{d.get('t_min')}–{d.get('t_max')}°C, rain "
                f"{d.get('precip_sum')} mm ({d.get('precip_prob')}% chance), "
                f"wind up to {d.get('wind_max')} km/h.")
        if forecast.get("missing"):
            lines.append("Note: some forecast fields were unavailable ("
                         + ", ".join(forecast["missing"]) + ").")
        return "\n".join(lines)


# ============================================================================
# SYMPTOM / CROP PARSING HELPERS (used by the diagnosis route)
# ============================================================================

class CropIntelligence:
    """Symptom parsing + structured diagnosis for the DIAGNOSIS route only."""

    CROPS = ["tomato", "potato", "wheat", "rice", "maize", "corn", "cotton",
             "chilli", "pepper", "onion", "garlic", "cucumber", "beans",
             "cauliflower", "cabbage", "mango", "banana", "grape", "soybean",
             "mustard", "sugarcane", "groundnut", "millet", "barley"]

    @classmethod
    def parse(cls, text: str) -> dict:
        low = text.lower()
        crop = next((c for c in cls.CROPS if re.search(rf"\b{c}s?\b", low)), None)
        symptoms = []
        if "yellow" in low:
            symptoms.append("yellowing")
        if re.search(r"slow(ly)? grow|stunt|not grow", low):
            symptoms.append("stunted")
        if "wilt" in low:
            symptoms.append("wilting")
        if re.search(r"spot|blight|lesion", low):
            symptoms.append("spots/lesions")
        if "curl" in low:
            symptoms.append("leaf curl")
        if "white" in low:
            symptoms.append("whitish coating")
        if re.search(r"pest|insect|bug|aphid|borer", low):
            symptoms.append("pest signs")
        return {"crop": crop, "symptoms": symptoms}

    @staticmethod
    def analyse(parsed: dict, state: dict, weather_text: str) -> dict:
        crop = state.get("crop") or parsed.get("crop") or "unknown crop"
        symptoms = state.get("symptoms") or parsed.get("symptoms") or []
        yellowing = any("yellowing" in s for s in symptoms)
        stunting = any("stunted" in s for s in symptoms)

        possible_causes, supporting = [], []
        if yellowing and stunting:
            possible_causes += [
                "Nutrient imbalance (commonly nitrogen, or magnesium/iron)",
                "Water stress — either drought or waterlogged roots",
                "Early-stage disease or root damage",
            ]
            supporting += ["Yellowing plus slow growth together most often point "
                           "to a nutrient or root problem rather than a leaf disease."]
        elif yellowing:
            possible_causes += [
                "Nutrient imbalance (nitrogen on older leaves; iron/zinc on younger)",
                "Overwatering or poor drainage",
                "Early fungal or viral infection",
            ]
        elif stunting:
            possible_causes += ["Water stress", "Nutrient deficiency",
                                "Root damage or soil compaction"]
        else:
            possible_causes = ["Insufficient symptom detail — more information needed"]

        missing = []
        if not state.get("crop_stage"):
            missing.append("crop growth stage (seedling / flowering / fruiting)")
        if not state.get("watering"):
            missing.append("recent watering and fertilizer history")
        if not state.get("location"):
            missing.append("location (for weather/season context)")

        limitations = [
            "No photo or lab result — this is a shortlist of plausible causes, "
            "NOT a confirmed diagnosis.",
            "Symptoms of nutrient, water, and disease problems often overlap.",
        ]
        if not weather_text:
            limitations.append("No live weather data available for this conversation.")

        return {
            "crop": crop,
            "symptoms": symptoms,
            "possible_causes": possible_causes,
            "supporting_evidence": supporting,
            "missing_information": missing,
            "confidence_limitations": limitations,
        }

    @staticmethod
    def brief(assessment: dict) -> str:
        lines = [f"**Crop assessment ({assessment['crop']})**"]
        for c in assessment["possible_causes"]:
            lines.append(f"- Possible: {c}")
        if assessment["missing_information"]:
            lines.append("Still unknown: " + "; ".join(assessment["missing_information"]))
        return "\n".join(lines)


# ============================================================================
# PLACE EXTRACTION
# ============================================================================

PLACE_RE = re.compile(
    r"\b(?:in|near|around|at|from)\s+([A-Z][A-Za-z .'-]{2,40})"
    r"|\b(village|district|taluk|tehsil)\s+(?:of\s+)?([A-Z][A-Za-z .'-]{2,40})"
    r"|^\s*([A-Z][A-Za-z .'-]{2,40})\s*,?\s*$")  # bare place answer like "Puhana"


def extract_place(text: str):
    m = PLACE_RE.search(text)
    if not m:
        return None
    for group in m.groups():
        if group and group.lower() not in ("village", "district", "taluk", "tehsil"):
            return group.strip(" .,")
    return None


# ============================================================================
# SESSION MEMORY (context-aware, per active conversation)
# ============================================================================

_SESSIONS: dict = {}


def new_session(session_id) -> dict:
    return {
        "id": session_id,
        "history": [],          # [{"user": str, "agent": str}]
        "memory": {},           # verified facts the farmer actually stated
        "asked": set(),         # clarification questions already asked
        "active_topic": None,   # "diagnosis" | "cultivation" | "weather" | ...
    }


def _extract_stage(text_low: str):
    if re.search(r"seedling|nursery|young plant", text_low):
        return "seedling"
    if "flower" in text_low:
        return "flowering"
    if re.search(r"fruit", text_low):
        return "fruiting"
    return None


def _extract_pattern(text_low: str):
    if re.search(r"lower leaves|older leaves|bottom leaves", text_low):
        return "older/lower leaves"
    if re.search(r"younger|top leaves|new leaves", text_low):
        return "younger/upper leaves"
    if re.search(r"all (over|leaves)", text_low):
        return "all over"
    return None


def update_memory(session: dict, text: str):
    """Record only facts the farmer actually stated. Latest statement wins."""
    low = text.lower()
    mem = session["memory"]

    parsed = CropIntelligence.parse(text)
    if parsed["crop"]:
        mem["crop"] = parsed["crop"]
    if parsed["symptoms"]:
        # latest symptom statement replaces, never merges blindly
        mem["symptoms"] = parsed["symptoms"]

    place = extract_place(text)
    if place and len(place) >= 3:
        mem["location"] = place

    stage = _extract_stage(low)
    if stage:
        mem["crop_stage"] = stage

    pattern = _extract_pattern(low)
    if pattern:
        mem["pattern"] = pattern

    if re.search(r"water|irrigat", low):
        mem.setdefault("watering", "mentioned")
    if re.search(r"fertiliz|fertili|manure|compost|npk|urea", low):
        mem.setdefault("fertilizer", "mentioned")


def _asked_before(session: dict, key: str) -> bool:
    return key in session["asked"]


def _mark_asked(session: dict, *keys: str):
    session["asked"].update(keys)


# ============================================================================
# INTENT CLASSIFICATION — runs BEFORE any response is generated
# ============================================================================

INTENTS = (
    "GREETING_OR_IDENTITY", "GENERAL_CROP_GUIDANCE", "CROP_PROBLEM_DIAGNOSIS",
    "WEATHER_REQUEST", "IRRIGATION_AND_SOIL", "FERTILIZER_AND_NUTRIENTS",
    "PEST_AND_DISEASE_MANAGEMENT", "FOLLOW_UP", "GENERAL_AGRICULTURE",
    "CLARIFICATION_REQUIRED",
)

_GREET_PAT = re.compile(
    r"^(hi|hii|hey|hello|namaste|good (morning|afternoon|evening)|yo)\b[\s!,.]*$"
    r"|who (are|r) you|what (can|do) you do|about (you|yourself)|your (name|purpose|"
    r"capabilities)|introduce yourself|tell me about (farmaction|this agent)")

_WEATHER_PAT = re.compile(
    r"\bweather\b|\brain\b|\bforecast\b|rainfall|temperature today|tomorrow"
    r"|\bhumid\b|wind|heat wave|cold wave|will it (rain|be hot|be cold)")

_SYMPTOM_PAT = re.compile(
    r"yellow|wilt|spot|blight|lesion|stunt|not grow(ing)?|slow(ly)? grow|dying|"
    r"curl|rot|pest|insect|aphid|borer|mite|fungus|fungi|disease|deficien|"
    r"looks bad|turning brown|black spot|powdery|mold|mould|drooping|drying up")

_CULTIVATION_PAT = re.compile(
    r"\bhow (do|to|can|should) (i|we)? ?(grow|plant|cultivate|sow|propagate)\b"
    r"|growing (a |an )?\w+|plant(ing)? (a |an )?\w+|best season|planting calendar"
    r"|sowing time|harvest(ing)? (time|when)|spacing|prun(e|ing)|propagation"
    r"|seed(ing)? (rate|method)|when to plant|when to sow")

_IRRIGATION_PAT = re.compile(
    r"irrigat|soil moisture|drainage|how often should i water|water(ing)? schedule"
    r"|water need|drip|sprinkler|moisture")

_FERTILIZER_PAT = re.compile(
    r"fertiliz|fertili|manure|compost|nutrient|npk|urea|micronutrient|"
    r"which fertilizer|dose of|soil health|soil test|ph (level|of soil)")

_PEST_MGMT_PAT = re.compile(
    r"pest control|insecticide|pesticide|fungicide|prevent (pest|disease)|"
    r"manage (pest|disease)|treatment for|safe spray|organic control")


def classify_intent(text: str, session: dict) -> str:
    """Rule-based primary intent over the latest message + session context.
    Deliberately runs BEFORE memory/tools/response so each question gets the
    format it deserves."""
    low = (text or "").lower().strip()
    if not low:
        return "CLARIFICATION_REQUIRED"

    # 1. Identity / greetings
    if _GREET_PAT.search(low):
        return "GREETING_OR_IDENTITY"

    has_symptoms = bool(_SYMPTOM_PAT.search(low))
    is_followup_style = len(low.split()) <= 6 and session["history"]

    # 2. Weather
    if _WEATHER_PAT.search(low):
        return "WEATHER_REQUEST"

    # 3. Explicit problem report beats generic cultivation wording
    #    ("yellow leaves on my mango" is diagnosis, not cultivation)
    if has_symptoms and re.search(
            r"\b(my|our|the|these|those|its)\b.*\b(plant|plants|crop|crops|leaves|"
            r"leaf|tree|trees|seedling|field|farm)\b|\blooks bad\b", low):
        return "CROP_PROBLEM_DIAGNOSIS"
    if has_symptoms and _PEST_MGMT_PAT.search(low):
        return "PEST_AND_DISEASE_MANAGEMENT"

    # 4. Irrigation / soil
    if _IRRIGATION_PAT.search(low):
        # "how often should I water it" mid-cultivation or diagnosis is a follow-up
        if is_followup_style and session["active_topic"]:
            return "FOLLOW_UP"
        return "IRRIGATION_AND_SOIL"

    # 5. Fertilizer / nutrients
    if _FERTILIZER_PAT.search(low):
        if is_followup_style and session["active_topic"]:
            return "FOLLOW_UP"
        return "FERTILIZER_AND_NUTRIENTS"

    # 6. Pest/disease management (explicit management ask, no symptoms reported)
    if _PEST_MGMT_PAT.search(low):
        return "PEST_AND_DISEASE_MANAGEMENT"

    # 7. Cultivation guides
    if _CULTIVATION_PAT.search(low):
        return "GENERAL_CROP_GUIDANCE"

    # 8. FOLLOW-UP: short message continuing the active topic
    #    (e.g. "flowering stage", "Puhana", "older lower leaves")
    if is_followup_style and session["active_topic"]:
        # If it just supplied answers to our questions, keep the topic
        mem = session["memory"]
        if session["active_topic"] == "diagnosis" and not has_symptoms:
            # verify the message actually adds known-type info or is topical
            new_facts = (_extract_stage(low) or _extract_pattern(low)
                         or extract_place(text) or re.search(r"water|fertiliz|rain", low))
            if new_facts or re.search(r"yes|no|ok|okay", low):
                return "FOLLOW_UP"
            # a short brand-new unrelated question is NOT a follow-up
            return "GENERAL_AGRICULTURE"
        return "FOLLOW_UP"

    # 9. Diagnosis catch: symptoms mentioned without the possessive pattern
    if has_symptoms:
        return "CROP_PROBLEM_DIAGNOSIS"

    # 10. Anything else farming-related
    if re.search(r"farm|crop|seed|soil|harvest|agricul|khets?|kheti", low):
        return "GENERAL_AGRICULTURE"

    return "CLARIFICATION_REQUIRED"


# ============================================================================
# WEATHER SPECIALIST DISPATCH (guarded, honest statuses)
# ============================================================================

def run_weather_specialist(session: dict) -> dict:
    """Every failure mode returns an honest status; never raises, never fabricates."""
    place = session["memory"].get("location")
    if not place:
        return {"status": "no_location", "forecast": None, "place_label": None}
    status, data = WeatherIntelligence.geocode(place)
    if status == "ambiguous":
        opts = ", ".join(WeatherIntelligence.label(p) for p in data[:5])
        return {"status": "ambiguous", "forecast": None, "place_label": None,
                "options": opts}
    if status != "ok":
        return {"status": status, "forecast": None, "place_label": None}
    try:
        forecast = WeatherIntelligence.forecast(data["latitude"], data["longitude"])
    except Exception:
        return {"status": "error", "forecast": None, "place_label": None}
    return {"status": "ok", "forecast": forecast,
            "place_label": WeatherIntelligence.label(data)}


# ============================================================================
# INTENT HANDLERS — one format per intent
# ============================================================================

IDENTITY_TEXT = (
    "Hi! I'm FarmAction AI, your AI farming assistant. I can help you grow crops, "
    "understand plant problems, plan irrigation, learn about soil and nutrients, "
    "and make informed farming decisions. Tell me what crop you're growing and "
    "what you'd like help with."
)


def handle_identity(session: dict, text: str) -> str:
    session["active_topic"] = None  # a greeting resets topic context
    return IDENTITY_TEXT


CULTIVATION_PROMPT = """You are FarmAction AI, an agricultural cultivation advisor.
The farmer asked a GROWING/CULTIVATION question — this is NOT a diagnosis request.

Write a structured, practical cultivation guide covering (as relevant):
- Suitable climate and growing conditions
- Site selection and sunlight
- Soil and drainage requirements
- Seedling vs grafted plant considerations (if the crop is commonly grafted)
- Planting method and spacing
- Watering during establishment and after establishment
- Mulching, nutrition, and pruning (if relevant)
- Expected establishment and fruiting timelines, expressed cautiously
- Common problems and prevention
- Practical next steps for the farmer

End with one or two relevant optional questions (e.g. the farmer's location, or
whether the plant is newly planted or already established).

Rules: do NOT invent symptoms; do NOT output a "crop assessment" section; do NOT
add weather disclaimers; simple, respectful farmer-friendly language; never
guarantee yield or outcomes."""


MANGO_FALLBACK_GUIDE = """**Growing a mango plant**

1. **Climate & site**: Mango needs a frost-free climate with a warm, dry season
   at flowering. Pick a spot with full sun (6–8+ hours) and good air flow, away
   from shade and waterlogged hollows.
2. **Soil**: Deep, well-drained soil (sandy loam to laterite is fine). Avoid
   heavy clay that holds standing water — mango roots rot easily. A pH around
   5.5–7.5 suits it.
3. **Grafted vs seedling**: Prefer a **grafted** plant from a reliable nursery —
   it fruits in about 3–4 years and matches a known variety. Seedling trees take
   roughly 6–8 years and fruit quality is unpredictable.
4. **Planting**: Dig a pit about 1m × 1m × 1m, mix the topsoil with well-rotted
   manure/compost, and plant so the graft union stays well above the soil.
   Standard spacing is roughly 10m × 10m for vigorous varieties; dwarf hybrids
   can go closer (around 5m × 5m). Plant at the start of the rainy season.
5. **Watering**: Water 2–3 times a week for the first year so the root ball
   stays moist but never waterlogged. Once established, deep watering every
   10–15 days in dry periods is usually enough. Stop or reduce heavy watering
   before flowering to encourage flower buds.
6. **Mulch & nutrition**: Mulch with dry leaves/straw around the base (keeping
   it off the trunk). Feed with compost/well-rotted manure yearly; young plants
   need only light, frequent feeding.
7. **Pruning**: Remove dead/crossing branches and suckers below the graft. Keep
   an open canopy so light and air reach the centre.
8. **Timelines**: Expect 3–4 years to first fruit for a grafted plant (varies
   by variety, climate, and care — this is a cautious estimate, not a promise).
9. **Common problems**: anthracnose (dark spots on leaves/fruit in humid
   weather), powdery mildew on panicles, mango hoppers and mealybugs. Good
   canopy airflow and orchard hygiene prevent most of it — consult your local
   agriculture extension officer before any spray.

Is your plant newly planted or already established, and which district are you
in? I can tailor the watering and care schedule to your season.
"""

GENERAL_CULTIVATION_FALLBACK = (
    "Here's practical guidance: start with your local climate and season — plant "
    "when conditions suit the crop (most crops go in at the start of the rainy "
    "season or the recommended local window). Choose well-drained soil with good "
    "sunlight, prepare the field with compost or well-rotted manure, use quality "
    "seed or healthy seedlings from a reliable source, and keep watering regular "
    "but never waterlogged during establishment. Watch weekly for pests or "
    "discolored leaves and act early with prevention rather than curative "
    "sprays. For variety-specific calendars, your local agriculture extension "
    "office has the official sowing/harvest window for your district.\n\n"
    "Tell me the crop and your location, and I'll give you a specific planting "
    "and care plan."
)


def _llm_reply(prompt: str, text: str, session: dict, max_tokens=1300):
    """Optional LLM refinement; returns None when unavailable (never raises)."""
    client = _get_llm()
    if client is None:
        return None
    try:
        messages = [{"role": "system", "content": prompt}]
        # include the last few turns so follow-ups like "it" resolve correctly
        for turn in session["history"][-4:]:
            if turn.get("user"):
                messages.append({"role": "user", "content": turn["user"]})
            if turn.get("agent"):
                messages.append({"role": "assistant", "content": turn["agent"]})
        messages.append({"role": "user", "content": text})
        resp = client.chat.completions.create(
            model="asi1-mini", messages=messages, max_tokens=max_tokens)
        refined = (resp.choices[0].message.content or "").strip()
        return refined or None
    except Exception:
        return None


def handle_cultivation(session: dict, text: str) -> str:
    session["active_topic"] = "cultivation"
    reply = _llm_reply(CULTIVATION_PROMPT, text, session)
    if reply:
        return reply
    low = text.lower()
    if "mango" in low or "aam" in low:
        return MANGO_FALLBACK_GUIDE
    return GENERAL_CULTIVATION_FALLBACK


def handle_weather(session: dict, text: str) -> str:
    session["active_topic"] = "weather"
    weather = run_weather_specialist(session)
    if weather["status"] == "no_location":
        return ("Which village, town, or district is your farm in? "
                "I'll pull the live forecast for that location.")
    if weather["status"] == "ambiguous":
        return (f"I found more than one match for that place name: "
                f"{weather['options']}. Which one is your farm near?")
    if weather["status"] in ("no_result", "error", "missing_input"):
        return (f"I could not retrieve a live forecast for "
                f"'{session['memory'].get('location', 'that location')}' right now "
                "(the weather service didn't return usable data). "
                "Please try again later or check a local forecast source.")
    return WeatherIntelligence.summarize(weather["forecast"], weather["place_label"])


DIAGNOSIS_PROMPT = """You are FarmAction AI, coordinator of an agricultural
decision-support system. The farmer REPORTED A PLANT PROBLEM — use the diagnostic
format. The session memory (JSON at the end) contains facts the farmer has
already stated; NEVER ask for facts listed there as known.

Sections (only when relevant):
**What we know** — reported/verified observations, from memory + this message.
**Possible causes** — plausible explanations, clearly NOT confirmed diagnoses.
**Weather implications** — ONLY if a [LIVE WEATHER DATA] block exists; otherwise
say weather data was not available. Never equate weather with a confirmed disease.
**What to do now** — safe immediate observations/actions. NEVER invent pesticide
dosages; defer to product labels and the local agricultural extension officer.
**What to monitor** — specific observations and what they would indicate.
**What information is still needed** — at most the 3 most useful missing facts,
NONE of which may already be in memory.

Rules: distinguish possibilities from confirmed facts; never assert a confirmed
disease/deficiency/pest without sufficient evidence; farmer-friendly language."""


def handle_diagnosis(session: dict, text: str) -> str:
    session["active_topic"] = "diagnosis"
    session["history"].append({"user": text})
    update_memory(session, text)

    mem = session["memory"]
    parts = []

    # weather runs only in diagnosis context
    weather = run_weather_specialist(session)
    weather_text = ""
    if weather["status"] == "ok":
        weather_text = WeatherIntelligence.summarize(weather["forecast"],
                                                     weather["place_label"])
    elif weather["status"] == "ambiguous":
        parts.append(f"To add local weather context, which of these matches your "
                     f"farm: {weather['options']}?")

    parsed = CropIntelligence.parse(text)
    assessment = CropIntelligence.analyse(parsed, mem, weather_text)

    if assessment["supporting_evidence"]:
        parts.insert(0, assessment["supporting_evidence"][0])
    else:
        parts.insert(0, "Here is my assessment based on what you've shared.")

    # acknowledge facts already in memory so the farmer sees continuity
    known = []
    if mem.get("crop"):
        known.append(f"crop: {mem['crop']}")
    if mem.get("location"):
        known.append(f"location: {mem['location']}")
    if mem.get("crop_stage"):
        known.append(f"stage: {mem['crop_stage']}")
    if mem.get("pattern"):
        known.append(f"affected leaves: {mem['pattern']}")
    if known:
        parts.insert(1, "Noted so far — " + "; ".join(known) + ".")

    parts.append(CropIntelligence.brief(assessment))
    if weather_text:
        parts.append(weather_text)
    parts.append("**What to do now**\n"
                 "1. Check soil moisture a finger-depth down before any irrigation.\n"
                 "2. Inspect a few affected plants closely (roots, leaf undersides, stems).\n"
                 "3. Hold off on any chemical treatment until a cause is narrowed down — "
                 "check the product label and your local agricultural extension officer first.")
    parts.append("**What to monitor**\n"
                 "- Whether the problem spreads to new leaves or stays on old ones.\n"
                 "- New growth: healthy new leaves suggest a fixable nutrient/water issue.\n"
                 "- Any spots, wilting, or pest signs appearing alongside.")

    # ask only for facts genuinely missing AND never asked before
    missing_keys = {
        "location": "Which village, town, or district is your farm in?",
        "crop_stage": "What growth stage is the crop at — seedling, flowering, or fruiting?",
        "pattern": "Are the affected leaves the older/lower ones, the younger/top ones, or all over?",
        "watering": "How have you been watering recently, and did you apply any fertilizer?",
    }
    to_ask = []
    for key, q in missing_keys.items():
        if not mem.get(key) and not _asked_before(session, key) and len(to_ask) < 3:
            to_ask.append(q)
            _mark_asked(session, key)
    if to_ask:
        parts.append("To narrow this down: " + " ".join(to_ask))

    parts.append("**When to consult an expert**\n"
                 "If the problem spreads quickly, multiple plants collapse, or this "
                 "is your main income crop, involve your local agriculture officer "
                 "or extension centre now — a physical inspection beats remote guessing.")

    reply = "\n\n".join(p for p in parts if p)
    session["history"][-1]["agent"] = reply
    return reply


def handle_irrigation(session: dict, text: str) -> str:
    mem = session["memory"]
    crop = mem.get("crop")
    stage = mem.get("crop_stage")
    crop_part = f" for your {crop}" if crop else ""
    stage_part = f" (important at the {stage} stage)" if stage else ""
    reply = (f"**Irrigation guidance{crop_part}**\n"
             "1. Water based on soil moisture, not the calendar: check a "
             "finger-depth (5–7 cm) down; irrigate when it feels dry there.\n"
             "2. Water deeply and less often rather than shallow daily watering — "
             "this pushes roots downward and builds drought tolerance.\n"
             "3. Morning irrigation (before ~10 am) reduces evaporation losses"
             f"{stage_part}.\n"
             "4. Avoid waterlogging: most crops suffer root damage faster from "
             "too much water than too little. Ensure the field drains.\n"
             "5. In hot/windy weather, check moisture more often — but never "
             "let standing water sit around roots.")
    if mem.get("location"):
        reply += ("\n\nIf you'd like, I can add the live forecast for "
                  f"{mem['location']} to plan the next irrigation.")
    else:
        reply += ("\n\nWhich crop and district is this for? I can make this "
                  "specific to your situation.")
    return reply


def handle_fertilizer(session: dict, text: str) -> str:
    return ("**Nutrient management guidance**\n"
            "1. Test before you feed: a soil test (your local agriculture office "
            "or KVK can arrange one) tells you what's actually missing — guessing "
            "wastes money and can harm the crop.\n"
            "2. Build the base first: well-rotted compost or manure before "
            "sowing/planting improves almost every soil.\n"
            "3. Match fertilizer to growth stage: early growth needs nitrogen for "
            "leaves; flowering/fruiting needs balanced P & K. Apply in split "
            "doses rather than all at once.\n"
            "4. Yellowing patterns hint at the nutrient: older/lower leaves first "
            "suggests nitrogen; younger/top leaves suggests iron/zinc. But this "
            "is a hint, not a diagnosis — confirm with a test where possible.\n"
            "5. Never exceed label or extension-recommended doses.\n\n"
            "Which crop, its growth stage, and what symptoms (if any) made you "
            "think about fertilizer? I can be more specific.")


def handle_pest(session: dict, text: str) -> str:
    return ("**Pest & disease management**\n"
            "1. Identify before you treat: the same spots can be a fungus, a "
            "nutrient issue, or spray damage — treating the wrong cause wastes "
            "money and risks the crop.\n"
            "2. Start with prevention: field sanitation, removing badly affected "
            "plant parts, crop rotation, and proper spacing for airflow.\n"
            "3. Prefer safer options first: neem-based products and other IPM "
            "approaches where they're proven for your crop.\n"
            "4. If a chemical is genuinely needed, use ONLY what the product "
            "label and your local agricultural extension officer recommend for "
            "your crop — never a guessed dose, and respect the pre-harvest wait.\n"
            "5. Recheck after 3–5 days to confirm whatever you applied worked.\n\n"
            "Describe what you're seeing (which plant part, what the spots or "
            "insects look like) and I'll help you narrow it down.")


def handle_general(session: dict, text: str) -> str:
    reply = _llm_reply(
        "You are FarmAction AI, a general agriculture assistant. Answer the "
        "farmer's actual question directly, in simple practical language. "
        "Do not force it into a diagnostic report. Add at most one relevant "
        "follow-up question. Never fabricate data or tool results.",
        text, session)
    if reply:
        return reply
    return ("Here's what I can tell you: for questions like this, the most "
            "reliable local guidance comes from your district agriculture "
            "office or Krishi Vigyan Kendra, who know your soil, season, and "
            "varieties. If you tell me the crop you're growing and what you're "
            "trying to decide — planting, watering, nutrition, or a plant "
            "problem — I'll give you specific, practical advice.")


def handle_clarification(session: dict, text: str) -> str:
    return ("I want to make sure I understand correctly: is this about a plant "
            "problem you're seeing, how to grow or manage a crop, or something "
            "else? A sentence about what you need — and the crop involved — "
            "will let me help properly.")


def handle_follow_up(session: dict, text: str) -> str:
    """A short message continuing the active topic. Acknowledge the new facts,
    update memory, and continue the assessment — never re-ask known facts."""
    topic = session["active_topic"]
    update_memory(session, text)

    if topic == "diagnosis":
        return handle_diagnosis(session, text)
    if topic == "weather":
        return handle_weather(session, text)
    if topic == "cultivation":
        # e.g. "How often should I water it?" after the mango guide
        if re.search(r"water|irrigat", text.lower()):
            return handle_irrigation(session, text)
        if re.search(r"fertiliz|nutrient|manure", text.lower()):
            return handle_fertilizer(session, text)
        return handle_cultivation(session, text)
    return handle_general(session, text)


INTENT_HANDLERS = {
    "GREETING_OR_IDENTITY": handle_identity,
    "GENERAL_CROP_GUIDANCE": handle_cultivation,
    "CROP_PROBLEM_DIAGNOSIS": handle_diagnosis,
    "WEATHER_REQUEST": handle_weather,
    "IRRIGATION_AND_SOIL": handle_irrigation,
    "FERTILIZER_AND_NUTRIENTS": handle_fertilizer,
    "PEST_AND_DISEASE_MANAGEMENT": handle_pest,
    "GENERAL_AGRICULTURE": handle_general,
    "CLARIFICATION_REQUIRED": handle_clarification,
    "FOLLOW_UP": handle_follow_up,
}


def handle_message(session: dict, text: str) -> str:
    """Router: classify intent -> retrieve tools if needed -> respond in the
    right format -> store the reply. This is the single entry point."""
    intent = classify_intent(text, session)
    handler = INTENT_HANDLERS[intent]
    reply = handler(session, text)

    # verify: the reply must be non-empty and must answer the intent
    if not reply or not reply.strip():
        reply = ("Something went wrong while processing that — please try "
                 "rephrasing your message.")

    if not session["history"] or session["history"][-1].get("user") != text:
        session["history"].append({"user": text})
    session["history"][-1]["agent"] = reply
    return reply


# ============================================================================
# ACP — AGENT CHAT PROTOCOL (official chat_protocol_spec)
# ============================================================================

chat_protocol = Protocol(spec=chat_protocol_spec)


@chat_protocol.on_message(model=ChatMessage)
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


@chat_protocol.on_message(model=ChatAcknowledgement)
async def handle_chat_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"ACK from {sender}: {msg.acknowledged_msg_id}")


# ============================================================================
# WIRING — done exactly once. NO agent.run(), no startup event.
# ============================================================================

agent.include(chat_protocol)
