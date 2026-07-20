"""Phase 2 of the writing pipeline: once a manuscript is finalized, pull
exactly the papers it cites out of your library into their own folder.

Scans a manuscript (.docx, .md, or .txt) for citation labels of the form
"[Entry 12" / "[Entry 12, p.5]" — the format every research pack and
draft in this pipeline uses — looks each one up in the tracking sheet,
and copies its PDF into a destination folder. Filenames already carry
the entry number and title (from build_pdf_filename in doi_resolver.py),
so the copies stay traceable to the manuscript without renaming.

Also writes a manifest (CSV) listing every cited entry: title, year,
DOI, how many times it's cited, and whether its PDF was found — so you
can spot a citation that has no backing file before submission.

Usage:
    python extract_cited_references.py manuscript.docx
    python extract_cited_references.py manuscript.docx --out my_refs
"""
import argparse
import csv
import os
import re
import shutil
import sys
import zipfile

from doi_resolver import load_config, load_tracking

CITATION_PATTERN = re.compile(r"\[?\bEntry\s+(\d+)\b", re.IGNORECASE)
DEFAULT_OUT_DIR = "cited_references"


def extract_text_from_docx(path):
    # Doesn't require python-docx: a .docx is a zip archive, and
    # word/document.xml holds the text between XML tags. Paragraph ends
    # are turned into newlines before the remaining tags are stripped,
    # so each line of a generated placeholder box (one Paragraph each)
    # survives as its own line — needed by extract_cited_figures.py to
    # associate a citation with the panel/row it appeared in.
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = xml.replace("</w:p>", "</w:p>\n")
    return re.sub(r"<[^>]+>", " ", xml)


def extract_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return extract_text_from_docx(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def find_cited_entries(text):
    # Order of first appearance, deduplicated — reads naturally as a
    # reference list and makes the manifest easy to skim against the
    # manuscript.
    seen = []
    counts = {}
    for match in CITATION_PATTERN.finditer(text):
        entry = match.group(1)
        if entry not in counts:
            seen.append(entry)
        counts[entry] = counts.get(entry, 0) + 1
    return seen, counts


def build_entry_index(tracking):
    index = {}
    for row in tracking.values():
        entry = (row.get("Entry") or "").strip()
        if entry:
            index[entry] = row
    return index


def main():
    parser = argparse.ArgumentParser(
        description="Copy every PDF cited [Entry N] in a finished manuscript into its own folder."
    )
    parser.add_argument("manuscript", help="path to the manuscript (.docx, .md, or .txt)")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help=f"destination folder (default: {DEFAULT_OUT_DIR})")
    args = parser.parse_args()

    if not os.path.exists(args.manuscript):
        sys.exit(f"Manuscript not found: {args.manuscript}")

    config = load_config()
    tracking = load_tracking(config["paths"]["tracking_csv"])
    entry_index = build_entry_index(tracking)

    text = extract_text(args.manuscript)
    cited, counts = find_cited_entries(text)
    if not cited:
        sys.exit("No [Entry N] citations found in that file — nothing to extract.")

    print(f"{len(cited)} distinct entr{'y' if len(cited) == 1 else 'ies'} cited in {os.path.basename(args.manuscript)}.")
    os.makedirs(args.out, exist_ok=True)

    manifest = []
    copied = missing_row = missing_pdf = 0

    for entry in cited:
        row = entry_index.get(entry)
        if not row:
            print(f"  [Entry {entry}] not found in the tracking sheet — skipped")
            manifest.append({"Entry": entry, "Title": "", "Year": "", "DOI": "",
                              "Citations": counts[entry], "Status": "not in tracking sheet"})
            missing_row += 1
            continue

        pdf_path = row.get("PDF_Path", "")
        title = row.get("Title", "")
        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  [Entry {entry}] {title[:60]!r} — no PDF on file, skipped")
            manifest.append({"Entry": entry, "Title": title, "Year": row.get("Year", ""),
                              "DOI": row.get("DOI", ""), "Citations": counts[entry],
                              "Status": "no PDF found"})
            missing_pdf += 1
            continue

        dest_name = os.path.basename(pdf_path)
        dest_path = os.path.join(args.out, dest_name)
        # Two different entries should never share a source filename
        # (build_pdf_filename always leads with the entry number), but
        # guard anyway rather than silently overwrite one paper with another.
        if os.path.exists(dest_path) and not os.path.samefile(dest_path, pdf_path):
            base, ext = os.path.splitext(dest_name)
            dest_name = f"{base}_Entry{entry}{ext}"
            dest_path = os.path.join(args.out, dest_name)
        shutil.copy2(pdf_path, dest_path)
        copied += 1
        manifest.append({"Entry": entry, "Title": title, "Year": row.get("Year", ""),
                          "DOI": row.get("DOI", ""), "Citations": counts[entry],
                          "Status": "copied", "File": dest_name})

    manifest_path = os.path.join(args.out, "reference_manifest.csv")
    with open(manifest_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Entry", "Title", "Year", "DOI", "Citations", "Status", "File"])
        writer.writeheader()
        for row in manifest:
            writer.writerow(row)

    print()
    print(f"Done. {copied} PDF(s) copied to {args.out}/"
          + (f", {missing_pdf} cited entr{'y has' if missing_pdf == 1 else 'ies have'} no PDF" if missing_pdf else "")
          + (f", {missing_row} not found in the tracking sheet" if missing_row else "")
          + ".")
    print(f"Manifest written to {manifest_path}.")
    if missing_pdf or missing_row:
        print("Check the manifest before submission — those citations have no backing file in this folder.")


if __name__ == "__main__":
    main()
