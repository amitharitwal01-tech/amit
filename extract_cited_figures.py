"""Phase 3 of the writing pipeline: pull the actual source images for
every figure and table your finalized manuscript's placeholder boxes
point to.

The draft scripts (export_for_claude.py output, as drafted by Claude)
write figure/table placeholders like:

    [ FIGURE 1 — The Emergence of Perovskite Photovoltaics ]
    A (Global energy landscape): adapt from [Entry 476], Fig. 1a (p.1)
    B (PV technology comparison): adapt from [Entry 2071], Fig. 1b (p.2)

    [ TABLE 1 — First-, Second-, and Third-Generation PV ]
    Library-supported: c-Si 27.4% [Entry 1361]; PSC >27% [Entry 451]

This script scans the manuscript for those boxes, and for every
"[Entry N] ... (p.X)" reference inside one:
  1. Uses the pre-cropped figure image already in your library index,
     if that entry/page combination was captured during build_index.py;
  2. Otherwise renders that exact page of the source PDF as an image —
     always possible as long as the paper itself was downloaded.
A citation with no page number (common in table boxes, which usually
cite a value rather than a location) has no single spot to crop, so
its whole source PDF is copied instead — enough to look the value up.

Output is organized as one subfolder per figure/table (named exactly
as it appears in the manuscript, e.g. "Figure1", "Table1"), each file
named by its panel and source so it stays traceable back to the box
that asked for it. A manifest CSV records every citation found and
how (or whether) it was resolved.

Usage:
    python extract_cited_figures.py manuscript.docx
    python extract_cited_figures.py manuscript.docx --out my_figures
"""
import argparse
import csv
import os
import re
import shutil
import sys

from doi_resolver import load_config, load_tracking
from extract_cited_references import extract_text
import build_index

BOX_HEADER = re.compile(r"\[\s*(FIGURE|TABLE)\s+(\d+)\s*[—\-–]+\s*([^\]]*)\]", re.IGNORECASE)
ENTRY_WITH_PAGE = re.compile(r"\[?Entry\s+(\d+)\]?[^()\[\]\n]{0,60}?\(p\.\s*(\d+)\)", re.IGNORECASE)
ENTRY_BARE = re.compile(r"\[?Entry\s+(\d+)\]?", re.IGNORECASE)
PANEL_LABEL = re.compile(r"^\s*(?:panel\s+)?([A-Ha-h])\s*\(([^)]{1,60})\)\s*:", re.IGNORECASE)
DEFAULT_OUT_DIR = "cited_figures_tables"


def find_boxes(text):
    # A "box" runs from one [FIGURE N — ...] / [TABLE N — ...] header
    # to the next one (or the end of the file).
    headers = list(BOX_HEADER.finditer(text))
    boxes = []
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        boxes.append({
            "kind": m.group(1).upper(),
            "number": m.group(2),
            "title": m.group(3).strip(),
            "body": text[m.end():end],
        })
    return boxes


def parse_box_citations(box):
    # Per line: an optional leading panel label, every entry+page pair
    # on that line, and any bare (page-less) entry citations left over.
    items = []
    for line in box["body"].splitlines():
        if not line.strip():
            continue
        panel_match = PANEL_LABEL.match(line)
        panel = panel_match.group(1).upper() if panel_match else None

        with_page = list(ENTRY_WITH_PAGE.finditer(line))
        paged_entries = {m.group(1) for m in with_page}
        for m in with_page:
            items.append({"panel": panel, "entry": m.group(1), "page": m.group(2), "line": line.strip()})

        for m in ENTRY_BARE.finditer(line):
            if m.group(1) not in paged_entries:
                items.append({"panel": panel, "entry": m.group(1), "page": None, "line": line.strip()})
                paged_entries.add(m.group(1))  # one bare mention per entry per line is enough
    return items


def build_figure_lookup(db):
    # (doi, page) -> [(caption, image_path), ...] — the pre-cropped
    # images already captured by build_index.py.
    lookup = {}
    try:
        rows = db.execute("SELECT doi, page, caption, image_path FROM figures").fetchall()
    except Exception:
        return lookup
    for doi, page, caption, image_path in rows:
        lookup.setdefault((doi, page), []).append((caption or "", image_path))
    return lookup


def pick_best_crop(candidates, line_text):
    # When a page has several extracted images, prefer the one whose
    # caption text actually appears in this citation's line.
    line_lower = line_text.lower()
    for caption, path in candidates:
        snippet = (caption or "")[:40].lower().strip()
        if snippet and snippet in line_lower and path and os.path.exists(path):
            return path
    for _, path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def render_pdf_page(pdf_path, page_num, dest_path, dpi=150):
    try:
        import fitz
    except ImportError:
        return False
    try:
        with fitz.open(pdf_path) as doc:
            if not (1 <= page_num <= doc.page_count):
                return False
            pix = doc[page_num - 1].get_pixmap(dpi=dpi)
            pix.save(dest_path)
        return True
    except Exception:
        return False


def safe_name(text, max_len=40):
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:max_len] or "item"


def main():
    parser = argparse.ArgumentParser(
        description="Pull the source images/pages for every figure and table cited in a finished manuscript."
    )
    parser.add_argument("manuscript", help="path to the manuscript (.docx, .md, or .txt)")
    parser.add_argument("--out", help="destination folder "
                        f"(default: finalized/<manuscript name>/{DEFAULT_OUT_DIR})")
    args = parser.parse_args()
    if not args.out:
        # Same per-manuscript folder the other finalize tools use.
        stem = os.path.splitext(os.path.basename(args.manuscript))[0]
        args.out = os.path.join("finalized", stem, DEFAULT_OUT_DIR)

    if not os.path.exists(args.manuscript):
        sys.exit(f"Manuscript not found: {args.manuscript}")

    config = load_config()
    tracking = load_tracking(config["paths"]["tracking_csv"])
    entry_index = {(row.get("Entry") or "").strip(): row for row in tracking.values() if row.get("Entry")}

    db = build_index.open_db()
    figure_lookup = build_figure_lookup(db)

    text = extract_text(args.manuscript)
    boxes = find_boxes(text)
    if not boxes:
        sys.exit(
            "No [FIGURE N — ...] / [TABLE N — ...] placeholder boxes found in that "
            "file — nothing to extract."
        )

    os.makedirs(args.out, exist_ok=True)
    manifest = []
    counts = {"crop": 0, "rendered": 0, "pdf_fallback": 0, "missing": 0}

    for box in boxes:
        box_name = f"{box['kind'].capitalize()}{box['number']}"
        box_dir = os.path.join(args.out, box_name)
        items = parse_box_citations(box)
        if not items:
            continue
        os.makedirs(box_dir, exist_ok=True)
        print(f"{box_name} — {box['title'][:60] or '(untitled)'}: {len(items)} citation(s)")

        seen_in_box = set()
        for item in items:
            entry, page, panel = item["entry"], item["page"], item["panel"]
            dedup_key = (entry, page)
            if dedup_key in seen_in_box:
                continue
            seen_in_box.add(dedup_key)

            row = entry_index.get(entry)
            label = f"  [Entry {entry}" + (f", p.{page}]" if page else "]")
            if not row:
                print(f"{label} not in tracking sheet — skipped")
                manifest.append({"Box": box_name, "Panel": panel or "", "Entry": entry, "Page": page or "",
                                  "Status": "not in tracking sheet", "File": ""})
                counts["missing"] += 1
                continue

            doi = row.get("DOI", "")
            pdf_path = row.get("PDF_Path", "")
            name_stub = (f"Panel{panel}_" if panel else "") + f"Entry{entry}" + (f"_p{page}" if page else "")

            if page:
                page_num = int(page)
                candidates = figure_lookup.get((doi, page_num), [])
                crop_path = pick_best_crop(candidates, item["line"]) if candidates else None
                if crop_path:
                    ext = os.path.splitext(crop_path)[1] or ".png"
                    dest = os.path.join(box_dir, name_stub + ext)
                    shutil.copy2(crop_path, dest)
                    print(f"{label} extracted crop -> {box_name}/{os.path.basename(dest)}")
                    manifest.append({"Box": box_name, "Panel": panel or "", "Entry": entry, "Page": page,
                                      "Status": "library crop", "File": dest})
                    counts["crop"] += 1
                    continue
                if pdf_path and os.path.exists(pdf_path):
                    dest = os.path.join(box_dir, name_stub + ".png")
                    if render_pdf_page(pdf_path, page_num, dest):
                        print(f"{label} rendered page {page} -> {box_name}/{os.path.basename(dest)}")
                        manifest.append({"Box": box_name, "Panel": panel or "", "Entry": entry, "Page": page,
                                          "Status": "rendered page", "File": dest})
                        counts["rendered"] += 1
                        continue

            # No page, or nothing above worked: fall back to the whole PDF.
            if pdf_path and os.path.exists(pdf_path):
                dest = os.path.join(box_dir, name_stub + "_" + os.path.basename(pdf_path))
                shutil.copy2(pdf_path, dest)
                print(f"{label} no page-level image — copied source PDF -> {box_name}/{os.path.basename(dest)}")
                manifest.append({"Box": box_name, "Panel": panel or "", "Entry": entry, "Page": page or "",
                                  "Status": "whole PDF (fallback)", "File": dest})
                counts["pdf_fallback"] += 1
            else:
                print(f"{label} no PDF on file — skipped")
                manifest.append({"Box": box_name, "Panel": panel or "", "Entry": entry, "Page": page or "",
                                  "Status": "no PDF found", "File": ""})
                counts["missing"] += 1

    manifest_path = os.path.join(args.out, "figure_table_manifest.csv")
    with open(manifest_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Box", "Panel", "Entry", "Page", "Status", "File"])
        writer.writeheader()
        for row in manifest:
            writer.writerow(row)

    print()
    print(
        f"Done. {counts['crop']} pre-cropped image(s), {counts['rendered']} page(s) rendered, "
        f"{counts['pdf_fallback']} whole-PDF fallback(s)"
        + (f", {counts['missing']} missing" if counts["missing"] else "") + "."
    )
    print(f"Manifest written to {manifest_path}.")


if __name__ == "__main__":
    main()
