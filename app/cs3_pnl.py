"""Case Study 3 - Daily FC P&L monitor & alert digest.

Workflow = five small agents run in order (each a plain function, so it is testable and cheap to run):

  IngestAgent   -> load + validate the day-level data, load target ranges            (load_data, load_targets)
  FinanceAgent  -> per-day P&L: each line % of revenue, CM1, CM1%, CM2, CM2%        (compute_pnl)
  WatchAgent    -> apply the business rules, colour-code each FC-day, build the log  (detect)
  InsightAgent  -> plain-language insight per anomaly (deterministic template;
                   optional local Ollama LLM may re-word it, with a number-preservation guard) (write_insight)
  NotifierAgent -> render per-FC digest, send via SMTP email / Slack webhook / dry-run outbox  (notify)

MemoryStore (SQLite) persists every FC-day so later runs know about streaks ("3rd day in a row") and trends.
Manual overrides (config/overrides.csv) mark an anomaly as acknowledged and mute its alert.
"""
import csv, html, json, os, re, smtplib, sqlite3, urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
from flask import Blueprint, render_template, request, redirect, url_for
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from . import jobs
from .jobs import UserError

bp = Blueprint("cs3", __name__)
ROOT = Path(__file__).resolve().parent.parent
LINES = ["Manpower Cost", "Packaging Cost", "Power & Fuel", "FC Rent", "Equipment Rentals", "Overheads"]
CM1_T, CM2_T, DEV = (65.0, 70.0), (15.0, 30.0), 3.0
RED_BELOW, BLUE_ABOVE = 12.0, 33.0
EMOJI = {"Red": "🔴", "Yellow": "🟡", "Green": "🟢", "Blue": "🔵"}
ACTIONS = {
    "Manpower Cost": "review headcount vs volume, overtime and attendance for the day",
    "Packaging Cost": "check packaging material usage/wastage and per-order consumption",
    "Power & Fuel": "check DG/electricity consumption and fuel purchases",
    "FC Rent": "verify rent accruals/invoice booked for the right period",
    "Equipment Rentals": "verify equipment rental invoices and idle equipment",
    "Overheads": "review overhead bookings for one-offs or misposted expenses"}
ALIASES = {"day": ["day", "date"], "fc": ["fc", "centre", "center", "warehouse", "location", "facility"], "Revenue": ["revenue", "sales"],
           "Manpower Cost": ["manpower"], "Packaging Cost": ["packaging"], "Power & Fuel": ["power", "fuel"], "FC Rent": ["rent"],
           "Equipment Rentals": ["equipment"], "Overheads": ["overhead"]}


# ============================================================================ IngestAgent
def _num(v):
    if v is None: return float("nan")
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try: return float(s) if s not in ("", "-", ".") else float("nan")
    except ValueError: return float("nan")


def _std_cols(cells):
    """Map header cells -> standard names. Each column goes to the standard name with the LONGEST matching keyword
    ('manpower' beats 'power', 'equipment' beats 'rent' in 'Equipment Rentals')."""
    best = {}
    for i, c in enumerate(cells):
        c = str(c).strip().lower()
        cand = [(len(k), std) for std, keys in ALIASES.items() for k in keys if k in c]
        if not cand: continue
        _, std = max(cand)
        if std not in best: best[std] = i
    return best


def load_data(path, default_fc):
    """-> (df[FC, Day, Revenue, <LINES>], warnings)"""
    p = Path(path); warns = []
    try:
        if p.suffix.lower() in (".csv", ".txt"): sheets = {default_fc: pd.read_csv(p, header=None, dtype=object)}
        else:
            xl = pd.ExcelFile(p); sheets = {n: xl.parse(n, header=None, dtype=object) for n in xl.sheet_names}
    except Exception:
        raise UserError("Could not open the data file. Upload a valid .xlsx/.xls/.csv with columns Day, Revenue, Manpower Cost, Packaging Cost, Power & Fuel, FC Rent, Equipment Rentals, Overheads.")
    frames = []
    for sname, raw in sheets.items():
        for hi in range(min(25, len(raw))):
            m = _std_cols(raw.iloc[hi].tolist())
            if "day" in m and "Revenue" in m and all(l in m for l in LINES):
                body = raw.iloc[hi + 1:]
                df = pd.DataFrame({"Day": body[m["day"]], "Revenue": body[m["Revenue"]].map(_num), **{l: body[m[l]].map(_num) for l in LINES}})
                df["FC"] = body[m["fc"]].astype(str).str.strip() if "fc" in m else (sname if len(sheets) > 1 and sname != "Sheet1" else default_fc)
                frames.append(df); break
    if not frames:
        missing = "Day, Revenue, " + ", ".join(LINES)
        raise UserError(f"Could not find a header row with all required columns ({missing}). Check the file/tab.")
    df = pd.concat(frames, ignore_index=True)
    df["Day"] = pd.to_datetime(df["Day"], errors="coerce", dayfirst=True)
    bad = df[df["Day"].isna() | df["Revenue"].isna()]
    if len(bad): warns.append(f"{len(bad)} row(s) skipped: unreadable date or revenue.")
    df = df.drop(bad.index)
    z = df[df["Revenue"] <= 0]
    if len(z): warns.append(f"{len(z)} row(s) skipped: revenue is zero/negative so percentages are undefined ({', '.join(sorted(set(z['FC'])))}).")
    df = df[df["Revenue"] > 0]
    nan_cost = df[df[LINES].isna().any(axis=1)]
    if len(nan_cost): warns.append(f"{len(nan_cost)} row(s) have blank cost cells; blanks treated as 0 (may hide under-reported costs).")
    df[LINES] = df[LINES].fillna(0)
    if df.empty: raise UserError("The file has no usable rows after validation.")
    dup = df.duplicated(["FC", "Day"], keep="last")
    if dup.any(): warns.append(f"{int(dup.sum())} duplicate FC/day row(s): kept the last one."); df = df[~dup]
    return df.sort_values(["FC", "Day"]).reset_index(drop=True), warns


def _range_from(v):
    if isinstance(v, str):
        n = re.findall(r"\d+(?:\.\d+)?", v)
        if len(n) >= 2: return float(n[0]), float(n[1])
    return None


def load_targets(sources):
    """sources: list of paths. Finds line-item target ranges (min,max %) wherever they appear; falls back to config/targets.csv."""
    found, note = {}, ""
    for src in sources:
        if not src: continue
        try:
            xl = pd.ExcelFile(src) if str(src).lower().endswith(("xlsx", "xls", "xlsm")) else None
            sheets = [xl.parse(n, header=None, dtype=object) for n in xl.sheet_names] if xl else [pd.read_csv(src, header=None, dtype=object)]
        except Exception:
            continue
        for raw in sheets:
            vals = raw.values.tolist()
            for r in vals:
                for ci, cell in enumerate(r):
                    if not isinstance(cell, str): continue
                    for line in LINES:
                        key = line.lower().split()[0]
                        if line in found or cell.strip().lower() not in (line.lower(), key, key + " cost"): continue
                        rest = r[ci + 1:]
                        rng = next((_range_from(x) for x in rest if _range_from(x)), None)
                        nums = [_num(x) for x in rest if isinstance(x, (int, float, str)) and not pd.isna(_num(x))]
                        if not rng and len(nums) >= 2 and nums[0] <= nums[1]: rng = (nums[0], nums[1])
                        if rng:
                            lo, hi = rng
                            if hi <= 1.0: lo, hi = lo * 100, hi * 100
                            found[line] = (lo, hi)
    missing = [l for l in LINES if l not in found]
    if missing:
        defaults = {r["Line Item"]: (float(r["Min %"]), float(r["Max %"])) for r in csv.DictReader(open(ROOT / "config" / "targets.csv"))}
        for l in missing: found[l] = defaults[l]
        note = ("No target ranges recognised in the uploaded files; used config/targets.csv defaults." if len(missing) == len(LINES)
                else f"Target ranges for {', '.join(missing)} not found in upload; used config/targets.csv defaults for those.")
    return found, note


# ============================================================================ FinanceAgent
def compute_pnl(df):
    p = df.copy()
    for l in LINES: p[l + " %"] = (p[l] / p["Revenue"] * 100).round(2)   # 2dp: avoids 9.0000001 > 9 false breaches from rounded rupee costs
    p["CM1"] = p["Revenue"] - p["Manpower Cost"] - p["Packaging Cost"]
    p["CM2"] = p["CM1"] - p[["Power & Fuel", "FC Rent", "Equipment Rentals", "Overheads"]].sum(axis=1)
    p["CM1 %"] = (p["CM1"] / p["Revenue"] * 100).round(2)
    p["CM2 %"] = (p["CM2"] / p["Revenue"] * 100).round(2)
    p["Colour"] = p["CM2 %"].map(colour)
    return p


def colour(cm2):
    if cm2 < RED_BELOW: return "Red"
    if cm2 < CM2_T[0]: return "Yellow"
    if cm2 <= CM2_T[1]: return "Green"
    if cm2 <= BLUE_ABOVE: return "Yellow"
    return "Blue"


# ============================================================================ WatchAgent
def detect(pnl, targets, overrides):
    log = []
    for _, r in pnl.iterrows():
        d = r["Day"].strftime("%Y-%m-%d"); items = []
        for l in LINES:
            lo, hi = targets[l]; v = r[l + " %"]
            if v > hi: items.append((l, v, f"{lo:g}–{hi:g}%", "Above max", v - hi))
            elif v < lo: items.append((l, v, f"{lo:g}–{hi:g}%", "Below min", lo - v))
        for nm, v, (lo, hi) in (("CM1", r["CM1 %"], CM1_T), ("CM2", r["CM2 %"], CM2_T)):
            if v < lo - DEV: items.append((nm, v, f"{lo:g}–{hi:g}% (±{DEV:g}pp)", "Below tolerance" + (" (RED)" if nm == "CM2" and v < RED_BELOW else ""), lo - DEV - v))
            elif v > hi + DEV: items.append((nm, v, f"{lo:g}–{hi:g}% (±{DEV:g}pp)", "Above tolerance" + (" (BLUE - suspicious)" if nm == "CM2" and v > BLUE_ABOVE else ""), v - hi - DEV))
        for l, v, rng, st, dev in items:
            ov = overrides.get((r["FC"], d, l)) or overrides.get((r["FC"], d, "*"))
            log.append(dict(Date=d, FC=r["FC"], **{"Line Item": l, "% of Revenue": round(v, 2), "Target Range": rng, "Status": st},
                            _dev=dev, Acknowledged=ov or ""))
    return pd.DataFrame(log, columns=["Date", "FC", "Line Item", "% of Revenue", "Target Range", "Status", "_dev", "Acknowledged"])


# ============================================================================ MemoryStore
class Memory:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS daily(fc TEXT, day TEXT, cm1 REAL, cm2 REAL, colour TEXT, breaches TEXT, PRIMARY KEY(fc, day))")

    def history(self):
        h = {}
        for fc, day, cm2, col, br in self.db.execute("SELECT fc, day, cm2, colour, breaches FROM daily"):
            h.setdefault(fc, {})[day] = dict(cm2=cm2, colour=col, breaches=set(json.loads(br)))
        return h

    def save(self, pnl, log):
        br = log.groupby(["FC", "Date"])["Line Item"].apply(list).to_dict() if len(log) else {}
        for _, r in pnl.iterrows():
            d = r["Day"].strftime("%Y-%m-%d")
            self.db.execute("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?)",
                            (r["FC"], d, r["CM1 %"], r["CM2 %"], r["Colour"], json.dumps(br.get((r["FC"], d), []))))
        self.db.commit()


def streak(hist, fc, day, item):
    days = sorted(hist.get(fc, {})); n = 0
    for d in reversed([x for x in days if x <= day]):
        if item in hist[fc][d]["breaches"]: n += 1
        else: break
    return n


def trend(hist, fc, day):
    days = [d for d in sorted(hist.get(fc, {})) if d <= day]
    if len(days) < 4: return ""
    last = [hist[fc][d]["cm2"] for d in days[-7:]]; prev = [hist[fc][d]["cm2"] for d in days[-14:-7]]
    t = f"7-day avg CM2 {sum(last)/len(last):.1f}%"
    if prev: t += f" ({sum(last)/len(last) - sum(prev)/len(prev):+.1f}pp vs previous week)"
    return t


# ============================================================================ InsightAgent
def write_insight(row, log_day, targets, hist):
    d = row["Day"]; ds = d.strftime("%Y-%m-%d"); fc = row["FC"]
    cm2, cm1, col = row["CM2 %"], row["CM1 %"], row["Colour"]
    head = f"On {d.strftime('%d %b %Y')}, {fc}: CM2 = {cm2:.1f}%"
    if cm2 < RED_BELOW: head += f" (RED - {RED_BELOW - cm2:.1f} pp below the {RED_BELOW:g}% red line; target {CM2_T[0]:g}–{CM2_T[1]:g}%)"
    elif cm2 < CM2_T[0]: head += f" (YELLOW - {CM2_T[0] - cm2:.1f} pp below target {CM2_T[0]:g}%)"
    elif cm2 > BLUE_ABOVE: head += f" (BLUE - {cm2 - BLUE_ABOVE:.1f} pp above {BLUE_ABOVE:g}%: suspiciously high)"
    elif cm2 > CM2_T[1]: head += f" (YELLOW - above target max {CM2_T[1]:g}%)"
    else: head += " (within target)"
    head += f". CM1 = {cm1:.1f}%" + ("" if CM1_T[0] - DEV <= cm1 <= CM1_T[1] + DEV else f" (outside {CM1_T[0]:g}–{CM1_T[1]:g}% ±{DEV:g}pp)") + "."
    rev = row["Revenue"]
    above = [(l, row[l + " %"], targets[l][1], row[l + " %"] - targets[l][1]) for l in LINES if row[l + " %"] > targets[l][1]]
    below = [(l, row[l + " %"], targets[l][0], targets[l][0] - row[l + " %"]) for l in LINES if row[l + " %"] < targets[l][0]]
    above.sort(key=lambda x: -x[3]); below.sort(key=lambda x: -x[3])
    parts, actions = [], []
    if above:
        parts.append("Over limit (largest impact first): " + "; ".join(
            f"{l} = {v:.1f}% (max {m:g}%, +{x:.1f} pp ≈ ₹{x / 100 * rev:,.0f})" + (f" [{streak(hist, fc, ds, l)} days in a row]" if streak(hist, fc, ds, l) > 1 else "") for l, v, m, x in above) + ".")
    if below:
        parts.append("Under minimum: " + "; ".join(f"{l} = {v:.1f}% (min {m:g}%, {x:.1f} pp short ≈ ₹{x / 100 * rev:,.0f})" for l, v, m, x in below) + ".")
    if not above and cm2 < CM2_T[0]:  # margin low but nothing individually breached: rank lines by distance above their range midpoint
        mids = sorted(((l, row[l + " %"] - sum(targets[l]) / 2) for l in LINES), key=lambda x: -x[1])[:3]
        parts.append("No single line is over its limit; biggest contributors vs mid-range: " + ", ".join(f"{l} (+{x:.1f} pp)" for l, x in mids if x > 0) + ".")
    if col == "Red":
        actions.append("immediate cost audit" + (f" of {', '.join(l for l, *_ in above)}" if above else "") + "; escalate to the FC head today")
    elif col == "Blue":
        actions.append("possible under-reported costs - verify " + (", ".join(l for l, *_ in below) if below else "all cost bookings") + " (invoices/accruals) before trusting this margin")
    elif above or below:
        for l, *_ in (above + below)[:2]: actions.append(f"{l}: {ACTIONS[l]}")
    elif col == "Yellow": actions.append("Monitor closely; no line is individually out of range")
    t = trend(hist, fc, ds)
    text = " ".join([head] + parts + ([t + "."] if t else []) + (["Recommended action: " + "; ".join(actions) + "."] if actions else []))
    if log_day is not None and (log_day["Acknowledged"] != "").any():
        text += " [Acknowledged by manager: " + "; ".join(sorted(set(log_day[log_day["Acknowledged"] != ""]["Acknowledged"]))) + "]"
    return llm_polish(text)


def llm_polish(text):
    """Optional: local Ollama model rewrites for readability. Falls back to the template if unavailable or if any number is lost."""
    model = os.environ.get("OLLAMA_MODEL")
    if not model: return text
    try:
        body = json.dumps({"model": model, "stream": False, "prompt": "Rewrite for a busy operations manager in 3-4 short plain sentences. Keep EVERY number, date and name exactly as is. Do not add facts.\n\n" + text}).encode()
        out = json.loads(urllib.request.urlopen(urllib.request.Request("http://localhost:11434/api/generate", body, {"Content-Type": "application/json"}), timeout=25).read())["response"].strip()
        if all(n in out for n in re.findall(r"\d+(?:\.\d+)?", text)): return out
    except Exception:
        pass
    return text


# ============================================================================ NotifierAgent
def digest_html(fc, day, row, insights, rows_fc, targets, manager):
    col = row["Colour"]; bg = {"Red": "#f8d7da", "Yellow": "#fff3cd", "Green": "#d4edda", "Blue": "#cfe2ff"}[col]
    lines = "".join(
        f"<tr><td>{html.escape(l)}</td><td align=right>{row[l + ' %']:.1f}%</td><td>{targets[l][0]:g}–{targets[l][1]:g}%</td>"
        f"<td>{'⚠ above' if row[l + ' %'] > targets[l][1] else '⚠ below' if row[l + ' %'] < targets[l][0] else 'ok'}</td></tr>" for l in LINES)
    ins = "".join(f"<li>{html.escape(t)}</li>" for t in insights) or "<li>No anomalies today. All lines within target.</li>"
    spark = " ".join(EMOJI[c] for c in rows_fc["Colour"].tail(7))
    return f"""<html><body style="font-family:Arial,sans-serif;max-width:680px">
<h2>{EMOJI[col]} {html.escape(fc)} — {day.strftime('%d %b %Y')}: {col}</h2><p>Hi {html.escape(manager or 'team')},</p>
<div style="background:{bg};padding:10px;border-radius:6px"><b>CM2 {row['CM2 %']:.1f}%</b> (target {CM2_T[0]:g}–{CM2_T[1]:g}%) &nbsp;|&nbsp; <b>CM1 {row['CM1 %']:.1f}%</b> (target {CM1_T[0]:g}–{CM1_T[1]:g}%) &nbsp;|&nbsp; Revenue ₹{row['Revenue']:,.0f}</div>
<h3>What needs attention</h3><ul>{ins}</ul>
<h3>Cost lines (% of revenue)</h3><table border=1 cellpadding=4 cellspacing=0 style="border-collapse:collapse"><tr><th>Line</th><th>Today</th><th>Target</th><th>Status</th></tr>{lines}</table>
<p>Last 7 days: {spark}</p><p style="color:#888;font-size:12px">Automated by the Prozo FC P&amp;L monitor.</p></body></html>"""


def digest_text(fc, day, row, insights):
    return f"{EMOJI[row['Colour']]} {fc} {day.strftime('%d %b')}: {row['Colour'].upper()} | CM2 {row['CM2 %']:.1f}% | CM1 {row['CM1 %']:.1f}%\n" + "\n".join("• " + t for t in insights)


def deliver(fc, subject, html_body, text_body, to, slack, send_real, outbox):
    """returns (channel, result). Dry-run writes the .eml to disk. Real sends use SMTP env vars / Slack webhook."""
    msg = EmailMessage(); msg["Subject"] = subject; msg["From"] = os.environ.get("SMTP_FROM", "pnl-monitor@localhost"); msg["To"] = to or "unassigned@example.com"
    msg.set_content(text_body); msg.add_alternative(html_body, subtype="html")
    res = []
    if send_real and os.environ.get("SMTP_HOST") and to:
        try:
            with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587)), timeout=20) as s:
                s.starttls()
                if os.environ.get("SMTP_USER"): s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
                s.send_message(msg)
            res.append(("email", f"sent to {to}"))
        except Exception as e:
            res.append(("email", f"FAILED: {e}"))
    else:
        why = "no recipient" if not to else "dry run" if not send_real else "SMTP_HOST not set"
        (outbox / f"{re.sub(r'[^A-Za-z0-9]+', '_', fc)}.eml").write_bytes(bytes(msg))
        res.append(("email", f"not sent ({why}); saved as .eml"))
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if send_real and hook:
        try:
            urllib.request.urlopen(urllib.request.Request(hook, json.dumps({"text": text_body}).encode(), {"Content-Type": "application/json"}), timeout=15)
            res.append(("slack", "posted"))
        except Exception as e:
            res.append(("slack", f"FAILED: {e}"))
    return res


def _mgr_map(path):
    m = {}
    try:
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            m[r.get("FC", "").strip().lower()] = r
    except Exception:
        pass
    return m


# ============================================================================ orchestration
def process(job_dir, progress, data_path, target_path, manager_path, fallback_email, send_real, report_date, default_fc):
    progress(0, 5, "IngestAgent: loading data")
    df, warns = load_data(data_path, default_fc)
    targets, tnote = load_targets([target_path, data_path])
    if tnote: warns.append(tnote)
    progress(1, 5, "FinanceAgent: computing P&L")
    pnl = compute_pnl(df)
    overrides = {}
    try:
        for r in csv.DictReader(open(ROOT / "config" / "overrides.csv", encoding="utf-8-sig")):
            overrides[(r["FC"].strip(), r["Date"].strip(), r["Line Item"].strip() or "*")] = r["Reason"].strip()
    except Exception: pass
    progress(2, 5, "WatchAgent: checking rules & memory")
    log = detect(pnl, targets, overrides)
    mem = Memory(jobs.DATA_DIR / "memory.db"); hist = mem.history()
    for _, r in pnl.iterrows():
        d = r["Day"].strftime("%Y-%m-%d")
        hist.setdefault(r["FC"], {})[d] = dict(cm2=r["CM2 %"], colour=r["Colour"], breaches=set(log[(log["FC"] == r["FC"]) & (log["Date"] == d)]["Line Item"]) if len(log) else set())
    mem.save(pnl, log)
    progress(3, 5, "InsightAgent: writing insights")
    flagged = set(zip(log["FC"], log["Date"])) | {(r["FC"], r["Day"].strftime("%Y-%m-%d")) for _, r in pnl.iterrows() if r["Colour"] != "Green"} if len(log) else \
              {(r["FC"], r["Day"].strftime("%Y-%m-%d")) for _, r in pnl.iterrows() if r["Colour"] != "Green"}
    insights = {}
    for _, r in pnl.iterrows():
        k = (r["FC"], r["Day"].strftime("%Y-%m-%d"))
        if k in flagged:
            ld = log[(log["FC"] == k[0]) & (log["Date"] == k[1])] if len(log) else None
            insights[k] = write_insight(r, ld, targets, hist)
    progress(4, 5, "NotifierAgent: building digests")
    rd = pd.to_datetime(report_date, errors="coerce") if report_date else pnl["Day"].max()
    if pd.isna(rd): rd = pnl["Day"].max()
    mgrs = _mgr_map(manager_path or ROOT / "config" / "managers.csv")
    outbox = job_dir / "out" / "outbox"; outbox.mkdir(exist_ok=True)
    dlog, dfiles = [], []
    for fc in sorted(pnl["FC"].unique()):
        sub = pnl[(pnl["FC"] == fc) & (pnl["Day"] <= rd)]
        if sub.empty or sub["Day"].max() != rd:
            dlog.append([fc, "", "", "no data for " + rd.strftime("%d %b %Y"), ""]); continue
        row = sub.iloc[-1]; k = (fc, rd.strftime("%Y-%m-%d"))
        ins = [insights[k]] if k in insights else []
        mg = mgrs.get(fc.lower(), {})
        to = (mg.get("Email") or fallback_email or "").strip()
        h = digest_html(fc, rd, row, ins, sub, targets, mg.get("Manager"))
        fn = f"digest_{re.sub(r'[^A-Za-z0-9]+', '_', fc)}_{rd.strftime('%Y%m%d')}.html"
        (job_dir / "out" / fn).write_text(h, encoding="utf-8"); dfiles.append(fn)
        for ch, rs in deliver(fc, f"{EMOJI[row['Colour']]} {fc} P&L {rd.strftime('%d %b')}: {row['Colour']}", h, digest_text(fc, rd, row, ins), to, mg.get("Slack"), send_real, outbox):
            dlog.append([fc, to or "(none)", ch, rs, {"v": row["Colour"], "cls": "c-" + row["Colour"]}])
    # ---- outputs
    out = job_dir / "out"
    show = pnl[["FC", "Day", "Revenue"] + LINES + [l + " %" for l in LINES] + ["CM1", "CM1 %", "CM2", "CM2 %", "Colour"]].copy()
    show["Day"] = show["Day"].dt.strftime("%Y-%m-%d")
    log_out = log.drop(columns=["_dev"]) if len(log) else log
    log_out.to_csv(out / "anomalies_log.csv", index=False)
    ins_df = pd.DataFrame([{"Date": k[1], "FC": k[0], "Insight": v} for k, v in sorted(insights.items(), key=lambda x: (x[0][1], x[0][0]))])
    dates = sorted(pnl["Day"].dt.strftime("%Y-%m-%d").unique()); fcs = sorted(pnl["FC"].unique())
    dash = pd.DataFrame({d: [pnl[(pnl["FC"] == f) & (pnl["Day"].dt.strftime("%Y-%m-%d") == d)]["Colour"].squeeze() if len(pnl[(pnl["FC"] == f) & (pnl["Day"].dt.strftime("%Y-%m-%d") == d)]) else "" for f in fcs] for d in dates}, index=fcs)
    dash.index.name = "FC"
    with pd.ExcelWriter(out / "pnl_report.xlsx", engine="openpyxl") as xw:
        dash.reset_index().to_excel(xw, sheet_name="Dashboard", index=False)
        show.to_excel(xw, sheet_name="Daily P&L", index=False)
        log_out.to_excel(xw, sheet_name="Anomalies", index=False)
        ins_df.to_excel(xw, sheet_name="Insights", index=False)
        pd.DataFrame([{"Line Item": l, "Min %": targets[l][0], "Max %": targets[l][1]} for l in LINES] +
                     [{"Line Item": "CM1 (±3pp tolerance)", "Min %": CM1_T[0], "Max %": CM1_T[1]}, {"Line Item": "CM2 (±3pp tolerance)", "Min %": CM2_T[0], "Max %": CM2_T[1]}]).to_excel(xw, sheet_name="Targets used", index=False)
        fills = {"Red": "F8CBAD", "Yellow": "FFEB9C", "Green": "C6EFCE", "Blue": "BDD7EE"}
        for ws in xw.book.worksheets:
            for c in ws[1]: c.font = Font(bold=True)
            for ci in range(1, ws.max_column + 1): ws.column_dimensions[get_column_letter(ci)].width = 120 if ws.title == "Insights" and ci == 3 else 16
            ws.freeze_panes = "B2"
            for row_ in ws.iter_rows(min_row=2):
                for c in row_:
                    if c.value in fills: c.fill = PatternFill("solid", fgColor=fills[c.value])
    # ---- web view
    cell_rows = []
    for f in fcs:
        r = [f]
        for d in dates:
            m = pnl[(pnl["FC"] == f) & (pnl["Day"].dt.strftime("%Y-%m-%d") == d)]
            r.append({"v": f"{m.iloc[0]['CM2 %']:.1f}%", "cls": "c-" + m.iloc[0]["Colour"]} if len(m) else "")
        cell_rows.append(r)
    counts = pnl["Colour"].value_counts().to_dict()
    result = {
        "summary": [{"label": f"{c} days", "value": counts.get(c, 0), "cls": "c-" + c} for c in ("Red", "Yellow", "Green", "Blue")] +
                   [{"label": "Anomalies logged", "value": len(log)}, {"label": "FCs", "value": len(fcs)}],
        "warnings": warns,
        "tables": [
            {"title": "Dashboard: CM2 % per FC per day (colour = status)", "columns": ["FC"] + [d[5:] for d in dates], "rows": cell_rows},
            {"title": f"Digest delivery for {rd.strftime('%d %b %Y')}", "columns": ["FC", "To", "Channel", "Result", "Status"], "rows": dlog},
            {"title": "Anomaly log", "columns": ["Date", "FC", "Line Item", "% of Revenue", "Target Range", "Status", "Acknowledged"],
             "rows": [[r["Date"], r["FC"], r["Line Item"], r["% of Revenue"], r["Target Range"], r["Status"], r["Acknowledged"]] for _, r in log.iterrows()] if len(log) else []},
            {"title": "Insights", "columns": ["Date", "FC", "Insight"], "rows": [[r["Date"], r["FC"], r["Insight"]] for _, r in ins_df.iterrows()]}],
        "targets": {l: list(targets[l]) for l in LINES}}
    progress(5, 5, "Done")
    return {"files": ["pnl_report.xlsx", "anomalies_log.csv"] + dfiles, "result": result}


@bp.route("/cs3", methods=["GET", "POST"])
def cs3():
    if request.method == "GET": return render_template("case.html", case="cs3")
    use_sample = request.form.get("sample") == "1"
    f = request.files.get("data")
    if not use_sample and not (f and f.filename):
        return render_template("case.html", case="cs3", error="Please upload the daily P&L file (Excel/CSV), or use the sample data."), 400
    jid, d = jobs.new_job_dir("cs3", "Daily FC P&L monitor")
    if use_sample:
        import shutil; src = ROOT / "sample_data" / "cs3" / "Case_Study_3_sample.xlsx"; dp = d / "in" / src.name; shutil.copy(src, dp); stem = "FC-1"
    else:
        dp = d / "in" / ("data_" + Path(f.filename).name); f.save(dp); stem = Path(f.filename).stem
        if dp.stat().st_size == 0: return render_template("case.html", case="cs3", error="That file is empty (0 bytes)."), 400
    paths = {}
    for k in ("targets", "managers"):
        u = request.files.get(k)
        if u and u.filename: paths[k] = d / "in" / (k + "_" + Path(u.filename).name); u.save(paths[k])
    jobs.submit(jid, process, dp, paths.get("targets"), paths.get("managers"), request.form.get("fallback_email", "").strip(),
                request.form.get("send_real") == "1", "2026-05-27" if use_sample else request.form.get("report_date", ""), stem)
    return redirect(url_for("job_page", jid=jid))
