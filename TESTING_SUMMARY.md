# Manual Testing Summary

Testing was performed locally against the bundled synthetic datasets. The original Prozo source datasets were not available, so results should not be presented as validation on production data.

## Case Study 1 — Parcel images

- Individual image processing and batch upload
- ZIP upload
- Clean, empty-frame, blurry, blocked-label, partial-label, multiple-parcel and corrupt-image scenarios
- Status counts and CSV export
- Search/filter behaviour, including no-result search
- Recent Jobs and reopening completed jobs
- Empty-upload validation

Observed sample batch: 16 images — 6 `OK`, 2 `NO_PARCEL`, 1 `LABEL_UNREADABLE`, 2 `LABEL_BLOCKED`, 2 `PARTIAL_PARCEL`, 2 `MULTIPLE_PARCELS`, 1 `IMAGE_CORRUPT`.

## Case Study 2 — Wagesheet vs ECR

- Bundled 220-employee sample processing
- PF and ESIC verdicts and exception reasons
- Search/filter checks
- Excel report download and workbook sheets
- Empty upload and missing-ECR validation
- PF-only / ESIC applicability scenarios were exercised during manual review

The exact ₹1 tolerance boundary and a deliberately duplicated ECR ID were not independently verified with dedicated fixtures. The implementation contains duplicate-ID aggregation, but this edge case should not be described as manually tested.

## Case Study 3 — Daily FC P&L monitor

- Bundled sample processing and dashboard
- CM1 / CM2 and cost percentages
- Red, yellow, green and blue status scenarios
- Anomaly and Insights filters
- Excel report tabs, anomaly CSV and target ranges
- Digest previews for Gurugram, Ludhiana and Pune
- Empty upload and wrong-file/header validation
- Correct CSV upload
- Recent Jobs reopening and report download

Custom target-file override behaviour was not independently confirmed. Digest delivery was exercised in dry-run mode; real SMTP/Slack delivery was not tested.

## Test-data caveat

All included sample inputs are synthetic. Passing these tests demonstrates the implemented workflow against the supplied fixtures, not real-world accuracy or production readiness. Validate with representative source files before operational use.
