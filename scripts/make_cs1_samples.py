"""Generate synthetic parcel photos for Case Study 1 (real dataset link was not accessible offline).
Scenarios: clean, no parcel, multiple parcels, partial (cropped), label blocked by a 'hand', blurry, label w/o weight.
Usage: python scripts/make_cs1_samples.py   -> sample_data/cs1_images/*.jpg + expected.csv
"""
import csv, io, random
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
import barcode
from barcode.writer import ImageWriter

OUT = Path(__file__).resolve().parent.parent / "sample_data" / "cs1_images"
OUT.mkdir(parents=True, exist_ok=True)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONTB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
random.seed(7)
COURIERS = ["DELHIVERY", "XPRESSBEES", "ECOM EXPRESS", "SHADOWFAX", "BLUE DART"]
CITIES = [("Ludhiana", "141001", "Punjab"), ("Gurugram", "122001", "Haryana"), ("Pune", "411001", "Maharashtra"), ("Bengaluru", "560001", "Karnataka")]


def f(sz, bold=False): return ImageFont.truetype(FONTB if bold else FONT, sz)


def make_label(awb, weight=None, dims=None, w=620, h=420):
    im = Image.new("RGB", (w, h), "white"); d = ImageDraw.Draw(im)
    d.rectangle([2, 2, w - 3, h - 3], outline="black", width=3)
    d.text((14, 10), random.choice(COURIERS), font=f(26, True), fill="black")
    d.text((w - 190, 14), random.choice(["PREPAID", "COD"]), font=f(22, True), fill="black")
    bc = barcode.get("code128", awb, writer=ImageWriter())
    buf = io.BytesIO(); bc.write(buf, options={"write_text": False, "module_height": 14, "module_width": 0.32, "quiet_zone": 4})
    b = Image.open(buf).convert("RGB"); b = b.resize((min(w - 40, b.width), 90))
    im.paste(b, (20, 50))
    d.text((20, 145), f"AWB No: {awb}", font=f(26, True), fill="black")
    c = random.choice(CITIES)
    d.text((20, 190), f"Ship To: Rohan Verma, 14 MG Road, {c[0]}", font=f(18), fill="black")
    d.text((20, 218), f"{c[2]} PIN: {c[1]}", font=f(18), fill="black")
    d.text((20, 250), f"Order: OD{random.randint(10**9, 10**10-1)}", font=f(18), fill="black")
    y = 290
    if weight: d.text((20, y), f"Weight: {weight} kg", font=f(22), fill="black"); y += 36
    if dims: d.text((20, y), f"Dimensions: {dims[0]} x {dims[1]} x {dims[2]} cm", font=f(22), fill="black")
    return im


def scene(labels, size=(1280, 960), bg=(176, 138, 98)):
    """cardboard-coloured parcel(s) with labels pasted at slight angles on a warehouse-floor background"""
    canvas = Image.new("RGB", size, (110, 112, 118))
    arr = np.array(canvas).astype(np.int16) + np.random.randint(-8, 8, (size[1], size[0], 1))
    canvas = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
    d = ImageDraw.Draw(canvas)
    n = len(labels)
    for i, lab in enumerate(labels):
        cw = size[0] // n
        x0, y0 = i * cw + 40, 80
        d.rectangle([x0, y0, x0 + cw - 80, size[1] - 80], fill=bg, outline=(120, 90, 60), width=4)
        lab = lab.resize((min(lab.width, cw - 140), int(lab.height * min(1, (cw - 140) / lab.width))))
        lab = lab.rotate(random.uniform(-3, 3), expand=True, fillcolor=(255, 255, 255))
        canvas.paste(lab, (x0 + (cw - 80 - lab.width) // 2, y0 + 110))
    return canvas


def awb():  return str(random.randint(10**11, 10**12 - 1))


rows = []
def save(name, im, status, a="", wt="", dm=""):
    im.save(OUT / name, quality=90); rows.append([name, status, a, wt, dm])


for i in range(1, 7):                         # clean, various field presence
    a = awb(); wt = random.choice([0.35, 0.8, 1.25, 2.4, 3.1]); dm = random.choice([(20, 15, 10), (30, 20, 12), (40, 30, 25)])
    full = i % 3 != 0
    save(f"clean_{i}.jpg", scene([make_label(a, wt if full else None, dm if full else None)]), "OK", a, wt if full else "", "x".join(map(str, dm)) if full else "")

save("empty_belt_1.jpg", scene([]), "NO_PARCEL")
Image.fromarray(np.random.randint(90, 130, (960, 1280, 3), dtype="uint8")).save(OUT / "empty_belt_2.jpg"); rows.append(["empty_belt_2.jpg", "NO_PARCEL", "", "", ""])

for i in (1, 2):                              # two parcels in frame
    save(f"two_parcels_{i}.jpg", scene([make_label(awb(), 1.1, (20, 15, 10)), make_label(awb(), 2.2, (25, 20, 10))]), "MULTIPLE_PARCELS")

for i in (1, 2):                              # partial: crop through the label
    a = awb(); im = scene([make_label(a, 1.5, (22, 14, 9))])
    w, h = im.size
    save(f"partial_{i}.jpg", im.crop((int(w * 0.30), int(h * 0.12), w, int(h * 0.62))), "PARTIAL_PARCEL")

for i in (1, 2):                              # hand covering AWB/barcode
    a = awb(); im = scene([make_label(a, 0.9, (18, 12, 8))]); d = ImageDraw.Draw(im)
    d.ellipse([330, 150, 760, 400], fill=(224, 172, 140)); d.ellipse([280, 300, 700, 520], fill=(214, 160, 128))
    save(f"blocked_{i}.jpg", im, "LABEL_BLOCKED")

a = awb(); save("blurry_1.jpg", scene([make_label(a, 1.0, (20, 20, 10))]).filter(ImageFilter.GaussianBlur(9)), "LABEL_UNREADABLE")
open(OUT / "corrupt.jpg", "wb").write(b"not really a jpeg")  ; rows.append(["corrupt.jpg", "IMAGE_CORRUPT", "", "", ""])

with open(OUT.parent / "cs1_expected.csv", "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["file", "expected_status", "awb", "weight_kg", "dims"]); w.writerows(rows)
print(f"wrote {len(rows)} images to {OUT}")
