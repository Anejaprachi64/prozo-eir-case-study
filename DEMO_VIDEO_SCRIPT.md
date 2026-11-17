# Demo Video Script (target: 5–7 minutes)

## Before recording
- Start the app with `python run.py` and open `http://127.0.0.1:5000`.
- Use the bundled sample buttons so the walkthrough is repeatable.
- Keep the terminal and browser readable; close unrelated tabs and notifications.
- Do not show `.env`, passwords, personal data or real employee records.

## 0:00–0:30 — Introduction
"This is my Prozo EIR case-study implementation. It is a single Flask application with three workflows: parcel-image extraction, wagesheet-versus-ECR verification, and a daily FC P&L monitor. The demo uses synthetic sample data because the original source datasets were not accessible in my environment."

## 0:30–2:00 — Case Study 1: Parcel images
- Open **Parcel images**.
- Run the bundled sample or upload the sample image ZIP.
- Point out the extracted AWB/weight/dimensions, status flags and notes.
- Show the CSV export and explain why uncertain extractions are flagged for review rather than silently trusted.

## 2:00–3:30 — Case Study 2: Wagesheet vs ECR
- Open **Wagesheet vs ECR** and run the bundled sample.
- Show the summary counts and employee-level PF/ESIC statuses.
- Open the Excel report and briefly show Summary, Per-employee results and Exceptions only.
- Explain name matching, amount tolerance and the distinction between Missing and Not applicable.

## 3:30–5:30 — Case Study 3: Daily FC P&L
- Open **Daily FC P&L monitor** and run the bundled sample.
- Show the FC-by-day CM2 dashboard and colour-coded statuses.
- Show anomaly log and insights, highlighting the Ludhiana red day and Pune suspicious-high blue day.
- Open one digest preview and show the generated Excel report tabs.
- Mention that notifications default to dry-run; live SMTP/Slack delivery requires environment configuration and was not tested.

## 5:30–6:15 — Engineering and limitations
- Mention background jobs and Recent Jobs.
- Mention clear validation for missing/incorrect files.
- State that fixtures are synthetic and real source files would be needed to calibrate parsing and thresholds.

## 6:15–6:30 — Close
"The code, setup instructions, case-study notes and sample data are available in the GitHub repository. Thank you."
