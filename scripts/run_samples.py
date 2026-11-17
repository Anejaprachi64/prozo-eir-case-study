"""Run all three pipelines on the bundled sample data and copy results to sample_outputs/ (no server needed)."""
import shutil, tempfile, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from app import cs1_parcels, cs2_compliance, cs3_pnl
OUT = ROOT / "sample_outputs"; OUT.mkdir(exist_ok=True)
nop = lambda *a: None


def job():
    d = Path(tempfile.mkdtemp()); (d / "out").mkdir(); (d / "in").mkdir(); return d


d = job(); imgs = sorted((ROOT / "sample_data" / "cs1_images").iterdir())
cs1_parcels.process_batch(d, nop, [(p.name, p) for p in imgs]); shutil.copy(d / "out" / "shipments.csv", OUT / "cs1_shipments.csv")
d = job(); S = ROOT / "sample_data" / "cs2"
cs2_compliance.process(d, nop, S / "wagesheet.xlsx", S / "pf_ecr.pdf", S / "esic_ecr.pdf", 1.0); shutil.copy(d / "out" / "verification_report.xlsx", OUT / "cs2_verification_report.xlsx")
d = job(); r = cs3_pnl.process(d, nop, ROOT / "sample_data" / "cs3" / "Case_Study_3_sample.xlsx", None, None, "", False, "2026-05-27", "FC-1")
(OUT / "cs3").mkdir(exist_ok=True)
for f in r["files"]: shutil.copy(d / "out" / f, OUT / "cs3" / f)
for f in (d / "out" / "outbox").iterdir(): shutil.copy(f, OUT / "cs3" / f.name)
print("sample outputs written to", OUT)
