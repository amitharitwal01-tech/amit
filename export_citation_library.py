"""Phase 4 of the writing pipeline: get every paper your finalized
manuscript cites into EndNote or Zotero, so you can insert real, live
citations in place of the [Entry N] placeholders — and from then on,
adding, removing, or reordering a reference anywhere renumbers the
whole document and reference list automatically.

That auto-renumbering only works through EndNote's Cite-While-You-Write
or Zotero's Word plugin, because it depends on field codes only those
tools write correctly into the .docx. This script does not attempt to
generate those field codes itself — that format is undocumented and
version-specific, and getting it wrong risks corrupting a finished
manuscript. Instead it does the two things a script CAN do safely:

  1. Export every cited paper's metadata as a .ris file — the standard
     format both EndNote and Zotero import in one step (File > Import),
     building a ready-to-cite library instead of adding papers by hand.
     Each record includes the paper's DOI (so either tool can fetch full
     bibliographic detail) and a link to its PDF (so it's attached on
     import), plus a note recording which [Entry N] it corresponds to.

  2. Write a checklist, in the order citations first appear in the
     manuscript, of which placeholder maps to which paper — so you can
     work through the document top to bottom, replacing "[Entry 476]"
     with a real "Insert Citation" from the plugin as you go.

Workflow after running this:
  a. Open EndNote/Zotero, File > Import, choose the .ris file produced
     here. Papers arrive with their PDFs attached.
  b. Open the manuscript in Word with the Cite-While-You-Write or
     Zotero plugin enabled.
  c. Using the checklist as your guide, Ctrl+F to each [Entry N],
     delete the placeholder text, and use the plugin's Insert Citation
     to insert the real paper.
  d. Once every placeholder is replaced, use the plugin's "Update
     Citations and Bibliography" — numbering and the reference list are
     now fully automatic from here on.

Usage:
    python export_citation_library.py manuscript.docx
    python export_citation_library.py manuscript.docx --out my_library
"""
import argparse
import csv
import os
import sys

from doi_resolver import load_config, load_tracking
from extract_cited_references import extract_text, find_cited_entries, build_entry_index

DEFAULT_OUT_PREFIX = "cited_papers"


def split_authors(authors_field):
    # Stored as "Given Family; Given Family; ...". RIS prefers
    # "Family, Given" per AU line; both EndNote and Zotero parse
    # "Given Family" too, but the explicit split is more reliable,
    # especially for display in the reference list.
    names = []
    for raw in (authors_field or "").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split()
        if len(parts) >= 2:
            family = parts[-1]
            given = " ".join(parts[:-1])
            names.append(f"{family}, {given}")
        else:
            names.append(raw)
    return names


def ris_record(row, entry, citation_count, manuscript_name):
    lines = ["TY  - JOUR"]
    title = (row.get("Title") or "").strip()
    if title:
        lines.append(f"TI  - {title}")
    for author in split_authors(row.get("Authors", "")):
        lines.append(f"AU  - {author}")
    year = (row.get("Year") or "").strip()
    if year:
        lines.append(f"PY  - {year}")
    doi = (row.get("DOI") or "").strip()
    if doi:
        lines.append(f"DO  - {doi}")
    abstract = (row.get("Abstract") or "").strip()
    if abstract and not abstract.startswith("(no abstract"):
        # RIS reads a field until the next two-letter tag, so keep the
        # abstract on one line.
        lines.append(f"AB  - {abstract}")
    pdf_path = row.get("PDF_Path", "")
    if pdf_path and os.path.exists(pdf_path):
        # L1 is the standard RIS "link to full text" tag both EndNote
        # and Zotero use to auto-attach a local PDF on import.
        lines.append(f"L1  - {os.path.abspath(pdf_path)}")
    lines.append(
        f"N1  - Paper-pipeline Entry {entry} — cited {citation_count}x in {manuscript_name}. "
        f"Use this note to match this record back to the [Entry {entry}] placeholder in the manuscript."
    )
    lines.append("ER  - ")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Export every paper cited [Entry N] in a manuscript as a .ris file for EndNote/Zotero."
    )
    parser.add_argument("manuscript", help="path to the manuscript (.docx, .md, or .txt)")
    parser.add_argument("--out", default=DEFAULT_OUT_PREFIX,
                         help=f"output filename prefix (default: {DEFAULT_OUT_PREFIX})")
    args = parser.parse_args()

    if not os.path.exists(args.manuscript):
        sys.exit(f"Manuscript not found: {args.manuscript}")

    config = load_config()
    tracking = load_tracking(config["paths"]["tracking_csv"])
    entry_index = build_entry_index(tracking)

    text = extract_text(args.manuscript)
    cited, counts = find_cited_entries(text)
    if not cited:
        sys.exit("No [Entry N] citations found in that file — nothing to export.")

    manuscript_name = os.path.basename(args.manuscript)
    records = []
    checklist = []
    missing = 0

    for order, entry in enumerate(cited, 1):
        row = entry_index.get(entry)
        if not row:
            print(f"  [Entry {entry}] not in tracking sheet — skipped")
            missing += 1
            continue
        records.append(ris_record(row, entry, counts[entry], manuscript_name))
        title = row.get("Title", "")
        first_author = split_authors(row.get("Authors", ""))
        first_author_name = first_author[0].split(",")[0] if first_author else ""
        checklist.append({
            "Order": order, "Entry": entry, "Citations": counts[entry],
            "First_Author": first_author_name, "Year": row.get("Year", ""),
            "Title": title, "DOI": row.get("DOI", ""),
        })

    ris_path = f"{args.out}.ris"
    with open(ris_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(records) + "\n")

    checklist_path = f"{args.out}_insertion_checklist.csv"
    with open(checklist_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Order", "Entry", "Citations", "First_Author", "Year", "Title", "DOI"])
        writer.writeheader()
        for row in checklist:
            writer.writerow(row)

    print()
    print(f"{len(records)} paper(s) exported to {ris_path}"
          + (f" ({missing} citation(s) had no tracking-sheet entry, skipped)" if missing else "") + ".")
    print(f"Insertion checklist (order of first appearance) written to {checklist_path}.")
    print()
    print("Next: import the .ris file into EndNote or Zotero (File > Import), then use the")
    print("checklist to replace each [Entry N] in the manuscript with a real Insert Citation")
    print("from the plugin — from then on, the plugin renumbers everything automatically.")


if __name__ == "__main__":
    main()
