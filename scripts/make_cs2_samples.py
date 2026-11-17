"""Generate a synthetic wagesheet + PF ECR PDF + ESIC ECR PDF with KNOWN ground truth.
Usage: python scripts/make_cs2_samples.py [n_employees=220] [outdir=sample_data/cs2]
The real sample was not downloadable offline, so formats mimic the EPFO ECR / ESIC contribution-history layouts described in the brief.
"""
import math, random, sys
from pathlib import Path
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

N = int(sys.argv[1]) if len(sys.argv) > 1 else 220
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "sample_data" / "cs2")
OUT.mkdir(parents=True, exist_ok=True)
random.seed(11)
FIRST = "Rahul Suresh Amit Sunita Priya Mohammed Rajesh Rakesh Anil Sunil Kavita Pooja Vikram Deepak Neha Sanjay Manoj Ritu Geeta Harpreet Gurpreet Jaswinder Balwinder Simran Ankit Rohit Sachin Manish Pankaj Ashok Ramesh Mukesh Naveen Kiran Anita Seema Rekha Meena Lalita Kamlesh Dinesh Vijay Ajay Sumit Tarun Varun Nitin Yogesh".split()
LAST = "Kumar Singh Sharma Verma Yadav Gupta Mishra Chauhan Thakur Patel Khan Ahmed Das Roy Saini Bansal Mehta Joshi Rana Tiwari Pandey Dhillon Sandhu Kaur Devi".split()
STATES = ["Punjab", "Haryana", "Maharashtra", "Karnataka", "Delhi"]
AGENCIES = ["Alpha Manpower", "Bharat Staffing", "Careforce Services"]


def uan(): return str(random.randint(10**11, 10**12 - 1))
def ip(): return str(random.randint(10**9, 10**10 - 1))


def typo(n):
    i = random.randrange(1, len(n) - 1); return n[:i] + n[i + 1:] if random.random() < .5 else n[:i] + n[i] + n[i:]


def vary(name):
    """trivial variations that must NOT be flagged -> (variant, kind)"""
    p = name.split()
    k = random.choice(["order", "typo", "initial", "case", "spacing", "phon", "dropmid"])
    if k == "order" and len(p) >= 2: return " ".join(reversed(p)), k
    if k == "typo" and len(p[0]) >= 6: return typo(p[0]) + " " + " ".join(p[1:]), k
    if k == "initial": return p[0][0] + " " + " ".join(p[1:]), k
    if k == "spacing" and len(p[-1]) >= 5: return p[0] + " " + p[-1][:3] + " " + p[-1][3:], k
    if k == "phon":
        for a, b in (("Sunita", "Suneeta"), ("Kavita", "Kavitha"), ("Mohammed", "Mohd"), ("Priya", "Priyaa")):
            if p[0] == a: return b + " " + " ".join(p[1:]), k
    if k == "dropmid": return name, "same"
    return name.upper(), "case"


def table_pdf(path, title, header, rows, widths, name_col, landscape_=True):
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=30, bottomMargin=30)
    st = getSampleStyleSheet()["BodyText"]; st.fontSize = 7.5; st.leading = 9
    data = [header] + [[Paragraph(str(c), st) if i == name_col else str(c) for i, c in enumerate(r)] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7.5), ("GRID", (0, 0), (-1, -1), .25, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    doc.build([Paragraph(title, getSampleStyleSheet()["Title"]), Spacer(1, 6), t],
              onLaterPages=lambda c, d: c.drawString(24, 15, f"Page {d.page}"), onFirstPage=lambda c, d: c.drawString(24, 15, f"Page {d.page}"))


people, truth = [], []
used = set()
for i in range(N):
    while True:
        nm = f"{random.choice(FIRST)} {random.choice(LAST)}"
        if random.random() < .25: nm = f"{random.choice(FIRST)} {random.choice(FIRST)} {random.choice(LAST)}"   # long names wrap in the PDF
        if nm not in used: used.add(nm); break
    gross = random.choice([12000, 14500, 15000, 16800, 18000, 21500, 24000, 30000])
    epf_wages = min(gross, 15000)
    pf = round(0.12 * epf_wages)
    esic_applicable = gross <= 21000
    esic = math.ceil(0.0075 * gross - 1e-9) if esic_applicable else 0
    people.append(dict(name=nm, uan=uan(), ip=ip() if esic_applicable else "", gross=gross, epf_wages=epf_wages, pf=pf, esic=esic,
                       agency=random.choice(AGENCIES), state=random.choice(STATES)))

pf_rows, esic_rows = [], []
for i, p in enumerate(people):
    pf_v = esic_v = "Matched"; pf_name = esic_name = p["name"]; pf_amt, esic_amt = p["pf"], p["esic"]; pf_id, esic_id = p["uan"], p["ip"]
    r = random.random()
    note = ""
    if r < .12: pf_name, k = vary(p["name"]); esic_name = pf_name; note = "variation:" + k              # tolerated
    elif r < .16: pf_name = esic_name = f"{random.choice(FIRST)} {random.choice(LAST)}"; pf_v = "Name mismatch"; esic_v = "Name mismatch" if p["ip"] else "Not applicable"
    elif r < .20: pf_amt -= random.choice([100, 300, 1800]) if p["pf"] > 300 else 0; pf_v = "Amount mismatch" if pf_amt != p["pf"] else "Matched"
    elif r < .24 and p["ip"]: esic_amt += random.choice([5, 40]) if random.random() < .3 else -random.choice([3, 20, 66]); esic_amt = max(esic_amt, 1); esic_v = "Amount mismatch" if esic_amt != p["esic"] else "Matched"
    elif r < .28: pf_v = "Missing"
    elif r < .32 and p["ip"]: esic_v = "Missing"
    if not p["ip"]: esic_v = "Not applicable"
    if pf_v != "Missing":
        eps = round(0.0833 * p["epf_wages"]); pf_rows.append([i + 1, pf_id, pf_name.upper(), p["gross"], p["epf_wages"], p["epf_wages"], p["epf_wages"], pf_amt, eps, pf_amt - eps, 0, 0])
    if esic_v not in ("Missing", "Not applicable"):
        esic_rows.append([len(esic_rows) + 1, esic_id, esic_name.upper(), 26, p["gross"], esic_amt, math.ceil(0.0325 * p["gross"]), "", ""])
    truth.append(dict(name=p["name"], uan=p["uan"], ip=p["ip"], expected_pf=pf_v if pf_v != "Missing" or True else "", expected_esic=esic_v, note=note))
# members present in ECR but not in the wagesheet (should land in the 'not in wagesheet' tab)
for k in range(3):
    pf_rows.append([len(pf_rows) + 1, uan(), f"EXTRA PERSON {k}", 15000, 15000, 15000, 15000, 1800, 1250, 550, 0, 0])
    esic_rows.append([len(esic_rows) + 1, ip(), f"EXTRA ESIC PERSON {k}", 26, 15000, 113, 488, "", ""])
random.shuffle(pf_rows); random.shuffle(esic_rows)
for n, r in enumerate(pf_rows, 1): r[0] = n
for n, r in enumerate(esic_rows, 1): r[0] = n

ws = pd.DataFrame([{"Name": (p["name"] if random.random() < .8 else p["name"].upper()), "UAN": p["uan"], "IP Number": p["ip"], "Agency": p["agency"], "State": p["state"],
                    "ESIC @ 0.75%": p["esic"], "PF @ 12%": p["pf"]} for p in people])
ws.to_excel(OUT / "wagesheet.xlsx", index=False)
table_pdf(OUT / "pf_ecr.pdf", "EPFO - Electronic Challan cum Return (ECR) - Wage Month Oct-2026",
          ["Sr.No", "UAN", "Member Name", "Gross Wages", "EPF Wages", "EPS Wages", "EDLI Wages", "EPF Contribution Remitted (EE)", "EPS Contribution Remitted", "Diff EPF & EPS (ER)", "NCP Days", "Refund of Advances"],
          pf_rows, [30, 70, 120, 48, 48, 48, 48, 75, 65, 60, 40, 55], 2)
table_pdf(OUT / "esic_ecr.pdf", "ESIC - Contribution History - Oct-2026",
          ["Sr.No", "IP Number", "IP Name", "No of Days", "Total Monthly Wages", "IP Contribution", "Employer Contribution", "Reason Code", "Last Working Day"],
          esic_rows, [30, 70, 150, 50, 70, 65, 70, 55, 70], 2)
pd.DataFrame(truth).to_csv(OUT / "ground_truth.csv", index=False)
print(f"wrote {N} employees; PF rows {len(pf_rows)}, ESIC rows {len(esic_rows)} -> {OUT}")
