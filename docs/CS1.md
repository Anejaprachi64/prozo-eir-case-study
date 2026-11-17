# Case Study 1 — Parcel images → shipment data

**Run:** `python run.py` → `/cs1` → select many images or a `.zip` → results table with thumbnails + **Download shipments.csv**.
CLI: `python -m app.cs1_parcels sample_data/cs1_images out.csv`.

## Approach (all free, offline)
Per image (`app/cs1_parcels.py::analyze_image`):
1. Decode (corrupt → `IMAGE_CORRUPT`), downscale to ≤1600 px.
2. **Barcode decode** (zxing-cpp). Code128 has a checksum, so a decoded barcode is the most trustworthy AWB source.
3. **OCR** (RapidOCR / ONNX — models bundled in the pip wheel; no Tesseract binary, no API key).
4. Regex extraction anchored on label words: AWB (`AWB/Waybill/Tracking/Docket…`), weight (kg/g, prefers dead weight over volumetric), dimensions (`L x W x H`, cm/mm/in → cm).
5. Rule-based quality flags, then a batch-level duplicate-AWB check.

**Rule: a wrong extraction is worse than a flag.** An AWB is written to the `awb` column only if it is barcode-decoded (and cross-checked with OCR when both exist), or OCR-read *with* a label anchor and line confidence ≥ 0.90. Otherwise the candidate goes into `notes` and the row is flagged.

### Status taxonomy (`status`) and `usable` (Y / REVIEW / N)
| status | meaning / how detected |
|---|---|
| `OK` | AWB verified. Missing weight/dims are noted as "not on label", not treated as errors |
| `NO_PARCEL` | no label text, no barcode (empty frame — or parcel facing away; indistinguishable) |
| `MULTIPLE_PARCELS` | ≥2 distinct keyword-anchored/barcode AWBs, or two same-type numeric barcodes far apart. **No AWB assigned** |
| `PARTIAL_PARCEL` | label text/barcode touches the frame edge. AWB kept only if barcode-verified, `usable=REVIEW` |
| `LABEL_BLOCKED` | label text present but AWB covered: "AWB" heading with no number, or skin-tone area over the label text |
| `LABEL_UNREADABLE` | blurry / low OCR confidence; unverified candidates listed in `notes` |
| `DUPLICATE_AWB` | same AWB on >1 image in the batch |
| `IMAGE_CORRUPT` | not a decodable image |

CSV columns: `file_name, status, usable, awb, awb_source, weight_kg, length_cm, width_cm, height_cm, confidence, notes` (UTF-8 with BOM so Excel opens it cleanly).

## Result on test data
16 synthetic photos (clean ×6 incl. 2 without weight/dims, empty ×2, two-parcel ×2, cropped ×2, hand-covered ×2, blurry, corrupt): **16/16 correct status, all AWBs/weights/dimensions exact** (`sample_outputs/cs1_shipments.csv`). Speed ≈ 0.9 s/image on CPU, so 500 images ≈ 7–8 min, in the background.
**Caveat:** synthetic labels are cleaner than real warehouse photos. I could not access the provided dataset.

## Known limitations
* Thresholds (`BLUR_MIN`, `OCR_AWB_MIN_CONF`, `EDGE_MARGIN`) were tuned on synthetic data and will need a pass on real images — they are constants at the top of the file.
* "Hand blocking" uses a skin-tone mask restricted to the label area; gloves or boxes covering a label fall back to `LABEL_UNREADABLE`/`NO_PARCEL`.
* Multi-parcel detection is heuristic (distinct AWBs). Two parcels with unreadable labels may be reported as another flag.
* Curved/rotated/very small labels, thermal-print fading, and non-Latin text are weak spots. Couriers' AWB formats are matched generically (9–18 digits or letter-prefixed), not per courier.
* Images are processed sequentially per job (simple, predictable).

## With more time
Courier-specific AWB validators (length/prefix/check digit) and courier detection; perspective-correct + deskew the label before OCR; a small YOLO/OpenCV label detector to get real "parcel present" and "label cut" signals; parallel workers; a review UI to correct flagged rows and feed corrections back as a regression set.
