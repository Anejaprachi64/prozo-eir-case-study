# Case Study 3 — Daily FC P&L monitor & alert digest

**Run:** `/cs3` → upload daily data (xlsx/csv; `FC` column optional) → dashboard, anomaly log, insights, per-FC digests; downloads: `pnl_report.xlsx` (Dashboard, Daily P&L, Anomalies, Insights, Targets used), `anomalies_log.csv`, `digest_<FC>_<date>.html` (+ preview).
Optional uploads: targets file, FC→manager CSV (`FC,Manager,Email,Slack`). Defaults live in `config/`.

## Agentic decomposition (`app/cs3_pnl.py`)
Five small agents run in a fixed order; each is a plain function so it is testable, cheap and deterministic:
1. **IngestAgent** — finds the header row, normalises columns (longest-keyword match: "Manpower" ≠ "Power"), cleans ₹/commas, skips bad rows *and tells you how many*, reads target ranges from the workbook (else `config/targets.csv`; the ranges used are shown on the page).
2. **FinanceAgent** — every line as % of revenue, CM1 = Rev − Manpower − Packaging, CM2 = CM1 − the rest, both as % of revenue (rounded to 2 dp so ₹-rounding never creates a fake breach).
3. **WatchAgent** — flags lines outside their range; CM1/CM2 beyond target ±3 pp; colour per FC-day (Red <12, Yellow 12–14.9 or 30–33, Green 15–30, **Blue >33 = suspiciously high**). Logs `Date | FC | Line Item | % of Revenue | Target Range | Status`.
4. **InsightAgent** — per anomalous FC-day: deviation size, breached lines ranked by ₹ impact, streaks ("3 days in a row"), 7-day trend, and a recommended action (Red → cost audit; Blue → verify under-reported costs; line-only → line-specific check). Deterministic template by default; if `OLLAMA_MODEL` is set a local LLM may re-word it, **rejected automatically if any number from the original is missing**.
5. **NotifierAgent** — HTML + text digest per FC to its manager. Real channels: **SMTP email** (`SMTP_*` env vars) and/or **Slack webhook** (`SLACK_WEBHOOK_URL`). Default is a **dry run** that saves `.eml` files (tick "Actually send" to send). Failed sends are reported per FC.

Central dashboard = FC × day CM2% grid, colour-coded (web page + `Dashboard` sheet).

Example output (sample data): *"On 27 May 2026, FC Ludhiana: CM2 = 10.1% (RED - 1.9 pp below the 12% red line; target 15–30%). CM1 = 60.2% (outside 65–70% ±3pp). Over limit (largest impact first): Manpower Cost = 32.8% (max 30%, +2.8 pp ≈ ₹37,735); Power & Fuel = 15.2% (max 14%, +1.2 pp ≈ ₹16,172). … Recommended action: immediate cost audit of Manpower Cost, Power & Fuel; escalate to the FC head today."*

Sample data (3 FCs × 14 days, 18–31 May 2026) deliberately contains: clean days, a CM2 breach (RED), a 3-day Power & Fuel streak, a line-only breach (Packaging at a healthy CM2), a suspicious-high CM2 (BLUE), and a YELLOW day with no single breach. Digest date for the sample = 27 May.

## How this gets smarter over time
* **Memory (implemented):** every FC-day is stored in SQLite (`data/memory.db`) and read back on later runs, so streaks and week-over-week trend work across uploads. Next: per-FC *learned* baselines (seasonality, day-of-week), so "normal" is FC-specific rather than one global range, and alert only on statistically unusual moves.
* **Manual overrides (implemented minimally):** `config/overrides.csv` (`FC,Date,Line Item,Reason`; `Line Item` blank = whole day) marks an anomaly *Acknowledged* with the manager's reason and carries that into the log/insight. Next: a UI button, auto-expiry, and using acknowledged reasons as training signal ("festive-season power spike is expected").
* **Trend tracking:** alert on *drift* before a breach (3 days rising toward a max), on repeated acknowledged anomalies, and weekly/monthly rollups per FC and per cost line.
* **Closed loop:** managers reply "fixed/expected/investigating" in Slack/email → stored → suppresses duplicates and measures time-to-resolution. An LLM agent can then draft root-cause hypotheses from notes + history, always cross-checked against the numbers.

## Known limitations
* Target ranges are read heuristically from the workbook (a row whose label is a line item followed by min/max or "a–b%"). If the real sheet lays them out differently, they silently fall back to `config/targets.csv` defaults **(and the page says so)** — upload a tidy targets CSV in that case. My default ranges are plausible guesses, not Prozo's.
* Yellow days are shown in the dashboard/digest but only rule breaches go in the anomaly log (CM2 in 12–14.9 is within ±3 pp of target so it is not a "flag" per the brief).
* Gap in the brief's colour bands (32.9–33.0) is treated as Yellow.
* Revenue ≤ 0 rows are skipped (warned). Blank costs are treated as 0 (warned) — that could hide exactly the under-reporting the Blue flag is meant to catch.
* No scheduler: the run is triggered by upload. Production would run daily via cron/GitHub Actions on a data drop.
* Email delivery was tested in dry-run only (no SMTP account here); the SMTP/Slack code paths are standard `smtplib`/webhook calls.
