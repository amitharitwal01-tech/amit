"""Bring PDFs you already have — downloaded outside this pipeline, so
they never went through Stage 1/2 and aren't in your Excel input — into
the library properly: copied into downloads/, renamed to the same
<Entry>_<Category>_<Year>_<Title>.pdf scheme every other paper uses,
and appended as real rows in the tracking sheet.

Since these PDFs have no Excel row to source a title/DOI from, each one
is identified from its own content: the DOI is searched for on its
first pages first (fastest, most reliable), and if none is printed
there, the PDF's own title metadata or first substantial line of text
is used to search CrossRef by title instead. Whichever way the DOI is
found, the same CrossRef lookup and enrichment Stage 1 uses fills in
Year, Authors, Category, Key_Info, Abstract, and the corresponding
author's e-mail read off the PDF itself.

A paper already in your library (matched by DOI) is skipped, so this
is safe to run again over a folder you keep adding new PDFs to.

You will always be asked which folder to copy from — nothing is read
from config, so there's no stale path to forget about.

Usage:
    python import_existing_pdfs.py
    python import_existing_pdfs.py --folder "C:\path\to\pdfs" --yes

--folder/--yes are for driving this script non-interactively (e.g. from
the desktop app's own folder picker) — running it plain still asks for
the folder and a confirmation, exactly as before.
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

from doi_resolver import (
    TRACKING_FIELDS,
    build_pdf_filename,
    classify_topic,
    extract_pdf_contact_info,
    fetch_crossref_metadata,
    load_config,
    load_tracking,
    normalize_doi,
    resolve_doi_via_crossref,
    resolve_publisher_url,
    sanitize_filename,
    save_tracking,
    summarize_abstract,
)

# Matches a DOI as printed on an article's own first page, e.g. the
# "https://doi.org/10.1002/..." or "DOI: 10.1002/..." line publishers
# routinely include — independent of whatever normalize_doi() later
# does with a value coming from an Excel cell.
DOI_IN_TEXT_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"'<>()]+", re.IGNORECASE)


def extract_first_pages_text(pdf_path, pages=2):
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        return "".join((p.extract_text() or "") for p in reader.pages[:pages])
    except Exception:
        return ""


def find_doi_in_text(text):
    for match in DOI_IN_TEXT_PATTERN.finditer(text):
        candidate = normalize_doi(match.group(0))
        if candidate:
            return candidate
    return None


def guess_title_from_pdf(pdf_path, first_page_text):
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        meta_title = (reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:
        meta_title = ""
    # Generic titles some tools stamp on export aren't usable as a
    # search query.
    if meta_title and not re.match(r"^(microsoft word|untitled|document\d*)\b", meta_title, re.IGNORECASE):
        return meta_title

    for line in first_page_text.splitlines():
        line = line.strip()
        if len(line) >= 20 and not line.isupper():
            return line
    return ""


def title_from_filename(pdf_path):
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    stem = re.sub(r"^\d+[_\-]?", "", stem)  # a leading number is usually an entry id, not the title
    return re.sub(r"[_\-]+", " ", stem).strip()


def existing_dois(tracking):
    return {normalize_doi(row.get("DOI", "")) for row in tracking.values() if row.get("DOI")}


def next_entry_number(tracking):
    numbers = [int(row["Entry"]) for row in tracking.values() if str(row.get("Entry", "")).isdigit()]
    return (max(numbers) + 1) if numbers else 1


def import_one_pdf(pdf_path, session, email, timeout, known_dois):
    text = extract_first_pages_text(pdf_path)
    doi = find_doi_in_text(text)
    title = ""

    if not doi:
        title = guess_title_from_pdf(pdf_path, text) or title_from_filename(pdf_path)
        if title:
            found = resolve_doi_via_crossref(title, "", session, timeout)
            if found:
                doi = normalize_doi(found)

    if doi and normalize_doi(doi) in known_dois:
        return {"status": "duplicate", "doi": doi}

    meta = fetch_crossref_metadata(doi, session, timeout) if doi else {}
    if not title:
        title = meta.get("title", "") or title_from_filename(pdf_path)
    elif meta.get("title"):
        title = meta["title"]  # CrossRef's own title is more reliable than our guess

    record = {field: "" for field in TRACKING_FIELDS}
    record.update({
        "Title": title or "(title not resolved — please check manually)",
        "DOI": doi or "",
        "Year": meta.get("year", ""),
        "Last_Updated": datetime.now(timezone.utc).isoformat(),
    })
    if meta.get("authors"):
        record["Authors"] = "; ".join(a["name"] for a in meta["authors"])
    record["Abstract"] = meta.get("abstract", "") or "(no abstract deposited with CrossRef)"
    record["Key_Info"] = summarize_abstract(meta.get("abstract", ""))
    record["Category"] = classify_topic(title, meta.get("abstract", ""))

    if doi:
        record["Source_URL"] = f"https://doi.org/{doi}"
        record["Publisher_URL"] = resolve_publisher_url(doi, session, timeout)
        record["Status"] = "downloaded"
    else:
        record["Status"] = "manual_check_needed"
        record["Notes"] = "Imported from a local folder; no DOI could be found in the PDF or by title search — please verify Title/DOI/Year."

    author_entries = meta.get("authors") or [a.strip() for a in re.split(r"[;,]", record["Authors"]) if a.strip()]
    name, emails, institute = extract_pdf_contact_info(pdf_path, author_entries)
    record["Corresponding_Author"] = name
    record["Corresponding_Email"] = emails
    record["Institute"] = institute

    return {"status": "ok", "record": record}


def main():
    parser = argparse.ArgumentParser(description="Import manually-downloaded PDFs into the library.")
    parser.add_argument("--folder", help="source folder (skips the interactive prompt)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    config = load_config()
    tracking_path = config["paths"]["tracking_csv"]
    downloads_dir = config["paths"]["downloads_dir"]
    os.makedirs(downloads_dir, exist_ok=True)

    net_cfg = config.get("network", {})
    timeout = net_cfg.get("timeout_seconds", 30)
    delay = net_cfg.get("request_delay_seconds", 0.5)

    source_dir = args.folder or input("Folder to import PDFs from: ").strip().strip('"')
    if not source_dir or not os.path.isdir(source_dir):
        sys.exit(f"Not a folder: {source_dir!r}")

    candidates = sorted(
        os.path.join(source_dir, name)
        for name in os.listdir(source_dir)
        if name.lower().endswith(".pdf")
    )
    if not candidates:
        sys.exit(f"No .pdf files found directly in {source_dir}")

    print(f"\n{len(candidates)} PDF(s) found in {source_dir}:")
    for path in candidates:
        print(f"  {os.path.basename(path)}")
    if not args.yes and input(f"\nImport these {len(candidates)} file(s)? [Y/n] ").strip().lower() == "n":
        print("Cancelled.")
        return

    tracking = load_tracking(tracking_path)
    known_dois = existing_dois(tracking)
    entry_number = next_entry_number(tracking)

    session = requests.Session()
    email = config.get("unpaywall_email", "")
    session.headers.update({"User-Agent": f"paper-pipeline (mailto:{email})"})

    imported = skipped = flagged = 0

    for i, pdf_path in enumerate(candidates, 1):
        name = os.path.basename(pdf_path)
        print(f"[{i}/{len(candidates)}] {name[:70]!r}", end=" ... ", flush=True)

        try:
            result = import_one_pdf(pdf_path, session, email, timeout, known_dois)
        except Exception as e:
            print(f"error ({type(e).__name__}: {e}) — skipped")
            time.sleep(delay)
            continue

        if result["status"] == "duplicate":
            print(f"already in library (DOI {result['doi']}) — skipped")
            skipped += 1
            time.sleep(delay)
            continue

        record = result["record"]
        record["Entry"] = str(entry_number)
        dest_name = build_pdf_filename(entry_number, record["Category"], record["Year"], record["Title"], record["DOI"])
        dest_path = os.path.join(downloads_dir, dest_name)
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(dest_name)
            dest_path = os.path.join(downloads_dir, f"{base}_dup{ext}")

        try:
            import shutil
            shutil.copy2(pdf_path, dest_path)
        except OSError as e:
            print(f"could not copy file ({e}) — skipped")
            time.sleep(delay)
            continue

        record["PDF_Path"] = dest_path
        # Keyed by title, same as every other row — a blank/placeholder
        # title still gets a unique key via the entry number.
        tracking_key = record["Title"] if record["Title"] else f"(imported entry {entry_number})"
        tracking[tracking_key] = record
        if record["DOI"]:
            known_dois.add(normalize_doi(record["DOI"]))

        save_tracking(tracking_path, tracking)

        if record["Status"] == "manual_check_needed":
            print(f"imported as Entry {entry_number} (no DOI found — flagged for a manual check)")
            flagged += 1
        else:
            print(f"imported as Entry {entry_number}")
            imported += 1

        entry_number += 1
        time.sleep(delay)

    print()
    print(
        f"Done. {imported} paper(s) imported cleanly"
        + (f", {flagged} imported but flagged for a manual metadata check" if flagged else "")
        + (f", {skipped} already in the library (skipped)" if skipped else "")
        + f". See {tracking_path}."
    )


if __name__ == "__main__":
    main()
