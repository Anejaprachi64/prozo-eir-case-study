import pandas as pd, pytest
from app.cs2_compliance import name_match, read_wagesheet, parse_ecr, run_verification
from pathlib import Path

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "cs2"

SAME = [("Rahul Kumar", "KUMAR RAHUL"), ("Rahul Kumar", "R Kumar"), ("Sunita Devi", "Suneeta Devi"), ("Mohammed Ali", "Mohd Ali"),
        ("Dinesh Kaur", "Dnesh Kaur"), ("Amit  Singh", "amit singh."), ("Anil Kumar Sharma", "Anil Sharma"), ("Singhal Ravi", "Sing hal Ravi"),
        ("Mr. Rohit Verma", "ROHIT VERMA"), ("Priya Sharma", "Priyaa Sharma")]
DIFF = [("Rahul Kumar", "Suresh Kumar"), ("Rajesh Kumar", "Rakesh Kumar"), ("Raj Singh", "Ram Singh"), ("Amit Sharma", "Sumit Sharma"),
        ("Rahul Kumar", "Rahul Verma"), ("Pooja Rani", "Neha Rani"), ("Anil Kumar", "Sunil Kumar")]


@pytest.mark.parametrize("a,b", SAME)
def test_trivial_variations_not_flagged(a, b): assert name_match(a, b)[0], name_match(a, b)


@pytest.mark.parametrize("a,b", DIFF)
def test_genuine_mismatch_flagged(a, b): assert not name_match(a, b)[0], name_match(a, b)


@pytest.mark.skipif(not (SAMPLE / "wagesheet.xlsx").exists(), reason="run scripts/make_cs2_samples.py first")
def test_end_to_end_matches_ground_truth():
    w = read_wagesheet(SAMPLE / "wagesheet.xlsx")
    pf, _ = parse_ecr(SAMPLE / "pf_ecr.pdf", "pf"); es, _ = parse_ecr(SAMPLE / "esic_ecr.pdf", "esic")
    res = run_verification(w, pf, es, 1.0)
    gt = pd.read_csv(SAMPLE / "ground_truth.csv", dtype=str).fillna("")
    m = res.merge(gt, left_on="UAN", right_on="uan")
    assert (m["PF Status"] == m["expected_pf"]).mean() >= 0.99
    assert (m["ESIC Status"] == m["expected_esic"]).mean() >= 0.99
