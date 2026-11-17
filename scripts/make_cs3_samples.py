"""Synthetic day-level P&L for 3 FCs (18-31 May 2026) with injected scenarios.
Tab 1 'Data' = Day, FC, Revenue + 6 cost lines (INR). Tab 2 'Targets' = per-line target ranges (% of revenue)."""
import random
from pathlib import Path
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "sample_data" / "cs3"; OUT.mkdir(parents=True, exist_ok=True)
random.seed(5)
LINES = ["Manpower Cost", "Packaging Cost", "Power & Fuel", "FC Rent", "Equipment Rentals", "Overheads"]
T = {"Manpower Cost": (22, 30), "Packaging Cost": (5, 8), "Power & Fuel": (8, 14), "FC Rent": (14, 22), "Equipment Rentals": (5, 9), "Overheads": (6, 10)}
# explicit % overrides: (fc, day) -> pct per line, in LINES order
OVR = {
    ("FC Ludhiana", 27): [32.8, 7.0, 15.2, 19.0, 8.0, 7.9],     # CM2 10.1% -> RED (also CM1 < tolerance)
    ("FC Ludhiana", 28): [29.0, 7.0, 14.6, 19.5, 8.5, 9.5],     # P&F still above max, CM2 ~ 12.9% -> YELLOW (streak)
    ("FC Ludhiana", 29): [28.0, 7.0, 14.4, 18.0, 8.0, 9.0],     # P&F 3rd day in a row
    ("FC Gurugram", 25): [27.0, 9.2, 10.0, 17.0, 6.5, 7.0],     # line-only breach: packaging; CM2 healthy
    ("FC Pune", 30): [25.0, 6.0, 11.0, 12.0, 5.0, 5.0],         # CM2 36% -> BLUE suspicious (rent & overheads below min)
    ("FC Pune", 21): [27.0, 7.0, 13.0, 21.0, 9.0, 10.0],        # CM2 yellow (low side), no breach
}
rows = []
for fc in ("FC Ludhiana", "FC Gurugram", "FC Pune"):
    for day in range(18, 32):
        rev = random.randint(850_000, 1_350_000)
        pct = OVR.get((fc, day)) or [random.uniform(lo + (hi - lo) * .3, lo + (hi - lo) * .65) for lo, hi in T.values()]
        rows.append({"Day": pd.Timestamp(2026, 5, day), "FC": fc, "Revenue": rev, **{l: round(rev * p / 100) for l, p in zip(LINES, pct)}})
with pd.ExcelWriter(OUT / "Case_Study_3_sample.xlsx") as xw:
    pd.DataFrame(rows).to_excel(xw, sheet_name="Data", index=False)
    pd.DataFrame([{"Line Item": l, "Min %": lo, "Max %": hi} for l, (lo, hi) in T.items()] +
                 [{"Line Item": "CM1", "Min %": 65, "Max %": 70}, {"Line Item": "CM2", "Min %": 15, "Max %": 30}]).to_excel(xw, sheet_name="Targets", index=False)
print("ok", len(rows))
