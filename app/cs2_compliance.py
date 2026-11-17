"""Case Study 2 - Wagesheet vs PF/ESIC ECR verification.

Approach (deterministic, no LLM needed -> free, fast, auditable):
  * PDFs are read with PyMuPDF as positioned words, regrouped into visual rows (works for 100s of pages, ~50 pages/s)
  * a row is a member record if it starts with a 12-digit UAN (PF) / 10- or 17-digit IP number (ESIC)
  * names that wrap onto a 2nd line are re-joined using x-position
  * amount columns are picked by position; sanity-checked against statutory rates (12% / 0.75%) and a warning is
    raised if the column mapping looks wrong, instead of silently trusting it
  * names are compared with a token-level fuzzy matcher (see name_match) with documented tolerance
"""
import math, re, unicodedata
from pathlib import Path

import pandas as pd
from flask import Blueprint, render_template, request, redirect, url_for
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from rapidfuzz import fuzz

from . import jobs
from .jobs import UserError

bp = Blueprint("cs2", __name__)

UAN_RX = re.compile(r"^\d{12}$")
IP_RX = re.compile(r"^(\d{10}|\d{17})$")
NUM_RX = re.compile(r"^-?\d[\d,]*(\.\d+)?$")
PF_EE_INDEX = 4   # after name: Gross, EPF wages, EPS wages, EDLI wages, [EPF contribution (EE)], EPS contr, ER diff, NCP, refund
DEFAULT_TOL = 1.0  # rupees: PF/ESIC are rounded to whole rupees, so +/-1 is rounding, not a genuine mismatch


# ============================================================================ name matching
TITLES = {"MR", "MRS", "MS", "MISS", "SHRI", "SHREE", "SMT", "SRI", "DR", "LATE"}
ALIASES = {"MD": "MOHAMMAD", "MOHD": "MOHAMMAD", "MOHAMMED": "MOHAMMAD", "MUHAMMAD": "MOHAMMAD", "MOHAMED": "MOHAMMAD", "MOHAMAD": "MOHAMMAD"}
PHON = [("EE", "I"), ("OO", "U"), ("PH", "F"), ("TH", "T"), ("DH", "D"), ("BH", "B"), ("GH", "G"), ("KH", "K"), ("SH", "S"), ("W", "V")]


def _phon(t):
    for a, b in PHON: t = t.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", t)   # collapse doubled letters


def name_tokens(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().upper()
    s = re.sub(r"[^A-Z ]", " ", s.replace(".", " "))
    toks = [ALIASES.get(t, t) for t in s.split() if t not in TITLES]
    return toks


def _tok_score(a, b):
    """similarity of two tokens, 0..1, plus a label"""
    if a == b: return 1.0, "exact"
    if len(a) == 1 or len(b) == 1: return (0.9, "initial") if a[0] == b[0] else (0.0, "")
    pa, pb = _phon(a), _phon(b)
    if pa == pb: return 0.97, "spelling"
    if max(len(a), len(b)) < 6 or min(len(a), len(b)) < 4: return 0.0, ""   # short names: exact/phonetic only (RAJ != RAM)
    r = max(fuzz.ratio(pa, pb), fuzz.ratio(a, b)) / 100
    return (r, "spelling") if r >= 0.85 else (0.0, "")


def name_match(a, b):
    """-> (is_match, score 0-100, note).

    Tolerated (NOT flagged): case/punctuation/titles, word order, spacing (SING HAL vs SINGHAL), phonetic spelling
    variants (SUNITA/SUNEETA, MOHD/MOHAMMED), 1-edit typos in names >=6 letters, initials (R KUMAR vs RAHUL KUMAR),
    and ONE omitted word (middle name / surname).
    Flagged: different given name/surname, e.g. RAJESH vs RAKESH, RAHUL KUMAR vs SURESH KUMAR.
    """
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb: return False, 0.0, "name missing"
    if ta == tb: return True, 100.0, ""
    notes = []
    if sorted(ta) == sorted(tb): return True, 98.0, "word order differs"
    ja, jb = "".join(ta), "".join(tb)
    if ja == jb: return True, 98.0, "spacing differs"
    if fuzz.ratio(_phon("".join(sorted(ta))), _phon("".join(sorted(tb)))) >= 93 and min(len(ja), len(jb)) >= 8:
        return True, 94.0, "minor spelling/spacing variation"
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # spacing / word-order variation combined with ONE omitted word (drop one token from either side, compare joined strings)
    for x, y in ((ta, tb), (tb, ta)):
        for drop in range(len(x)):
            cand = x[:drop] + x[drop + 1:]
            if "".join(cand) == "".join(y) or "".join(sorted(cand)) == "".join(sorted(y)):
                return True, 92.0, "one word missing, spacing/word order differs"
    free = list(range(len(long_))); scores = []; kinds = []
    for t in sorted(short, key=len, reverse=True):
        best = (0.0, None, "")
        for i in free:
            s, k = _tok_score(t, long_[i])
            if s > best[0]: best = (s, i, k)
        if best[1] is None: return False, round(100 * fuzz.token_sort_ratio(" ".join(ta), " ".join(tb)) / 100, 0), "different name"
        free.remove(best[1]); scores.append(best[0]); kinds.append(best[2])
    extra = len(free)
    full_hits = sum(1 for k in kinds if k in ("exact", "spelling"))
    if extra > 1 or (len(short) == 1 and extra == 1 and kinds[0] == "initial") or (full_hits == 0 and kinds.count("initial") < 2):
        return False, round(100 * sum(scores) / len(long_), 0), "different name"
    if "spelling" in kinds: notes.append("spelling variation")
    if "initial" in kinds: notes.append("initial vs full name")
    if extra: notes.append("one word missing/extra")
    return True, round(100 * sum(scores) / len(long_), 0), ", ".join(notes) or "word order differs"


# ============================================================================ PDF parsing
def _rows_from_page(page, ytol=3.0):
    words = page.get_text("words")              # x0,y0,x1,y1,text,...
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows, cur, cy = [], [], None
    for w in words:
        yc = (w[1] + w[3]) / 2
        if cy is None or abs(yc - cy) <= ytol: cur.append(w); cy = yc if cy is None else (cy + yc) / 2
        else: rows.append(sorted(cur, key=lambda z: z[0])); cur, cy = [w], yc
    if cur: rows.append(sorted(cur, key=lambda z: z[0]))
    return rows


def _num(t):
    return float(t.replace(",", ""))


def parse_ecr(path, kind, progress=None):
    """kind 'pf' or 'esic'. Returns (records: dict id -> {name, amount, wages, count, pages}, info)."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    try:
        doc = fitz.open(str(path))
    except Exception:
        raise UserError(f"'{Path(path).name}' is not a readable PDF (corrupt or not a PDF).")
    if doc.needs_pass: raise UserError(f"'{Path(path).name}' is password-protected.")
    if doc.page_count == 0: raise UserError(f"'{Path(path).name}' has no pages.")
    id_rx = UAN_RX if kind == "pf" else IP_RX
    recs, order, info = {}, [], dict(pages=doc.page_count, rows=0, consistent=0, checkable=0, warnings=[], text_chars=0)
    for pno, page in enumerate(doc):
        if progress and pno % 5 == 0: progress(pno, doc.page_count, f"Reading {kind.upper()} ECR page {pno + 1} of {doc.page_count}")
        prev = None
        for row in _rows_from_page(page):
            toks = [w[4] for w in row]; info["text_chars"] += sum(len(t) for t in toks)
            idx = next((i for i, t in enumerate(toks) if id_rx.match(t) and i <= 2), None)
            if idx is None:
                # wrapped name continuation: alpha-only row aligned under the previous row's name
                if prev and all(re.fullmatch(r"[A-Za-z.\-']+", t) for t in toks) and abs(row[0][0] - prev["x0"]) < 25 and 0 < row[0][1] - prev["y1"] < 18:
                    prev["name"] += " " + " ".join(toks); prev["y1"] = row[0][3]
                continue
            j = idx + 1; name_w = []
            while j < len(toks) and not NUM_RX.match(toks[j]): name_w.append(row[j]); j += 1
            nums = [_num(t) for t in toks[j:] if NUM_RX.match(t)]
            if not name_w or not nums: continue
            rec = dict(id=toks[idx], name=" ".join(w[4] for w in name_w), x0=name_w[0][0], y1=row[0][3], page=pno + 1)
            if kind == "pf":
                if len(nums) <= PF_EE_INDEX: info["warnings"].append(f"page {pno+1}: row for UAN {rec['id']} has too few numeric columns"); continue
                rec["amount"], rec["wages"] = nums[PF_EE_INDEX], nums[1]
                info["checkable"] += 1; info["consistent"] += abs(rec["amount"] - round(0.12 * rec["wages"])) <= 2
            else:
                wi = ci = None
                for a in range(len(nums)):                       # find (wages, 0.75% contribution) pair
                    for b in range(a + 1, len(nums)):
                        if nums[a] > 0 and abs(nums[b] - math.ceil(0.0075 * nums[a] - 1e-9)) <= 1: wi, ci = a, b; break
                    if ci is not None: break
                if ci is None:
                    if len(nums) < 3: info["warnings"].append(f"page {pno+1}: row for IP {rec['id']} has too few numeric columns"); continue
                    wi, ci = 1, 2; info["checkable"] += 1
                else: info["checkable"] += 1; info["consistent"] += 1
                rec["amount"], rec["wages"] = nums[ci], nums[wi]
            info["rows"] += 1; prev = rec
            order.append(rec)
    doc.close()
    if info["text_chars"] < 50:
        raise UserError(f"No text found in '{Path(path).name}'. It looks like a scanned/image PDF - OCR for ECR PDFs is not supported; download the original PDF from the portal.")
    for r in order:
        if r["id"] in recs:
            e = recs[r["id"]]; e["amount"] += r["amount"]; e["count"] += 1
        else:
            recs[r["id"]] = dict(name=r["name"].strip(), amount=r["amount"], wages=r["wages"], count=1, page=r["page"])
    if not recs:
        raise UserError(f"Could not find any member rows in the {kind.upper()} ECR '{Path(path).name}'. Expected rows starting with a "
                        f"{'12-digit UAN' if kind == 'pf' else '10-digit IP number'}. Is this the right file in the right slot?")
    if info["checkable"] and info["consistent"] / info["checkable"] < 0.5:
        info["warnings"].append(f"Only {info['consistent']}/{info['checkable']} parsed rows satisfy the statutory rate "
                                f"({'12% of EPF wages' if kind == 'pf' else '0.75% of wages'}). The column mapping may be wrong for this PDF layout - check the parse preview.")
    info["duplicates"] = sum(1 for r in recs.values() if r["count"] > 1)
    return recs, info


# ============================================================================ wagesheet
def _digits(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return ""
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "-", "na", "n/a"): return ""
    if re.fullmatch(r"\d+\.0+", s): s = s.split(".")[0]
    elif re.fullmatch(r"\d(\.\d+)?[eE]\+?\d+", s):
        from decimal import Decimal
        s = str(int(Decimal(s)))
    return re.sub(r"\D", "", s)


def _amt(v):
    try:
        if v is None or str(v).strip() in ("", "-", "nan"): return 0.0
        return float(str(v).replace(",", "").replace("₹", "").strip())
    except ValueError:
        return float("nan")


def read_wagesheet(path):
    try:
        xl = pd.ExcelFile(path)
    except Exception:
        try:
            df = pd.read_csv(path, header=None, dtype=object); xl = None
        except Exception:
            raise UserError("The wagesheet could not be opened. Upload a valid .xlsx/.xls/.csv file.")
    sheets = {n: xl.parse(n, header=None, dtype=object) for n in xl.sheet_names} if xl else {"csv": df}
    for name, raw in sheets.items():
        for hi in range(min(20, len(raw))):
            cells = [str(c).strip().lower() for c in raw.iloc[hi].tolist()]
            if any("name" in c for c in cells) and any("uan" in c for c in cells):
                df = raw.iloc[hi + 1:].copy(); df.columns = cells
                def col(*keys, excl=()):
                    return next((c for c in cells if any(k in c for k in keys) and not any(e in c for e in excl)), None)
                m = dict(name=col("name"), uan=col("uan"), ip=col("ip number", "ip no", "ip_number", "esic no", "ip"),
                         agency=col("agency", "vendor"), state=col("state"), esic=col("esic"), pf=col("pf"))
                if not (m["name"] and m["uan"] and m["pf"]):
                    continue
                out = pd.DataFrame({
                    "Name": df[m["name"]].fillna("").astype(str).str.strip(),
                    "UAN": df[m["uan"]].map(_digits),
                    "IP Number": df[m["ip"]].map(_digits) if m["ip"] else "",
                    "Agency": df[m["agency"]].fillna("") if m["agency"] else "",
                    "State": df[m["state"]].fillna("") if m["state"] else "",
                    "ESIC_amt": df[m["esic"]].map(_amt) if m["esic"] else 0.0,
                    "PF_amt": df[m["pf"]].map(_amt)})
                out = out[(out["Name"] != "") | (out["UAN"] != "")].reset_index(drop=True)
                if out.empty: raise UserError("The wagesheet has a header row but no employee rows.")
                return out
    raise UserError("Could not find the wagesheet header. Expected columns: Name, UAN, IP Number, Agency, State, ESIC @ 0.75%, PF @ 12%.")


# ============================================================================ verification
def _verify(scheme, sid, name, sheet_amt, recs, tol, label):
    """scheme 'PF' or 'ESIC' -> dict of verdict fields"""
    out = dict(status="", reason="", ecr_name="", ecr_amt=None, diff=None, name_score=None)
    idname = "UAN" if scheme == "PF" else "IP Number"
    if not sid:
        if not sheet_amt or math.isnan(sheet_amt) or sheet_amt == 0:
            out.update(status="Not applicable", reason=f"No {idname} and no {scheme} deduction in wagesheet (not covered).")
        else:
            out.update(status="Missing", reason=f"{scheme} of {sheet_amt:,.0f} deducted but {idname} is blank in wagesheet.")
        return out
    if math.isnan(sheet_amt):
        out.update(status="Missing", reason=f"wagesheet {scheme} amount is not a number"); return out
    rec = recs.get(sid)
    if rec is None:
        out.update(status="Missing", reason=f"{idname} {sid} not present in {scheme} ECR."); return out
    ok, score, note = name_match(name, rec["name"])
    diff = round(rec["amount"] - sheet_amt, 2)
    out.update(ecr_name=rec["name"], ecr_amt=rec["amount"], diff=diff, name_score=score)
    amt_txt = f"wagesheet {sheet_amt:,.0f} vs ECR {label} {rec['amount']:,.0f}"
    dup = f" (ID appears {rec['count']}x in ECR; amounts summed)" if rec["count"] > 1 else ""
    if not ok:
        out.update(status="Name mismatch", reason=f"{idname} found but ECR name '{rec['name']}' differs from wagesheet '{name}' (score {score:.0f}); "
                   + (f"amount also differs: {amt_txt}, diff {diff:+,.0f}" if abs(diff) > tol else "amount OK") + dup)
    elif abs(diff) > tol:
        out.update(status="Amount mismatch", reason=f"{amt_txt}; difference {diff:+,.0f}" + (f". Name tolerated: {note}" if note else "") + dup)
    else:
        out.update(status="Matched", reason=(f"name OK ({note}); " if note else "name OK; ") + f"{amt_txt}" + (f" (rounding diff {diff:+.0f})" if diff else "") + dup)
    return out


STATUSES = ["Matched", "Name mismatch", "Amount mismatch", "Missing", "Not applicable"]
FILL = {"Matched": "C6EFCE", "Name mismatch": "FFEB9C", "Amount mismatch": "F8CBAD", "Missing": "FFC7CE", "Not applicable": "E7E6E6"}


def run_verification(wage, pf, esic, tol):
    rows = []
    for _, r in wage.iterrows():
        row = {"Name": r["Name"], "UAN": r["UAN"], "IP Number": r["IP Number"], "Agency": r["Agency"], "State": r["State"]}
        if pf is not None:
            v = _verify("PF", r["UAN"], r["Name"], r["PF_amt"], pf, tol, "EE share")
            row.update({"PF Status": v["status"], "PF Reason": v["reason"], "PF Wagesheet": r["PF_amt"], "PF ECR (EE share)": v["ecr_amt"],
                        "PF Diff": v["diff"], "PF ECR Name": v["ecr_name"], "PF Name Score": v["name_score"]})
        if esic is not None:
            v = _verify("ESIC", r["IP Number"], r["Name"], r["ESIC_amt"], esic, tol, "IP contribution")
            row.update({"ESIC Status": v["status"], "ESIC Reason": v["reason"], "ESIC Wagesheet": r["ESIC_amt"], "ESIC ECR (IP contrib.)": v["ecr_amt"],
                        "ESIC Diff": v["diff"], "ESIC ECR Name": v["ecr_name"], "ESIC Name Score": v["name_score"]})
        rows.append(row)
    res = pd.DataFrame(rows)
    # duplicates inside the wagesheet itself
    dup_u = res[res["UAN"] != ""]["UAN"].duplicated(keep=False)
    res["Wagesheet Note"] = ["Duplicate UAN in wagesheet" if d else "" for d in dup_u.reindex(res.index, fill_value=False)]
    return res


def _unlisted(recs, ids):
    return pd.DataFrame([{"ID": k, "ECR Name": v["name"], "ECR Amount": v["amount"]} for k, v in recs.items() if k not in ids])


def write_workbook(path, res, summary_rows, extras, assumptions, parse_info):
    from openpyxl import Workbook
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(summary_rows).to_excel(xw, sheet_name="Summary", index=False, startrow=1)
        res.to_excel(xw, sheet_name="Per-employee results", index=False)
        sc = [c for c in res.columns if c.endswith("Status")]
        exc = res[res[sc].isin(["Name mismatch", "Amount mismatch", "Missing"]).any(axis=1)] if sc else res.iloc[0:0]
        exc.to_excel(xw, sheet_name="Exceptions only", index=False)
        for nm, df in extras.items(): df.to_excel(xw, sheet_name=nm, index=False)
        pd.DataFrame({"Assumption / parse note": assumptions + parse_info}).to_excel(xw, sheet_name="Notes & assumptions", index=False)
        wb = xw.book
        ws = wb["Summary"]; ws["A1"] = "Verdict counts (values are a snapshot of this run)"; ws["A1"].font = Font(bold=True, size=12)
        for name in ("Per-employee results", "Exceptions only"):
            ws = wb[name]
            heads = [c.value for c in ws[1]]
            for c in ws[1]: c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="305496")
            for ci, h in enumerate(heads, 1):
                ws.column_dimensions[get_column_letter(ci)].width = 70 if h and h.endswith("Reason") else 26 if h == "Name" or (h and "Name" in h) else 15
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    if heads[c.column - 1] and heads[c.column - 1].endswith("Status") and c.value in FILL:
                        c.fill = PatternFill("solid", fgColor=FILL[c.value])
                    if heads[c.column - 1] in ("UAN", "IP Number"): c.number_format = "@"
            ws.freeze_panes = "B2"; ws.auto_filter.ref = ws.dimensions
        for ws in wb.worksheets:
            for c in ws[1 if ws.title != "Summary" else 2]: c.font = Font(bold=True)
        wb["Notes & assumptions"].column_dimensions["A"].width = 150
        wb["Summary"].column_dimensions["A"].width = 24


def process(job_dir, progress, wage_path, pf_path, esic_path, tol):
    progress(0, 1, "Reading wagesheet")
    wage = read_wagesheet(wage_path)
    pf = esic = None; infos = {}
    if pf_path: pf, infos["PF"] = parse_ecr(pf_path, "pf", progress)
    if esic_path: esic, infos["ESIC"] = parse_ecr(esic_path, "esic", progress)
    progress(1, 1, "Matching employees")
    res = run_verification(wage, pf, esic, tol)
    summary = []
    for s in STATUSES:
        row = {"Verdict": s}
        if pf is not None: row["PF"] = int((res["PF Status"] == s).sum())
        if esic is not None: row["ESIC"] = int((res["ESIC Status"] == s).sum())
        summary.append(row)
    tot = {"Verdict": "Total employees"}
    if pf is not None: tot["PF"] = len(res)
    if esic is not None: tot["ESIC"] = len(res)
    summary.append(tot)
    extras = {}
    if pf is not None: extras["In PF ECR not in wagesheet"] = _unlisted(pf, set(wage["UAN"]))
    if esic is not None: extras["In ESIC ECR not in wagesheet"] = _unlisted(esic, set(wage["IP Number"]))
    assumptions = [
        f"Amount tolerance: +/-{tol:g} rupee (statutory amounts are rounded to whole rupees).",
        "PF: wagesheet 'PF @ 12%' is compared with the ECR EPF contribution remitted (EE share). ESIC: 'ESIC @ 0.75%' vs IP contribution.",
        "Name tolerance: ignores case, punctuation, titles, word order and spacing; accepts phonetic spelling variants (SUNITA/SUNEETA, MOHD/MOHAMMED), "
        "1-letter typos in names of 6+ letters (never in short names), initials (R KUMAR vs RAHUL KUMAR) and one omitted word. Different given name/surname (RAJESH vs RAKESH) is flagged.",
        "A name mismatch is reported even if the amount also differs (the amount difference is included in the reason).",
        "Employees with no UAN/IP number AND zero deduction are 'Not applicable' (e.g. wages above the ESIC ceiling), not 'Missing'.",
        "If an ID appears several times in an ECR the amounts are summed and the reason says so.",
    ]
    pinfo = []
    for k, i in infos.items():
        pinfo.append(f"{k} ECR: {i['pages']} pages, {i['rows']} member rows parsed, {i['duplicates']} IDs repeated. Rate sanity check: {i['consistent']}/{i['checkable']} rows consistent.")
        pinfo += [f"{k} WARNING: {w}" for w in i["warnings"][:20]]
    out = job_dir / "out" / "verification_report.xlsx"
    write_workbook(out, res, summary, extras, assumptions, pinfo)
    # web preview
    cols = ["Name", "UAN", "IP Number"] + [c for c in res.columns if c in ("PF Status", "PF Reason", "ESIC Status", "ESIC Reason")]
    bad = res[(res[[c for c in res.columns if c.endswith("Status")]].isin(["Name mismatch", "Amount mismatch", "Missing"])).any(axis=1)]

    def cell(c, v):
        if c.endswith("Status"): return {"v": v, "cls": "st-" + str(v).replace(" ", "_")}
        return "" if v is None or (isinstance(v, float) and math.isnan(v)) else v
    prev_rows = [[cell(c, r[c]) for c in cols] for _, r in bad.head(300).iterrows()]
    sm_cols = [c for c in summary[0].keys()]
    result = {
        "summary": [{"label": f"{k} {s['Verdict']}", "value": s[k], "cls": "st-" + s["Verdict"].replace(" ", "_")} for s in summary[:-1] for k in ("PF", "ESIC") if k in s] +
                   [{"label": "Employees", "value": len(res)}],
        "warnings": [w for i in infos.values() for w in i["warnings"][:5]],
        "tables": [{"title": f"Exceptions ({len(bad)} employees; first 300 shown - full list in the spreadsheet)", "columns": cols, "rows": prev_rows}],
        "notes": assumptions}
    progress(1, 1, "Done")
    return {"files": ["verification_report.xlsx"], "result": result}


@bp.route("/cs2", methods=["GET", "POST"])
def cs2():
    if request.method == "GET": return render_template("case.html", case="cs2")
    if request.form.get("sample") == "1":
        import shutil
        src = Path(__file__).resolve().parent.parent / "sample_data" / "cs2"
        jid, d = jobs.new_job_dir("cs2", "Wagesheet vs ECR verification (sample)")
        ps = {}
        for k, fn in (("w", "wagesheet.xlsx"), ("p", "pf_ecr.pdf"), ("e", "esic_ecr.pdf")):
            ps[k] = d / "in" / fn; shutil.copy(src / fn, ps[k])
        jobs.submit(jid, process, ps["w"], ps["p"], ps["e"], DEFAULT_TOL)
        return redirect(url_for("job_page", jid=jid))
    w, p, e = (request.files.get(k) for k in ("wagesheet", "pf_ecr", "esic_ecr"))
    def has(f): return f is not None and f.filename
    err = None
    if not has(w): err = "Please upload the wagesheet (Excel)."
    elif not (has(p) or has(e)): err = "Please upload at least one ECR PDF (PF and/or ESIC)."
    try: tol = float(request.form.get("tolerance") or DEFAULT_TOL)
    except ValueError: err = err or "Tolerance must be a number."; tol = DEFAULT_TOL
    if err: return render_template("case.html", case="cs2", error=err), 400
    jid, d = jobs.new_job_dir("cs2", "Wagesheet vs ECR verification")
    paths = {}
    for k, f in (("w", w), ("p", p), ("e", e)):
        if has(f):
            paths[k] = d / "in" / (k + "_" + Path(f.filename).name); f.save(paths[k])
            if paths[k].stat().st_size == 0:
                return render_template("case.html", case="cs2", error=f"'{f.filename}' is empty (0 bytes)."), 400
    jobs.submit(jid, process, paths["w"], paths.get("p"), paths.get("e"), tol)
    return redirect(url_for("job_page", jid=jid))
