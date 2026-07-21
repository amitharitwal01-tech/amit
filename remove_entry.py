"""Remove a paper — and everything associated with it — from the
library: its row in the tracking sheet, its entry in the search index
(the papers/chunks/figures tables, plus any cropped figure images
saved for it). The PDF itself is moved to removed_pdfs/, not deleted,
so nothing is lost if this turns out to be a mistake.

Use this to clean up something that got imported by mistake — e.g. a
resume, invoice, or other non-paper PDF picked up by
import_existing_pdfs.py / import_from_laptop.py.

Usage:
    python remove_entry.py --entry 2165
    python remove_entry.py --entry 2165 2170 2201
    python remove_entry.py --doi 10.1000/example
    python remove_entry.py --entry 2165 --yes
"""
import argparse
import os
import shutil
import sys

import build_index
from doi_resolver import load_config, load_tracking, normalize_doi, save_tracking

REMOVED_PDFS_DIR = "removed_pdfs"


def find_rows(tracking, entries, dois):
    wanted_entries = {str(e) for e in entries}
    wanted_dois = {normalize_doi(d) for d in dois}
    return [
        (key, row) for key, row in tracking.items()
        if str(row.get("Entry", "")) in wanted_entries
        or (row.get("DOI") and normalize_doi(row["DOI"]) in wanted_dois)
    ]


def remove_from_index(db, index_key):
    figure_paths = [r[0] for r in db.execute("SELECT image_path FROM figures WHERE doi = ?", (index_key,))]
    for image_path in figure_paths:
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except OSError:
                pass
    db.execute("DELETE FROM figures WHERE doi = ?", (index_key,))
    db.execute("DELETE FROM chunks WHERE doi = ?", (index_key,))
    db.execute("DELETE FROM papers WHERE doi = ?", (index_key,))
    db.commit()


def main():
    parser = argparse.ArgumentParser(
        description="Remove a paper from the tracking sheet and search index (its PDF is moved aside, not deleted)."
    )
    parser.add_argument("--entry", nargs="*", type=int, default=[], help="Entry number(s) to remove")
    parser.add_argument("--doi", nargs="*", default=[], help="DOI(s) to remove")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    if not args.entry and not args.doi:
        parser.error("give at least one --entry or --doi")

    config = load_config()
    tracking_path = config["paths"]["tracking_csv"]
    tracking = load_tracking(tracking_path)

    matches = find_rows(tracking, args.entry, args.doi)
    if not matches:
        sys.exit("No matching entries found — check the Entry number(s)/DOI(s) against the tracking sheet.")

    print(f"{len(matches)} entr{'y' if len(matches) == 1 else 'ies'} will be removed:")
    for _, row in matches:
        print(f"  Entry {row.get('Entry', '?')}: {row.get('Title', '')[:70]!r}  (DOI: {row.get('DOI') or '(none)'})")

    if not args.yes and input(
        "\nRemove these from the tracking sheet and search index? PDFs are moved to "
        f"{REMOVED_PDFS_DIR}/, not deleted. [Y/n] "
    ).strip().lower() == "n":
        print("Cancelled.")
        return

    db = build_index.open_db()
    os.makedirs(REMOVED_PDFS_DIR, exist_ok=True)

    for key, row in matches:
        index_key = normalize_doi(row.get("DOI", "")) or row.get("PDF_Path", "")
        if index_key:
            remove_from_index(db, index_key)

        pdf_path = row.get("PDF_Path", "")
        paths_to_move = [pdf_path] if pdf_path and os.path.exists(pdf_path) else []
        if pdf_path:
            # import_from_laptop.py attaches Supporting Information as
            # <same base>_SI.ext, _SI2.ext, ... next to the main PDF —
            # move those along too so nothing about a removed paper is
            # left behind.
            base, ext = os.path.splitext(pdf_path)
            n = 1
            while True:
                sibling = f"{base}{'_SI' if n == 1 else f'_SI{n}'}{ext}"
                if not os.path.exists(sibling):
                    break
                paths_to_move.append(sibling)
                n += 1

        for path in paths_to_move:
            dest = os.path.join(REMOVED_PDFS_DIR, os.path.basename(path))
            if os.path.exists(dest):
                dbase, dext = os.path.splitext(dest)
                dest = f"{dbase}_Entry{row.get('Entry', 'x')}{dext}"
            try:
                shutil.move(path, dest)
            except OSError as e:
                print(f"  could not move {os.path.basename(path)} for Entry {row.get('Entry', '?')}: {e}")

        del tracking[key]

    save_tracking(tracking_path, tracking)
    db.close()

    print(f"\nDone. {len(matches)} entr{'y' if len(matches) == 1 else 'ies'} removed from "
          f"{tracking_path} and the search index.")
    print(f"PDF file(s) moved to {REMOVED_PDFS_DIR}/ (not deleted) in case this was a mistake.")


if __name__ == "__main__":
    main()
