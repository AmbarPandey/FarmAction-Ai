# FarmAction AI — Testing

## Automated suite

`test_farmaction.py` (from Stage 9, committed unchanged) imports the real
`agent.py` and produces `task9_evidence.json` with per-test input, expected,
actual, tool-call evidence, PASS/FAIL, and remaining issues.

```bash
python test_farmaction.py            # includes live Open-Meteo tests
python test_farmaction.py --offline  # skip network-dependent tests
