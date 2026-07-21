"""Find every research-paper PDF already on your laptop — mainly meant
for a OneDrive folder full of years of downloaded papers — and bring
them into the library the same way import_existing_pdfs.py does, but
for many scattered files instead of one folder you point it at.

Two things make a whole-laptop sweep different from that simpler
folder-based import:

  1. Most PDFs found this way are NOT research papers (invoices, scans,
     slide decks, forms...). Every candidate is read and classified —
     it only counts as a main article if it has a printed DOI, or looks
     like a paper (an "Abstract" and enough pages). Anything else is
     left alone, untouched, and just listed in a log for you to check.

  2. Papers are often saved together with a separate Supporting
     Information PDF. An SI file is never imported as if it were its
     own paper (it would either fail to resolve, or — worse — get
     silently discarded as a "duplicate" of the main paper, since many
     SI files print the same DOI with a .sNNN suffix). Instead this
     script matches each SI file to its main article (by that DOI
     suffix, or by being the one other PDF in the same folder) and
     copies it in next to that entry's PDF as ..._SI.pdf, so nothing
     about the paper is lost, without inventing an extra library entry
     for it.

Nothing is ever moved or deleted from where it already lives — only
copied. Everything is a dry-run report first; nothing is copied until
you confirm (or pass --yes).

Usage:
    python import_from_laptop.py
    python import_from_laptop.py --roots "C:\\Users\\me\\OneDrive" "D:\\Papers"
    python import_from_laptop.py --dry-run     # just report, copy nothing
    python import_from_laptop.py --yes         # skip confirmation prompts
"""
import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

from doi_resolver import load_config, load_tracking, normalize_doi
from import_existing_pdfs import (
    DOI_IN_TEXT_PATTERN,
    existing_dois,
    extract_first_pages_text,
    import_pdf_list,
)

# Directory names that are never worth walking into, wherever they
# appear — junk, version control, caches, or huge OS/system trees that
# don't contain anyone's papers.
JUNK_DIR_NAMES = {
    "node_modules", ".git", ".svn", "__pycache__", "$recycle.bin",
    "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "appdata", ".venv", "venv",
    "pipeline_env", "site-packages",
}

SI_FILENAME_HINTS = re.compile(r"(support|supplementary|\bsi\b|_si[_.\-]|appendix)", re.IGNORECASE)
SI_TEXT_HINTS = re.compile(r"supporting information|supplementary (material|information|data|table|figure)", re.IGNORECASE)
COMPONENT_DOI_SUFFIX = re.compile(r"\.s\d+$", re.IGNORECASE)
ABSTRACT_HINT = re.compile(r"\babstract\b", re.IGNORECASE)


def default_scan_roots():
    """OneDrive (personal or work/school) plus the usual document
    folders, whichever of these actually exist — never the whole
    drive."""
    roots = []
    for env_var in ("OneDriveConsumer", "OneDriveCommercial", "OneDrive"):
        value = os.environ.get(env_var)
        if value and os.path.isdir(value) and value not in roots:
            roots.append(value)
    home = Path.home()
    for folder_name in ("Documents", "Downloads", "Desktop"):
        candidate = str(home / folder_name)
        if os.path.isdir(candidate) and candidate not in roots:
            roots.append(candidate)
    return roots


def resolve_roots(args_roots):
    if args_roots:
        return [r.strip().strip('"') for r in args_roots if r.strip()]
    suggested = default_scan_roots()
    if suggested:
        print("Found these folders automatically:")
        for r in suggested:
            print(f"  {r}")
        raw = input(
            "\nPress Enter to scan all of these, or type your own folder path(s) "
            "separated by semicolons: "
        ).strip()
    else:
        raw = input("Folder(s) to scan, separated by semicolons: ").strip()
    if not raw:
        return suggested
    return [p.strip().strip('"') for p in raw.split(";") if p.strip()]


def iter_pdf_paths(roots, exclude_dirs):
    """Yield every .pdf under roots, pruning junk/excluded folders and
    de-duplicating files reachable from more than one root (OneDrive
    sync folders and shortcuts do this often)."""
    exclude_abs = [os.path.abspath(p) for p in exclude_dirs]
    seen_real_paths = set()
    for root in roots:
        if not os.path.isdir(root):
            print(f"Skipping (not a folder): {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            abs_dirpath = os.path.abspath(dirpath)
            if any(abs_dirpath == p or abs_dirpath.startswith(p + os.sep) for p in exclude_abs):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d.lower() not in JUNK_DIR_NAMES]
            for name in filenames:
                if not name.lower().endswith(".pdf"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    real = os.path.realpath(path)
                except OSError:
                    real = path
                if real in seen_real_paths:
                    continue
                seen_real_paths.add(real)
                yield path


def raw_dois_in_text(text):
    for match in DOI_IN_TEXT_PATTERN.finditer(text):
        yield match.group(0).rstrip(").,;]\"'")


def page_count_of(pdf_path):
    try:
        from pypdf import PdfReader
        return len(PdfReader(pdf_path).pages)
    except Exception:
        return None


def classify_pdf(pdf_path, min_pages):
    """Decide whether a PDF is a main article, a Supporting Information
    file, or not a research paper at all — never guesses when unsure;
    "not a paper" is the safe default."""
    try:
        text = extract_first_pages_text(pdf_path, pages=3)
    except Exception as e:
        return {"kind": "error", "reason": f"{type(e).__name__}: {e}"}

    if not text or len(text.strip()) < 40:
        return {"kind": "skip", "reason": "no extractable text (scanned image only, or not a document)"}

    raw_dois = list(raw_dois_in_text(text))
    component = next((d for d in raw_dois if COMPONENT_DOI_SUFFIX.search(d)), None)
    doi = normalize_doi(component) if component else (normalize_doi(raw_dois[0]) if raw_dois else None)

    if component or SI_FILENAME_HINTS.search(os.path.basename(pdf_path)) or SI_TEXT_HINTS.search(text):
        return {"kind": "si", "parent_doi": doi, "reason": "component DOI" if component else "filename/text says Supporting Information"}

    if doi or (ABSTRACT_HINT.search(text[:4000]) and (page_count_of(pdf_path) or 0) >= min_pages):
        return {"kind": "main", "doi": doi}

    return {"kind": "skip", "reason": "doesn't look like a research article"}


def write_csv_log(log_dir, filename, rows, fieldnames):
    if not rows:
        return None
    os.makedirs(log_dir, exist_ok=True)
    out_path = os.path.join(log_dir, filename)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Scan your laptop for research-paper PDFs and import them.")
    parser.add_argument("--roots", nargs="*", help="folder(s) to scan (default: auto-detected OneDrive/Documents/"
                        "Downloads/Desktop, or asked interactively)")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    parser.add_argument("--dry-run", action="store_true", help="only report what would be imported; copies nothing")
    parser.add_argument("--min-pages", type=int, default=4,
                        help="minimum page count for a PDF with no DOI to still count as a paper (default 4)")
    parser.add_argument("--out", help="folder for the scan's log files (default: laptop_scan_logs/)")
    args = parser.parse_args()

    config = load_config()
    tracking_path = config["paths"]["tracking_csv"]
    downloads_dir = config["paths"]["downloads_dir"]
    os.makedirs(downloads_dir, exist_ok=True)
    net_cfg = config.get("network", {})
    timeout = net_cfg.get("timeout_seconds", 30)
    delay = net_cfg.get("request_delay_seconds", 0.5)

    roots = [r for r in resolve_roots(args.roots) if os.path.isdir(r)]
    if not roots:
        sys.exit("No valid folders to scan.")

    print("\nScanning these folders (a large OneDrive can take a while — reading every PDF's first pages):")
    for r in roots:
        print(f"  {r}")
    if not args.yes and input("\nProceed with the scan? [Y/n] ").strip().lower() == "n":
        print("Cancelled.")
        return

    exclude_dirs = [downloads_dir, "library_index", "browser_profile", "catalogs",
                     "research_packs", "answers", "finalized", "pipeline_env", "debug"]

    print("\nFinding PDF files...")
    all_pdfs = list(iter_pdf_paths(roots, exclude_dirs))
    print(f"Found {len(all_pdfs)} PDF file(s). Reading each one to identify research articles...")

    by_dir = defaultdict(list)
    classified = {}
    t0 = time.time()
    for i, path in enumerate(all_pdfs, 1):
        classified[path] = classify_pdf(path, args.min_pages)
        by_dir[os.path.dirname(path)].append(path)
        if i % 50 == 0 or i == len(all_pdfs):
            elapsed = time.time() - t0
            print(f"  [{i}/{len(all_pdfs)}] scanned ({elapsed:.0f}s elapsed)", flush=True)

    main_paths = [p for p, info in classified.items() if info["kind"] == "main"]
    si_paths = [p for p, info in classified.items() if info["kind"] == "si"]
    skipped = [(p, classified[p]["reason"]) for p in classified if classified[p]["kind"] in ("skip", "error")]

    print(f"\n{len(main_paths)} look like main research articles.")
    print(f"{len(si_paths)} look like Supporting Information files.")
    print(f"{len(skipped)} don't look like research papers (left untouched).")

    log_dir = args.out or "laptop_scan_logs"

    if args.dry_run:
        skip_log = write_csv_log(
            log_dir, "not_papers.csv",
            [{"Path": p, "Reason": reason} for p, reason in skipped], ["Path", "Reason"],
        )
        print("\nDry run — nothing copied or imported.")
        if skip_log:
            print(f"Full list of skipped files: {skip_log}")
        print("Re-run without --dry-run (or with --yes) to actually import.")
        return

    if not args.yes and input(
        f"\nImport the {len(main_paths)} main article(s) and try to attach the "
        f"{len(si_paths)} Supporting Information file(s)? [Y/n] "
    ).strip().lower() == "n":
        print("Cancelled.")
        return

    tracking = load_tracking(tracking_path)
    known_dois = existing_dois(tracking)
    session = requests.Session()
    email = config.get("unpaywall_email", "")
    session.headers.update({"User-Agent": f"paper-pipeline (mailto:{email})"})

    print(f"\nImporting {len(main_paths)} main article(s)...")
    imported, flagged, duplicate, entry_by_source = import_pdf_list(
        main_paths, tracking, tracking_path, downloads_dir, session, email, timeout, delay, known_dois
    )
    print(f"Done: {imported} imported, {flagged} flagged for manual check, {duplicate} already in the library.")

    print(f"\nAttaching {len(si_paths)} Supporting Information file(s)...")
    doi_to_row = {normalize_doi(row.get("DOI", "")): row for row in tracking.values() if row.get("DOI")}
    attached, unmatched = [], []
    si_counter = defaultdict(int)

    for si_path in si_paths:
        info = classified[si_path]
        row, method = None, ""

        parent_doi = info.get("parent_doi")
        if parent_doi and normalize_doi(parent_doi) in doi_to_row:
            row = doi_to_row[normalize_doi(parent_doi)]
            method = "matching DOI"
        else:
            siblings = [p for p in by_dir.get(os.path.dirname(si_path), []) if classified.get(p, {}).get("kind") == "main"]
            if len(siblings) == 1:
                sib_doi = entry_by_source.get(siblings[0])
                if sib_doi and normalize_doi(sib_doi) in doi_to_row:
                    row = doi_to_row[normalize_doi(sib_doi)]
                    method = "only main article in the same folder"

        if not row or not row.get("PDF_Path") or not os.path.exists(row["PDF_Path"]):
            unmatched.append({"Original_Path": si_path, "Reason": "could not confidently match to a main article"})
            continue

        si_counter[row["PDF_Path"]] += 1
        n = si_counter[row["PDF_Path"]]
        base, ext = os.path.splitext(row["PDF_Path"])
        dest = f"{base}{'_SI' if n == 1 else f'_SI{n}'}{ext}"
        try:
            import shutil
            shutil.copy2(si_path, dest)
        except OSError as e:
            unmatched.append({"Original_Path": si_path, "Reason": f"could not copy: {e}"})
            continue
        attached.append({
            "Entry": row.get("Entry", ""), "Title": row.get("Title", ""), "DOI": row.get("DOI", ""),
            "Original_SI_Path": si_path, "Saved_SI_Path": dest, "Match_Method": method,
        })
        print(f"  attached to Entry {row.get('Entry', '?')}: {os.path.basename(si_path)[:60]!r}")

    attached_log = write_csv_log(
        log_dir, "attached_supporting_info.csv",
        attached, ["Entry", "Title", "DOI", "Original_SI_Path", "Saved_SI_Path", "Match_Method"],
    )
    unmatched_log = write_csv_log(
        log_dir, "unmatched_supporting_info.csv",
        unmatched, ["Original_Path", "Reason"],
    )
    skip_log = write_csv_log(
        log_dir, "not_papers.csv",
        [{"Path": p, "Reason": reason} for p, reason in skipped], ["Path", "Reason"],
    )

    print()
    print(
        f"Done. {imported} paper(s) imported"
        + (f", {flagged} flagged for a manual metadata check" if flagged else "")
        + (f", {duplicate} already in the library" if duplicate else "")
        + f". {len(attached)} Supporting Information file(s) attached"
        + (f", {len(unmatched)} could not be matched automatically" if unmatched else "")
        + "."
    )
    if attached_log:
        print(f"Attached SI details: {attached_log}")
    if unmatched_log:
        print(f"SI files needing a manual look: {unmatched_log}")
    if skip_log:
        print(f"Full list of non-paper PDFs found (untouched): {skip_log}")


if __name__ == "__main__":
    main()
