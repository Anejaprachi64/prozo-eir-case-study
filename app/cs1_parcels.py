"""Case Study 1 - Parcel images -> structured shipment records (+ honest status flags).

Pipeline per image (all free / offline):
  1. decode image (corrupt -> IMAGE_CORRUPT)
  2. barcode decode with zxing-cpp  (Code128 etc. has a checksum => most trustworthy AWB source)
  3. OCR with RapidOCR (ONNX, models ship inside the pip wheel; no API key, no Tesseract binary)
  4. regex field extraction (AWB / weight / dimensions) anchored on label keywords
  5. rule-based quality flags: no parcel, multiple parcels, partial, blocked, unreadable
  6. batch-level checks: duplicate AWB

Design rule: an AWB is only written to the `awb` column if it is barcode-decoded, or OCR-read with
a label anchor AND high OCR confidence. Otherwise it goes to `notes` as a candidate and the row is flagged.
"""
import csv, re, shutil, time, zipfile
from pathlib import Path

import cv2
import numpy as np
from flask import Blueprint, render_template, request, redirect, url_for

from . import jobs
from .jobs import UserError

bp = Blueprint("cs1", __name__)

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
MAX_SIDE = 1600
BLUR_MIN = 40.0          # Laplacian variance below this = too blurry to trust
OCR_AWB_MIN_CONF = 0.90  # OCR-only AWBs need this line confidence
EDGE_MARGIN = 0.012      # text/barcode within 1.2% of the border counts as "touching the frame edge"

STATUS_HELP = {
    "OK": "Clean read. AWB verified.",
    "NO_PARCEL": "No label-like content found (empty frame, or parcel facing away).",
    "MULTIPLE_PARCELS": "More than one label/AWB in the frame - not assigning either.",
    "PARTIAL_PARCEL": "Label is cut off by the image edge.",
    "LABEL_BLOCKED": "Label present but covered (hand/object) - AWB not readable.",
    "LABEL_UNREADABLE": "Label present but blurry / low OCR confidence - AWB not trusted.",
    "DUPLICATE_AWB": "Same AWB appears on more than one image in this batch.",
    "IMAGE_CORRUPT": "File is not a readable image.",
}

_ocr = None


def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr


# ----------------------------------------------------------------------------- text parsing
ANCHOR = r"(?:AWB|A\.W\.B|WAY\s*BILL|TRACKING|TRK|DOCKET|CONSIGNMENT|\bLR)"
NUM = r"(\d{4}(?: \d{4}){2,4}|\d{9,18}|[A-Z]{1,4}\d[A-Z0-9]{7,20})"
RX_ANCHORED = re.compile(ANCHOR + r"\s*(?:NO|NUMBER|NUM|ID|#)?\s*[:.\-#]*\s*" + NUM, re.I)
RX_ANCHOR_ONLY = re.compile(ANCHOR, re.I)
RX_UNANCHORED = re.compile(r"(?<![\d])(\d{11,14})(?![\d])")
RX_WEIGHT = re.compile(
    r"((?:DEAD|ACTUAL|CHARGED|VOL(?:UMETRIC)?)?\s*(?:WEIGHT|WGT|WT))[^0-9]{0,12}(\d+(?:[.,]\d+)?)\s*(KGS?|GMS?|GRAMS?|G)?\b", re.I)
RX_WEIGHT_LOOSE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(KGS?)\b", re.I)
RX_DIM = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(CMS?|MM|INCH|IN)?", re.I)
LABEL_WORDS = re.compile(r"\b(AWB|WAYBILL|WAY BILL|SHIP\s*TO|DELIVER|CONSIGNEE|PIN|PINCODE|COD|PREPAID|ORDER|SELLER|"
                         r"DELHIVERY|BLUE\s*DART|BLUEDART|ECOM|XPRESSBEES|SHADOWFAX|DTDC|EKART|VALMO|AMAZON|WEIGHT|"
                         r"DIMENSIONS?|FRAGILE|SURFACE|EXPRESS|RETURN|SKU|INVOICE)\b", re.I)


def _clean_awb(s): return re.sub(r"\s+", "", s).upper()


def parse_weight(text):
    cands = []
    for m in RX_WEIGHT.finditer(text):
        label, val, unit = m.group(1).upper(), float(m.group(2).replace(",", ".")), (m.group(3) or "").upper()
        cands.append((label, val, unit))
    note = ""
    if not cands:
        m = RX_WEIGHT_LOOSE.search(text)
        if m: cands.append(("", float(m.group(1).replace(",", ".")), "KG"))
    if not cands: return None, ""
    pick = next((c for c in cands if "VOL" not in c[0]), cands[0])  # prefer dead/actual weight over volumetric
    _, val, unit = pick
    if not unit:
        unit = "KG" if val <= 50 else "G"; note = "weight unit assumed"
    kg = val / 1000 if unit.startswith("G") else val
    if kg <= 0 or kg > 500: return None, "weight out of plausible range"
    return round(kg, 3), note


def parse_dims(text):
    for m in RX_DIM.finditer(text):
        a, b, c = (float(m.group(i)) for i in (1, 2, 3))
        unit = (m.group(4) or "CM").upper()
        f = 0.1 if unit == "MM" else 2.54 if unit.startswith("IN") else 1.0
        dims = [round(x * f, 1) for x in (a, b, c)]
        if all(0.5 <= d <= 300 for d in dims):
            return dims
    return None


# ----------------------------------------------------------------------------- image analysis
def _box(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def skin_fraction(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV); ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    m1 = cv2.inRange(hsv, (0, 40, 70), (20, 170, 255))
    m2 = cv2.inRange(ycc, (0, 135, 85), (255, 173, 127))
    return float(np.count_nonzero(m1 & m2)) / m1.size


def analyze_image(path):
    """Return one record dict for one image file."""
    rec = dict(status="", usable="N", awb="", awb_source="", weight_kg="", length_cm="", width_cm="", height_cm="",
               confidence="", notes="")
    data = np.fromfile(str(path), dtype=np.uint8) if Path(path).exists() else None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR) if data is not None and data.size else None
    if img is None:
        rec["status"] = "IMAGE_CORRUPT"; rec["notes"] = "Could not decode file as an image"; return rec
    h0, w0 = img.shape[:2]
    s = MAX_SIDE / max(h0, w0)
    if s < 1: img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    contrast = float(gray.std())

    # --- barcodes
    barcodes = []
    try:
        import zxingcpp
        for r in zxingcpp.read_barcodes(gray):
            p = r.position
            xs = [p.top_left.x, p.top_right.x, p.bottom_right.x, p.bottom_left.x]
            ys = [p.top_left.y, p.top_right.y, p.bottom_right.y, p.bottom_left.y]
            barcodes.append(dict(text=r.text.strip(), fmt=str(r.format), box=(min(xs), min(ys), max(xs), max(ys))))
    except Exception as e:  # barcode lib failing must not kill the row
        rec["notes"] += f"barcode decode error: {e}; "

    # --- OCR
    lines = []
    try:
        res, _ = get_ocr()(img)
        for box, txt, sc in (res or []):
            lines.append(dict(text=txt, conf=float(sc), box=_box(box)))
    except Exception as e:
        rec["notes"] += f"OCR error: {e}; "
    lines.sort(key=lambda l: (round(l["box"][1] / 12), l["box"][0]))
    text = " ".join(l["text"] for l in lines)
    chars = sum(len(l["text"]) for l in lines)
    mean_conf = float(np.mean([l["conf"] for l in lines])) if lines else 0.0
    label_hits = len(LABEL_WORDS.findall(text))

    # --- AWB candidates
    def awb_like(t): return bool(re.fullmatch(r"[A-Z0-9]{8,22}", t)) and sum(c.isdigit() for c in t) >= 6
    bc_awbs = []
    for b in barcodes:
        t = _clean_awb(b["text"])
        if awb_like(t): bc_awbs.append((t, b))
    anchored = []
    for m in RX_ANCHORED.finditer(text):
        t = _clean_awb(m.group(1))
        if awb_like(t) and t not in [a[0] for a in anchored]:
            ln = next((l for l in lines if t[:6] in _clean_awb(l["text"])), None)
            anchored.append((t, ln["conf"] if ln else mean_conf, ln))
    anchor_word_seen = bool(RX_ANCHOR_ONLY.search(text))
    unanch = [x for x in RX_UNANCHORED.findall(text) if x not in [a[0] for a in anchored]]

    bc_vals = list(dict.fromkeys(t for t, _ in bc_awbs))
    an_vals = [a[0] for a in anchored]
    strong = list(dict.fromkeys(an_vals + [v for v in bc_vals if v in text.replace(" ", "")]))

    # --- partial-frame evidence
    mx, my = EDGE_MARGIN * W, EDGE_MARGIN * H
    def touches(b): return b[0] <= mx or b[1] <= my or b[2] >= W - mx or b[3] >= H - my
    edge_lines = [l for l in lines if touches(l["box"]) and len(l["text"]) > 3]
    bc_edge = any(touches(b["box"]) for _, b in bc_awbs)

    # skin tone is measured only around the label text: kraft cardboard also looks skin-coloured, so a
    # whole-image measure would false-alarm on every brown parcel
    skin = 0.0
    if lines:
        x0 = int(max(0, min(l["box"][0] for l in lines) - 0.05 * W)); x1 = int(min(W, max(l["box"][2] for l in lines) + 0.05 * W))
        y0 = int(max(0, min(l["box"][1] for l in lines) - 0.05 * H)); y1 = int(min(H, max(l["box"][3] for l in lines) + 0.05 * H))
        skin = skin_fraction(img[y0:y1, x0:x1])

    # --- weight / dims (from the label text)
    kg, wnote = parse_weight(text)
    dims = parse_dims(text)

    def fill_fields():
        if kg is not None: rec["weight_kg"] = kg
        if dims: rec["length_cm"], rec["width_cm"], rec["height_cm"] = dims
        if wnote: rec["notes"] += wnote + "; "

    # --- multiple parcels: >1 distinct strong AWB, or two same-length numeric barcodes far apart
    multi = len(strong) >= 2
    if not multi and len(bc_awbs) >= 2:
        for i in range(len(bc_awbs)):
            for j in range(i + 1, len(bc_awbs)):
                (ta, ba), (tb, bb) = bc_awbs[i], bc_awbs[j]
                if ta != tb and len(ta) == len(tb) and ta.isdigit() and tb.isdigit():
                    dx = abs((ba["box"][0] + ba["box"][2]) - (bb["box"][0] + bb["box"][2])) / 2 / W
                    dy = abs((ba["box"][1] + ba["box"][3]) - (bb["box"][1] + bb["box"][3])) / 2 / H
                    if max(dx, dy) > 0.30: multi = True
    no_evidence = not barcodes and label_hits == 0 and chars < 25 and not anchored

    if no_evidence:
        if blur < BLUR_MIN and contrast > 25:
            rec["status"] = "LABEL_UNREADABLE"; rec["notes"] = f"image very blurry (sharpness {blur:.0f}); nothing readable"
        else:
            rec["status"] = "NO_PARCEL"; rec["notes"] = "no label-like text or barcode detected"
        return rec

    if multi:
        rec["status"] = "MULTIPLE_PARCELS"
        rec["notes"] = "multiple distinct AWBs in frame: " + ", ".join(strong or [t for t, _ in bc_awbs]) + "; not assigning"
        return rec

    # --- choose AWB
    awb = src = ""; conf = 0.0
    if bc_vals:
        pick = None
        if an_vals and an_vals[0] in bc_vals:
            pick, src, conf = an_vals[0], "barcode+ocr", 0.98
        elif len(bc_vals) == 1:
            pick, src, conf = bc_vals[0], "barcode", 0.93
        else:
            nums = [v for v in bc_vals if v.isdigit() and 9 <= len(v) <= 16]
            if len(nums) == 1: pick, src, conf = nums[0], "barcode", 0.85
            else: rec["notes"] += "several barcodes, AWB ambiguous: " + ", ".join(bc_vals) + "; "
        if pick: awb = pick
    if not awb and anchored:
        t, c, _ = anchored[0]
        if c >= OCR_AWB_MIN_CONF: awb, src, conf = t, "ocr", round(0.6 + 0.3 * c, 2)
        else: rec["notes"] += f"low-confidence OCR AWB candidate {t} (conf {c:.2f}); "

    if awb:
        rec.update(awb=awb, awb_source=src, confidence=round(conf, 2))
        fill_fields()
        partial = bc_edge or len(edge_lines) >= 2
        if partial:
            rec["status"] = "PARTIAL_PARCEL"; rec["usable"] = "REVIEW"
            rec["notes"] += "label touches image edge - other fields may be cut off; AWB itself verified; "
        else:
            rec["status"] = "OK"; rec["usable"] = "Y"
            missing = [n for n, v in (("weight", kg), ("dimensions", dims)) if not v]
            if missing: rec["notes"] += "not present on label: " + ", ".join(missing) + "; "
        return rec

    # --- no trustworthy AWB: explain why
    if edge_lines and (len(edge_lines) >= 2 or bc_edge):
        rec["status"] = "PARTIAL_PARCEL"; rec["notes"] += "label cut by frame edge and AWB not readable"
    elif (anchor_word_seen and not anchored) or skin > 0.08:
        rec["status"] = "LABEL_BLOCKED"
        rec["notes"] += ("AWB heading visible but number covered" if anchor_word_seen else f"skin-coloured area ({skin:.0%}) over label: hand/object likely covering it")
    else:
        rec["status"] = "LABEL_UNREADABLE"
        rec["notes"] += f"AWB not found/verified (sharpness {blur:.0f}, mean OCR conf {mean_conf:.2f})"
        if unanch: rec["notes"] += "; unverified number-like text: " + ", ".join(unanch[:3])
    rec["notes"] = rec["notes"].strip()
    return rec


# ----------------------------------------------------------------------------- batch job
COLS = ["file_name", "status", "usable", "awb", "awb_source", "weight_kg", "length_cm", "width_cm", "height_cm",
        "confidence", "notes"]


def _dims_str(r):
    v = [r.get(k, "") for k in ("length_cm", "width_cm", "height_cm")]
    return "x".join(str(x) for x in v) if all(x != "" for x in v) else ""


def process_batch(job_dir, progress, files):
    """files: list of (display_name, saved_path)."""
    records = []
    total = len(files)
    for i, (disp, path) in enumerate(files, 1):
        progress(i - 1, total, f"Reading image {i} of {total}: {disp}")
        try:
            r = analyze_image(path)
        except Exception as e:
            r = dict(status="IMAGE_CORRUPT", usable="N", notes=f"processing error: {e}")
        r["file_name"] = disp; r["_saved"] = Path(path).name
        records.append(r)
    # batch-level duplicate check
    seen = {}
    for r in records:
        if r.get("awb"): seen.setdefault(r["awb"], []).append(r)
    for awb, rs in seen.items():
        if len(rs) > 1:
            for r in rs:
                r["notes"] = (r.get("notes", "") + f" duplicate AWB across {len(rs)} images").strip()
                r["usable"] = "REVIEW"
                if r["status"] == "OK": r["status"] = "DUPLICATE_AWB"
    out = job_dir / "out" / "shipments.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig so Excel opens it cleanly
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore"); w.writeheader()
        for r in records: w.writerow({c: r.get(c, "") for c in COLS})
    counts = {}
    for r in records: counts[r["status"]] = counts.get(r["status"], 0) + 1
    rows = []
    for r in records:
        rows.append([{"v": r["file_name"], "img": f"/jobs/{job_dir.name}/image/{r['_saved']}"},
                     {"v": r["status"], "cls": "st-" + r["status"]}, r.get("usable", ""), r.get("awb", ""),
                     r.get("weight_kg", ""), _dims_str(r),
                     r.get("confidence", ""), r.get("notes", "")])
    progress(total, total, "Done")
    return {"files": ["shipments.csv"], "result": {
        "summary": [{"label": "Images", "value": total}] + [{"label": k, "value": v, "cls": "st-" + k} for k, v in sorted(counts.items())],
        "legend": STATUS_HELP,
        "tables": [{"title": "Per-image results", "columns": ["Image", "Status", "Usable", "AWB", "Weight kg", "L x W x H cm", "Conf", "Notes"], "rows": rows}]}}


def _collect_files(job_dir, uploads):
    """Save uploads (images and/or zips) -> list of (display_name, path). Raises UserError on empty input."""
    out, n = [], 0
    indir = job_dir / "in"
    for up in uploads:
        name = Path(up.filename or "").name
        if not name: continue
        ext = Path(name).suffix.lower()
        if ext == ".zip":
            tmp = indir / f"_{n}.zip"; up.save(tmp)
            try:
                with zipfile.ZipFile(tmp) as z:
                    for info in z.infolist()[:5000]:
                        if info.is_dir() or Path(info.filename).suffix.lower() not in IMG_EXT or info.file_size > 60_000_000: continue
                        n += 1; p = indir / f"{n:05d}_{Path(info.filename).name}"
                        with z.open(info) as src, open(p, "wb") as dst: shutil.copyfileobj(src, dst)
                        out.append((info.filename, p))
            except zipfile.BadZipFile:
                raise UserError(f"'{name}' is not a valid zip file.")
            finally:
                tmp.unlink(missing_ok=True)
        else:
            n += 1; p = indir / f"{n:05d}_{name}"; up.save(p)
            out.append((name, p))   # non-images / corrupt files get an IMAGE_CORRUPT row rather than being silently dropped
    if not out:
        raise UserError("No images found in the upload. Choose JPG/PNG files (or a .zip of them).")
    return out


@bp.route("/cs1", methods=["GET", "POST"])
def cs1():
    if request.method == "GET":
        return render_template("case.html", case="cs1")
    if request.form.get("sample") == "1":
        jid, d = jobs.new_job_dir("cs1", "Parcel images -> shipment data (sample)")
        src = Path(__file__).resolve().parent.parent / "sample_data" / "cs1_images"
        files = []
        for n, p in enumerate(sorted(src.iterdir()), 1):
            if p.suffix.lower() in IMG_EXT:
                dst = d / "in" / f"{n:05d}_{p.name}"; shutil.copy(p, dst); files.append((p.name, dst))
        jobs.submit(jid, process_batch, files)
        return redirect(url_for("job_page", jid=jid))
    ups = [u for u in request.files.getlist("files") if u and u.filename]
    if not ups:
        return render_template("case.html", case="cs1", error="Please choose at least one image (or a .zip)."), 400
    jid, d = jobs.new_job_dir("cs1", "Parcel images -> shipment data")
    try:
        files = _collect_files(d, ups)
    except UserError as e:
        return render_template("case.html", case="cs1", error=str(e)), 400
    jobs.submit(jid, process_batch, files)
    return redirect(url_for("job_page", jid=jid))


if __name__ == "__main__":       # CLI:  python -m app.cs1_parcels  <image_folder>  <out.csv>
    import sys
    folder = Path(sys.argv[1]); out = Path(sys.argv[2] if len(sys.argv) > 2 else "shipments.csv")
    fs = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMG_EXT)
    recs = []
    for p in fs:
        r = analyze_image(p); r["file_name"] = p.name; recs.append(r); print(p.name, r["status"], r["awb"])
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore"); w.writeheader(); w.writerows(recs)
