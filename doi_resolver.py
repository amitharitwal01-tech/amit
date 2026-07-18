"""Stage 1: resolve DOIs for each paper in the Excel sheet and grab any
freely/legally available copy (via Unpaywall). Papers that have no open
access copy are left for Stage 2 (proxy_download.py).
"""
import csv
import html
import os
import re
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import yaml

CONFIG_PATH = "paper_pipeline_config.yaml"
TRACKING_FIELDS = [
    "Entry", "Title", "Authors", "DOI", "Year", "Category", "Status", "PDF_Path",
    "Source_URL", "Publisher_URL",
    "Corresponding_Author", "Corresponding_Email", "Institute",
    "Key_Info", "Abstract", "Notes", "Last_Updated",
]

DOI_PREFIXES_TO_STRIP = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"Config file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            sys.exit(
                f"Could not parse {CONFIG_PATH} — check for stray Tab "
                f"characters or bad indentation.\n{e}"
            )
    email = config.get("unpaywall_email", "")
    if not email or "@" not in email or email == "your_email@example.com":
        sys.exit(
            "Please set a real email address for 'unpaywall_email' in "
            f"{CONFIG_PATH} before running this script."
        )
    return config


def find_column(columns, wanted_name):
    wanted_lower = wanted_name.strip().lower()
    for col in columns:
        if str(col).strip().lower() == wanted_lower:
            return col
    return None


def load_excel_file(path, excel_cfg):
    # Normalizes one Excel file to fixed Title/Authors/DOI columns —
    # each file's actual column names are matched independently, so
    # files with different layouts can coexist in the input folder.
    df = pd.read_excel(path, sheet_name=excel_cfg.get("sheet_name", 0))
    doi_col = find_column(df.columns, excel_cfg.get("doi_column", "DOI"))
    title_col = find_column(df.columns, excel_cfg.get("title_column", "Title"))
    authors_col = find_column(df.columns, excel_cfg.get("authors_column", "Authors"))
    if title_col is None:
        return None
    return pd.DataFrame({
        "Title": df[title_col].astype(str),
        "Authors": df[authors_col].astype(str) if authors_col else "",
        "DOI": df[doi_col].astype(str) if doi_col else "",
    })


def load_input_rows(config):
    # Every .xlsx dropped into the input folder is read and combined
    # (sorted by filename, so publication_data_1/_2/_3 keeps a stable
    # order and new files append at the end). Falls back to the single
    # excel_input file when the folder isn't set up. Duplicate papers
    # across files (same DOI, or same title when no DOI) count once.
    excel_cfg = config.get("excel", {})
    paths_cfg = config["paths"]

    files = []
    input_dir = paths_cfg.get("excel_input_dir", "")
    if input_dir and os.path.isdir(input_dir):
        for name in sorted(os.listdir(input_dir)):
            # "~$..." are Excel's lock files for currently-open workbooks
            if name.lower().endswith((".xlsx", ".xls")) and not name.startswith("~$"):
                files.append(os.path.join(input_dir, name))

    if not files:
        single = paths_cfg.get("excel_input", "")
        if single and os.path.exists(single):
            files = [single]
    if not files:
        sys.exit(
            "No Excel input found. Either put .xlsx files in the "
            f"'{input_dir or 'publication_data'}' folder, or set "
            "'paths.excel_input' in the config."
        )

    frames = []
    for path in files:
        try:
            frame = load_excel_file(path, excel_cfg)
        except Exception as e:
            print(f"  (skipping {os.path.basename(path)}: {type(e).__name__}: {e})")
            continue
        if frame is None:
            print(f"  (skipping {os.path.basename(path)}: no Title column found)")
            continue
        print(f"  {os.path.basename(path)}: {len(frame)} row(s)")
        frames.append(frame)
    if not frames:
        sys.exit("None of the Excel files had a usable Title column.")

    combined = pd.concat(frames, ignore_index=True)
    seen = set()
    keep = []
    for _, row in combined.iterrows():
        title = str(row["Title"]).strip()
        if not title or title.lower() == "nan":
            keep.append(False)
            continue
        doi = normalize_doi(row["DOI"])
        key = ("doi", doi) if doi else ("title", title.lower())
        keep.append(key not in seen)
        seen.add(key)
    deduped = combined[pd.Series(keep, index=combined.index)].reset_index(drop=True)
    dropped = len(combined) - len(deduped)
    if dropped:
        print(f"  ({dropped} duplicate/empty row(s) across files ignored)")
    return deduped


def load_tracking(tracking_path):
    rows = {}
    if os.path.exists(tracking_path):
        # utf-8-sig transparently handles files both with and without
        # the BOM that save_tracking now writes.
        with open(tracking_path, "r", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                rows[row["Title"]] = row
    return rows


def save_tracking(tracking_path, rows):
    # utf-8-sig: without the BOM, Excel guesses a legacy encoding when
    # opening the CSV and renders "‐" as "â€" etc. — confirmed on a
    # real sheet. The BOM makes it read UTF-8 correctly.
    with open(tracking_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKING_FIELDS)
        writer.writeheader()
        for row in rows.values():
            writer.writerow({k: row.get(k, "") for k in TRACKING_FIELDS})

    # Also write a real .xlsx alongside the CSV — that's how the sheet
    # actually gets opened: everything on an "All" sheet, plus one sheet
    # per topic category. The CSV stays the source of truth (it's what
    # gets read back), so a failure here is not fatal.
    base, ext = os.path.splitext(tracking_path)
    try:
        df = pd.DataFrame(
            [{k: row.get(k, "") for k in TRACKING_FIELDS} for row in rows.values()]
        )
        with pd.ExcelWriter(base + ".xlsx") as writer:
            df.to_excel(writer, sheet_name="All", index=False)
            for category, group in df.groupby("Category"):
                if str(category).strip():
                    group.to_excel(writer, sheet_name=str(category)[:31], index=False)
    except Exception:
        pass


def normalize_doi(raw):
    doi = str(raw or "").strip()
    if not doi or doi.lower() == "nan":
        return ""
    for prefix in DOI_PREFIXES_TO_STRIP:
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    doi = doi.strip().strip(".,;: \t")
    # A trailing .s001/.s002/... marks a CrossRef *component* DOI — the
    # Supporting Information file, not the paper (ACS especially). It
    # resolves straight to the SI PDF, so a pipeline fed one downloads
    # the supplement thinking it's the article. Always aim at the main
    # article's DOI instead.
    return re.sub(r"\.s\d{3}$", "", doi, flags=re.IGNORECASE)


def resolve_publisher_url(doi, session, timeout):
    # Following https://doi.org/<doi> to its final redirect reveals the
    # exact article URL (domain *and* path — Wiley in particular has
    # dozens of journal-imprint subdomains, e.g.
    # advanced.onlinelibrary.wiley.com) without needing to log in: the
    # redirect itself is public, only the content behind it is gated.
    # Streamed and closed immediately so we get the final URL without
    # downloading the whole landing page body.
    # except Exception, not just RequestException: some redirect chains
    # emit genuinely malformed URLs (seen live: a Web of Science login
    # loop producing host "www.webofknowledge.comundefinednull..."),
    # which raises a urllib3 parse error that requests doesn't wrap.
    try:
        resp = session.get(
            f"https://doi.org/{doi}", timeout=timeout, allow_redirects=True, stream=True
        )
        resp.close()
        return resp.url or ""
    except Exception:
        return ""


def sanitize_filename(text, max_len=120):
    text = re.sub(r"[^\w\s-]", "", str(text)).strip()
    text = re.sub(r"[\s]+", "_", text)
    return text[:max_len] if text else "untitled"


EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# Typographic characters that display as "â€"-style garbage when the
# sheet is opened with the wrong encoding assumption, mapped to plain
# ASCII equivalents that survive anywhere. (Scientific symbols like π
# are kept — the utf-8-sig BOM on the CSV makes Excel read those right.)
UNICODE_PUNCTUATION = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "−": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    " ": " ", " ": " ", " ": " ",
}


def strip_jats_markup(text):
    # CrossRef returns abstracts as JATS XML ("<jats:p>...</jats:p>"),
    # usually leading with a literal "Abstract" heading.
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    for char, replacement in UNICODE_PUNCTUATION.items():
        text = text.replace(char, replacement)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^abstract\s*[:.]?\s*", "", text, flags=re.IGNORECASE)


# Abstracts open with background ("X has been sought after and
# debated..."); the paper's actual contribution starts at a marker
# sentence like "Here, we report...". That's the key information.
CONTRIBUTION_MARKERS = re.compile(
    r"\bhere,?\s+we\b"
    r"|\bwe (?:report|introduce|show|demonstrate|develop|present|propose|reveal|achieve|establish)\b"
    r"|\bin this (?:work|study|paper)\b"
    r"|\bthis (?:work|study) (?:reports|introduces|shows|demonstrates|presents|reveals)\b",
    re.IGNORECASE,
)


def summarize_abstract(text, count=2):
    if not text:
        return ""
    # Split only where a capital letter follows the terminator, so
    # decimals ("1.5 eV") don't end a sentence early.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    for idx, sentence in enumerate(sentences):
        if CONTRIBUTION_MARKERS.search(sentence):
            return " ".join(sentences[idx:idx + count]).strip()
    return " ".join(sentences[:count]).strip()


# Checked in order — first match wins, so the dominant topic of the
# corpus comes first (a tandem-solar-with-LED-readout paper counts as
# solar). Nothing matching any pattern is filed as "fundamental".
CATEGORY_PATTERNS = (
    ("solar-cell", re.compile(r"solar cell|photovoltaic|power conversion efficiency|\btandem\b", re.IGNORECASE)),
    ("LED", re.compile(r"light[- ]emitting diode|electroluminescen|\bleds?\b", re.IGNORECASE)),
    ("X-ray", re.compile(r"x[- ]?ray|scintillat|radiation detect", re.IGNORECASE)),
    ("photodetector", re.compile(r"photodetector|photodiode", re.IGNORECASE)),
    ("laser", re.compile(r"\blas(?:er|ing)\b", re.IGNORECASE)),
)


def classify_topic(title, abstract=""):
    text = f"{title} {abstract}"
    for label, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return label
    return "fundamental"


def fetch_crossref_metadata(doi, session, timeout):
    # Publication year, author list (with affiliations where CrossRef
    # has them) and the abstract, all from CrossRef's free public API —
    # no login or key needed.
    try:
        resp = session.get(f"https://api.crossref.org/works/{doi}", timeout=timeout)
        resp.raise_for_status()
        message = resp.json().get("message", {})
    except (requests.RequestException, ValueError):
        return {}

    year = ""
    for field in ("issued", "published-print", "published-online", "created"):
        parts = (message.get(field) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = str(parts[0][0])
            break

    authors = []
    for a in message.get("author", []) or []:
        name = " ".join(part for part in (a.get("given"), a.get("family")) if part)
        if not name:
            continue
        affiliations = [aff.get("name", "") for aff in (a.get("affiliation") or []) if aff.get("name")]
        authors.append({
            "name": name,
            "family": a.get("family", ""),
            "affiliation": affiliations[0] if affiliations else "",
        })

    return {
        "year": year,
        "authors": authors,
        "abstract": strip_jats_markup(message.get("abstract", "")),
    }


def extract_pdf_contact_info(pdf_path, author_entries):
    # Best-effort corresponding-author details, read from the PDF
    # itself: publishers print the contact email on the first page(s),
    # and the matching author is identified by their family name
    # appearing in the email address. Accepts author entries as dicts
    # (from CrossRef) or plain "Given Family" strings.
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        text = "".join((p.extract_text() or "") for p in reader.pages[:2])
    except Exception:
        return "", "", ""

    emails = []
    for email in EMAIL_PATTERN.findall(text):
        email = email.strip(".")
        if email.lower() not in (e.lower() for e in emails):
            emails.append(email)
    if not emails:
        return "", "", ""

    normalized = []
    for entry in author_entries or []:
        if isinstance(entry, str):
            tokens = entry.split()
            entry = {"name": entry, "family": tokens[-1] if tokens else "", "affiliation": ""}
        normalized.append(entry)

    name = affiliation = ""
    for entry in normalized:
        family = (entry.get("family") or "").lower()
        if len(family) >= 3 and any(family in e.split("@")[0].lower() for e in emails):
            name = entry.get("name", "")
            affiliation = entry.get("affiliation", "")
            break
    if not affiliation:
        affiliation = next((e.get("affiliation", "") for e in normalized if e.get("affiliation")), "")
    return name, "; ".join(emails), affiliation


def build_pdf_filename(entry, category, year, title, doi=""):
    # "<entry>_<category>_<year>_<title>.pdf", degrading gracefully when
    # a part is unknown — all the way down to the old DOI-based name if
    # nothing else is available.
    parts = [str(p).strip() for p in (entry, category, year) if str(p or "").strip()]
    title_part = sanitize_filename(title, max_len=80) if title else ""
    if title_part and title_part != "untitled":
        parts.append(title_part)
    if not parts:
        return sanitize_filename((doi or "paper").replace("/", "_")) + ".pdf"
    return "_".join(parts) + ".pdf"


def enrich_record(record, entry_number, session, timeout, downloads_dir):
    # Fill the metadata columns for a row that already went through the
    # pipeline (only blanks are filled), and move its saved PDF onto the
    # <entry>_<year>_<title> naming scheme — so re-running Stage 1
    # upgrades the existing library in place without re-downloading.
    record["Entry"] = str(entry_number)
    doi = record.get("DOI", "")

    # A Key_Info without a contribution marker is either missing or the
    # old background-style summary — re-fetch the abstract and upgrade
    # it to the "Here, we report..." form when the abstract has one.
    key_info_needs_upgrade = not CONTRIBUTION_MARKERS.search(record.get("Key_Info") or "")

    meta = {}
    if doi and (not record.get("Year") or key_info_needs_upgrade or not record.get("Abstract")):
        meta = fetch_crossref_metadata(doi, session, timeout)
    if not record.get("Year"):
        record["Year"] = meta.get("year", "")
    if not record.get("Authors") and meta.get("authors"):
        record["Authors"] = "; ".join(a["name"] for a in meta["authors"])
    if key_info_needs_upgrade:
        new_summary = summarize_abstract(meta.get("abstract", ""))
        if new_summary:
            record["Key_Info"] = new_summary
    if not record.get("Abstract"):
        # The sentinel stops this row triggering a CrossRef re-fetch on
        # every future run when the publisher simply deposits no abstract.
        record["Abstract"] = meta.get("abstract", "") or "(no abstract deposited with CrossRef)"
    if not record.get("Category"):
        record["Category"] = classify_topic(
            record.get("Title", ""), meta.get("abstract", "") or record.get("Key_Info", "")
        )

    pdf_path = record.get("PDF_Path", "")
    if pdf_path and os.path.exists(pdf_path):
        if not record.get("Corresponding_Email"):
            authors = meta.get("authors") or [
                a.strip() for a in re.split(r"[;,]", record.get("Authors") or "") if a.strip()
            ]
            name, emails, institute = extract_pdf_contact_info(pdf_path, authors)
            if name and not record.get("Corresponding_Author"):
                record["Corresponding_Author"] = name
            if emails:
                record["Corresponding_Email"] = emails
            if institute and not record.get("Institute"):
                record["Institute"] = institute

        desired_path = os.path.join(
            downloads_dir,
            build_pdf_filename(
                record["Entry"], record.get("Category", ""), record.get("Year", ""),
                record.get("Title", ""), doi,
            ),
        )
        if os.path.abspath(desired_path) != os.path.abspath(pdf_path) and not os.path.exists(desired_path):
            try:
                os.rename(pdf_path, desired_path)
                record["PDF_Path"] = desired_path
            except OSError:
                pass


def resolve_doi_via_crossref(title, authors, session, timeout):
    query = str(title)
    if authors:
        query += f" {authors}"
    try:
        resp = session.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": query, "rows": 1},
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
        if items:
            return items[0].get("DOI")
    except requests.RequestException:
        pass
    return None


def query_unpaywall(doi, email, session, timeout):
    try:
        resp = session.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": email},
            timeout=timeout,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def best_oa_pdf_url(unpaywall_data):
    if not unpaywall_data or not unpaywall_data.get("is_oa"):
        return None
    best = unpaywall_data.get("best_oa_location") or {}
    return best.get("url_for_pdf") or best.get("url")


def download_pdf(url, dest_path, session, timeout):
    try:
        resp = session.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            return False
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except requests.RequestException:
        return False


def format_remaining(seconds):
    if seconds < 60:
        return "<1m"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"~{minutes}m"
    return f"~{minutes // 60}h {minutes % 60:02d}m"


def main():
    config = load_config()
    print("Reading paper list(s):")
    df = load_input_rows(config)

    downloads_dir = config["paths"]["downloads_dir"]
    tracking_path = config["paths"]["tracking_csv"]
    os.makedirs(downloads_dir, exist_ok=True)

    net_cfg = config.get("network", {})
    delay = net_cfg.get("request_delay_seconds", 1.5)
    timeout = net_cfg.get("timeout_seconds", 30)
    email = config["unpaywall_email"]

    session = requests.Session()
    session.headers.update({"User-Agent": f"paper-pipeline (mailto:{email})"})

    tracking = load_tracking(tracking_path)

    downloaded_count = 0
    needs_proxy_count = 0
    total = len(df)

    limits_cfg = config.get("limits", {}) or {}
    stage1_limit = int(limits_cfg.get("stage1_papers_per_run", 2000) or 0)

    error_count = 0
    started = time.monotonic()
    handled = 0
    fresh_processed = 0
    limit_reached = False

    try:
        for i, row in df.iterrows():
            # Periodic save so a crash or Ctrl+C at paper 763 of 5000
            # doesn't lose the first 762 papers' work — the run resumes
            # from the saved sheet.
            if i and i % 25 == 0:
                save_tracking(tracking_path, tracking)

            # Remaining-time estimate from this session's own pace —
            # skipped rows are near-instant and full lookups a few
            # seconds, so the average self-corrects as the mix changes.
            eta = ""
            if handled >= 5:
                per_row = (time.monotonic() - started) / handled
                eta = f", {format_remaining(per_row * (total - i))} left"
            handled += 1

            title = str(row["Title"]).strip()
            authors = str(row["Authors"]).strip()
            if authors.lower() == "nan":
                authors = ""
            existing_doi = normalize_doi(row["DOI"])

            tracked = tracking.get(title)
            if tracked:
                doi_now = existing_doi or normalize_doi(tracked.get("DOI", ""))
                same_doi = doi_now == tracked.get("DOI", "")
                status = tracked.get("Status", "")
                # Skip papers already downloaded (by either stage) — but
                # only if the DOI we'd use now matches the one that
                # download was actually for. A DOI that normalization now
                # corrects (e.g. a supplementary .s001 component stripped
                # to the article's own DOI) means the saved file was the
                # wrong document, so that paper goes through again.
                if same_doi and status in ("downloaded", "downloaded_via_proxy"):
                    print(f"[{i + 1}/{total}{eta}] {title[:70]!r} — already downloaded, updating info columns")
                    enrich_record(tracked, i + 1, session, timeout, downloads_dir)
                    downloaded_count += 1
                    continue
                # Fast resume for a batch run that stopped partway:
                # rows already checked and waiting for Stage 2 don't
                # need their lookups repeated.
                if same_doi and status == "needs_proxy":
                    print(f"[{i + 1}/{total}{eta}] {title[:70]!r} — already checked (needs proxy), skipping")
                    needs_proxy_count += 1
                    continue
                # A title that had no DOI last time won't gain one by
                # asking again — unless the Excel now provides it.
                if status == "no_doi_found" and not existing_doi:
                    print(f"[{i + 1}/{total}{eta}] {title[:70]!r} — still no DOI, skipping")
                    continue

            # The per-run limit counts only fresh lookups — skipped
            # rows fly by for free, so each run handles the *next*
            # stage1_papers_per_run unprocessed papers.
            if stage1_limit and fresh_processed >= stage1_limit:
                limit_reached = True
                break
            fresh_processed += 1

            print(f"[{i + 1}/{total}{eta}] {title[:70]!r}", end=" ... ", flush=True)

            record = {field: "" for field in TRACKING_FIELDS}
            record.update({
                "Entry": str(i + 1),
                "Title": title,
                "Authors": authors,
                "Last_Updated": datetime.now(timezone.utc).isoformat(),
            })

            # One paper must never kill a batch run: whatever goes wrong
            # here is recorded on the row (status "error" retries on the
            # next run) and the loop moves on.
            try:
                doi = existing_doi
                if not doi:
                    doi = normalize_doi(resolve_doi_via_crossref(title, authors, session, timeout))
                record["DOI"] = doi or ""

                if not doi:
                    record["Status"] = "no_doi_found"
                    record["Notes"] = "Could not resolve a DOI from title/authors"
                    print("no DOI found")
                    tracking[title] = record
                    time.sleep(delay)
                    continue

                record["Source_URL"] = f"https://doi.org/{doi}"
                record["Publisher_URL"] = resolve_publisher_url(doi, session, timeout)

                # Year / author list / abstract summary from CrossRef —
                # fetched up front so the file can be named properly.
                meta = fetch_crossref_metadata(doi, session, timeout)
                record["Year"] = meta.get("year", "")
                if not record["Authors"] and meta.get("authors"):
                    record["Authors"] = "; ".join(a["name"] for a in meta["authors"])
                record["Key_Info"] = summarize_abstract(meta.get("abstract", ""))
                record["Abstract"] = meta.get("abstract", "") or "(no abstract deposited with CrossRef)"
                record["Category"] = classify_topic(title, meta.get("abstract", ""))

                unpaywall_data = query_unpaywall(doi, email, session, timeout)
                pdf_url = best_oa_pdf_url(unpaywall_data)

                if pdf_url:
                    dest_path = os.path.join(
                        downloads_dir,
                        build_pdf_filename(record["Entry"], record["Category"], record["Year"], title, doi),
                    )
                    if download_pdf(pdf_url, dest_path, session, timeout):
                        record["Status"] = "downloaded"
                        record["PDF_Path"] = dest_path
                        author_entries = meta.get("authors") or [
                            a.strip() for a in re.split(r"[;,]", record["Authors"] or "") if a.strip()
                        ]
                        name, emails, institute = extract_pdf_contact_info(dest_path, author_entries)
                        record["Corresponding_Author"] = name
                        record["Corresponding_Email"] = emails
                        record["Institute"] = institute
                        downloaded_count += 1
                        print("downloaded (open access)")
                    else:
                        record["Status"] = "needs_proxy"
                        record["Notes"] = f"Open access URL found but download failed: {pdf_url}"
                        needs_proxy_count += 1
                        print("needs proxy (OA download failed)")
                else:
                    record["Status"] = "needs_proxy"
                    record["Notes"] = "No open access copy found via Unpaywall"
                    needs_proxy_count += 1
                    print("needs proxy (no open access copy)")
            except Exception as e:
                record["Status"] = "error"
                record["Notes"] = f"Stage 1 error (will retry on next run): {type(e).__name__}: {e}"
                error_count += 1
                print(f"error ({type(e).__name__}) — recorded, moving on")

            tracking[title] = record
            time.sleep(delay)
    finally:
        save_tracking(tracking_path, tracking)

    print()
    if limit_reached:
        print(f"Per-run limit reached ({stage1_limit} fresh papers this run) — "
              "run the script again to continue with the rest.")
    print(f"Done. {downloaded_count} downloaded directly, {needs_proxy_count} need the university proxy"
          + (f", {error_count} error(s) to retry on the next run" if error_count else "") + ".")
    print(f"See {tracking_path} for the full breakdown.")


if __name__ == "__main__":
    main()
