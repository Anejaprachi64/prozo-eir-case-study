# Case Study 2 — Wagesheet vs ECR compliance verification

**Run:** `/cs2` → upload wagesheet (.xlsx) + PF ECR PDF + ESIC ECR PDF (either PDF alone is fine) → **Download verification_report.xlsx**.
Sheets: `Summary` (counts), `Per-employee results` (status + reason for PF and ESIC separately, both amounts, difference), `Exceptions only`, `In … ECR not in wagesheet` (reverse check), `Notes & assumptions` (incl. parse sanity checks).

## Approach (deterministic — no LLM; fast, free, auditable)
* **PDF parsing** (`parse_ecr`): PyMuPDF words → regrouped into visual rows by y-position → a row is a member if it begins with a 12-digit UAN (PF) / 10- or 17-digit IP number (ESIC). Names that wrap to a second line are re-joined by x-alignment. Page-by-page, so *hundreds of pages are fine* (168 pages parsed + matched in 0.6 s).
* **Amount columns**: PF EE share = 5th numeric after the name (Gross, EPF wages, EPS wages, EDLI wages, **EE**); ESIC contribution found as the number that equals ceil(0.75% × wages) among the row's numbers, else 3rd. Then a **sanity check** (how many rows satisfy 12% / 0.75%) — if <50% do, a warning says *the column mapping is probably wrong for this PDF layout* instead of silently trusting it.
* **Verdicts** per employee, per scheme: `Matched / Name mismatch / Amount mismatch / Missing` plus `Not applicable` (no ID *and* no deduction, e.g. wages above the ESIC ceiling — not "missing"). Name mismatch is reported even if the amount also differs (the reason includes the amount difference).
* **Amount tolerance:** ±₹1 (configurable on the form) because statutory amounts are rounded to whole rupees.

### Name-matching tolerance (the heart of it — `name_match`)
**Not flagged:** case, punctuation, titles (Mr/Smt…), **word order** (KUMAR RAHUL), **spacing** (SING HAL / SINGHAL), phonetic spelling variants (SUNITA/SUNEETA, KAVITA/KAVITHA, MOHD/MOHAMMED), **one-letter typos in names ≥6 letters** (DINESH/DNESH), **initials** (R KUMAR vs RAHUL KUMAR), and **one omitted word** (middle name/surname dropped) — also when combined with spacing/order changes.
**Flagged:** a different given name or surname — RAHUL/SURESH KUMAR, RAJESH/RAKESH (similarity 0.83 < 0.85 threshold on purpose), RAJ/RAM (short names must match exactly), ANIL/SUNIL, KUMAR/VERMA surnames.
The reason column says *why* a variation was tolerated, so a reviewer can disagree. Trade-off: any threshold can be wrong for some pair; the matrix of 17 example pairs in `tests/test_cs2.py` documents the intended behaviour.

## Result on test data
Synthetic ground truth (I injected word-order/typo/initial/spacing variations, genuine name swaps, amount errors, missing members): **220 employees → 100% of PF and ESIC verdicts correct; 3,000 employees (104 + 64 pages) → 100%**, whole pipeline ≈ 0.6 s. **Caveat:** I wrote both the generator and the matcher; see limitations.

## Known limitations (be skeptical here)
* The real ECR PDFs were not available, so the parser targets the *documented* EPFO/ESIC columns. If real layouts differ (merged cells, different column order, ESIC "contribution history" spanning several months), look at the parse warnings in the report first. `PF_EE_INDEX` is the single constant to change.
* If an ID appears several times in an ECR (arrears, multi-month history) amounts are **summed** and the reason says so; this may be wrong for multi-month ESIC history — filter to the wage month first.
* Scanned/image PDFs are rejected with a clear message (no OCR fallback).
* Matching assumes Latin-script names. Name-only checks can't detect two employees swapping IDs with similar names.
* Report counts are values (snapshot), not live Excel formulas.

## With more time
Header-driven column detection (find the "EE" header's x-position instead of positional), multi-month ESIC support with a month selector, a review UI to approve/override flagged names (persisted as an allow-list per vendor), an LLM only as a *second opinion* on borderline names (score 70–85) rather than as the primary matcher, and a vendor-level compliance trend report.
