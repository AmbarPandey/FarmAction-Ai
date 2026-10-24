# ============================================================================
# FarmAction AI — Task 9 Minimal Defect Patches
# Apply by copy-paste into agent.py at the marked locations. No other changes.
# ============================================================================

# ----------------------------------------------------------------------------
# P-1  WeatherIntelligence.geocode swallows ALL exceptions as "no_result",
#      so a network outage is indistinguishable from an unknown place name.
#      Fix: return an explicit ("error", []) status; callers disclose it.
# ----------------------------------------------------------------------------
# BEFORE (in geocode):
#     except Exception:
#         return ("no_result", [])
# AFTER:
#     except Exception:
#         return ("error", [])   # caller MUST disclose live-data unavailability

# ----------------------------------------------------------------------------
# P-2  Coordinator does not guard specialist calls, so one module exception
#      (test O2) crashes the whole turn. Fix: wrap each specialist invocation
#      in try/except and keep valid results from the other module.
# ----------------------------------------------------------------------------
# In the coordinator pipeline, replace the direct call:
#     assessment = CropIntelligence.assess(memory)
# with:
#     assessment, degraded = None, []
#     try:
#         assessment = CropIntelligence.assess(memory)
#     except Exception as e:
#         degraded.append(f"crop analysis unavailable ({type(e).__name__})")
#     try:
#         weather = run_weather(memory)          # existing weather step
#     except Exception as e:
#         weather = None
#         degraded.append(f"weather unavailable ({type(e).__name__})")
#     # When degraded: prepend "Note: " + "; ".join(degraded) to the final plan
#     # and do NOT claim a complete assessment.

# ----------------------------------------------------------------------------
# P-3  Malformed tool output (test F4): forecast() trusts payload shape.
#      Fix: validate the daily structure before normalization.
# ----------------------------------------------------------------------------
# In WeatherIntelligence.forecast, after parsing the JSON response:
#     daily_raw = payload.get("daily", {})
#     if not isinstance(daily_raw, dict) or not daily_raw.get("time"):
#         return {"location_ok": True, "missing": ["malformed forecast payload"],
#                 "fabricated": False}
