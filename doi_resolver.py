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
    "Entry", "Title", "Authors", "DOI", "Year", "Status", "PDF_Path",
    "Source_URL", "Publisher_URL",
    "Corresponding_Author", "Corresponding_Email", "Institute",
    "Key_Info", "Notes", "Last_Updated",
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


def load_excel(config):
    excel_cfg = config.get("excel", {})
    excel_path = config["paths"]["excel_input"]
    if not os.path.exists(excel_path):
        sys.exit(f"Excel file not found: {excel_path}")

    sheet_name = excel_cfg.get("sheet_name", 0)
    df = pd.read_excel(excel_path, sheet_name=sheet_name)

    doi_col = find_column(df.columns, excel_cfg.get("doi_column", "DOI"))
    title_col = find_column(df.columns, excel_cfg.get("title_column", "Title"))
    authors_col = find_column(df.columns, excel_cfg.get("authors_column", "Authors"))

    if title_col is None:
        sys.exit(
            "Could not find a Title column in the Excel file. "
            "Check 'excel.title_column' in the config."
        )
    return df, doi_col, title_col, authors_col


def load_tracking(tracking_path):
    rows = {}
    if os.path.exists(tracking_path):
        with open(tracking_path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["Title"]] = row
    return rows


def save_tracking(tracking_path, rows):
    with open(tracking_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKING_FIELDS)
        writer.writeheader()
        for row in rows.values():
            writer.writerow({k: row.get(k, "") for k in TRACKING_FIELDS})

    # Also write a real .xlsx alongside the CSV — that's how the sheet
    # actually gets opened. The CSV stays the source of truth (it's what
    # gets read back), so a failure here is not fatal.
    base, ext = os.path.splitext(tracking_path)
    try:
        pd.DataFrame(
            [{k: row.get(k, "") for k in TRACKING_FIELDS} for row in rows.values()]
        ).to_excel(base + ".xlsx", index=False)
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
    try:
        resp = session.get(
            f"https://doi.org/{doi}", timeout=timeout, allow_redirects=True, stream=True
        )
        resp.close()
        return resp.url or ""
    except requests.RequestException:
        return ""


def sanitize_filename(text, max_len=120):
    text = re.sub(r"[^\w\s-]", "", str(text)).strip()
    text = re.sub(r"[\s]+", "_", text)
    return text[:max_len] if text else "untitled"


EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def strip_jats_markup(text):
    # CrossRef returns abstracts as JATS XML ("<jats:p>...</jats:p>"),
    # usually leading with a literal "Abstract" heading.
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^abstract\s*[:.]?\s*", "", text, flags=re.IGNORECASE)


def first_sentences(text, count=2):
    if not text:
        return ""
    # Split only where a capital letter follows the terminator, so
    # decimals ("1.5 eV") don't end a sentence early.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return " ".join(sentences[:count]).strip()


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


def build_pdf_filename(entry, year, title, doi=""):
    # "<entry>_<year>_<title>.pdf", degrading gracefully when a part is
    # unknown — all the way down to the old DOI-based name if nothing
    # else is available.
    parts = [str(p).strip() for p in (entry, year) if str(p or "").strip()]
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

    meta = {}
    if doi and (not record.get("Year") or not record.get("Key_Info")):
        meta = fetch_crossref_metadata(doi, session, timeout)
    if not record.get("Year"):
        record["Year"] = meta.get("year", "")
    if not record.get("Authors") and meta.get("authors"):
        record["Authors"] = "; ".join(a["name"] for a in meta["authors"])
    if not record.get("Key_Info"):
        record["Key_Info"] = first_sentences(meta.get("abstract", ""))

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
            build_pdf_filename(record["Entry"], record.get("Year", ""), record.get("Title", ""), doi),
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


def main():
    config = load_config()
    df, doi_col, title_col, authors_col = load_excel(config)

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

    for i, row in df.iterrows():
        title = str(row[title_col]).strip()
        authors = str(row[authors_col]).strip() if authors_col else ""
        existing_doi = normalize_doi(row[doi_col]) if doi_col else ""

        tracked = tracking.get(title)
        if tracked and tracked["Status"] in ("downloaded", "downloaded_via_proxy"):
            # Skip papers already downloaded (by either stage) — but only
            # if the DOI we'd use now matches the one that download was
            # actually for. A DOI that normalization now corrects (e.g. a
            # supplementary .s001 component stripped to the article's own
            # DOI) means the saved file was the wrong document, so that
            # paper goes through the pipeline again.
            doi_now = existing_doi or normalize_doi(tracked.get("DOI", ""))
            if doi_now == tracked.get("DOI", ""):
                print(f"[{i + 1}/{total}] {title[:70]!r} — already downloaded, updating info columns")
                enrich_record(tracked, i + 1, session, timeout, downloads_dir)
                downloaded_count += 1
                continue

        print(f"[{i + 1}/{total}] {title[:70]!r}", end=" ... ")

        doi = existing_doi
        if not doi:
            doi = normalize_doi(resolve_doi_via_crossref(title, authors, session, timeout))

        record = {field: "" for field in TRACKING_FIELDS}
        record.update({
            "Entry": str(i + 1),
            "Title": title,
            "Authors": authors,
            "DOI": doi or "",
            "Last_Updated": datetime.now(timezone.utc).isoformat(),
        })

        if not doi:
            record["Status"] = "no_doi_found"
            record["Notes"] = "Could not resolve a DOI from title/authors"
            print("no DOI found")
            tracking[title] = record
            time.sleep(delay)
            continue

        record["Source_URL"] = f"https://doi.org/{doi}"
        record["Publisher_URL"] = resolve_publisher_url(doi, session, timeout)

        # Year / author list / abstract summary from CrossRef — fetched
        # up front so the downloaded file can be named properly.
        meta = fetch_crossref_metadata(doi, session, timeout)
        record["Year"] = meta.get("year", "")
        if not record["Authors"] and meta.get("authors"):
            record["Authors"] = "; ".join(a["name"] for a in meta["authors"])
        record["Key_Info"] = first_sentences(meta.get("abstract", ""))

        unpaywall_data = query_unpaywall(doi, email, session, timeout)
        pdf_url = best_oa_pdf_url(unpaywall_data)

        if pdf_url:
            dest_path = os.path.join(
                downloads_dir,
                build_pdf_filename(record["Entry"], record["Year"], title, doi),
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

        tracking[title] = record
        time.sleep(delay)

    save_tracking(tracking_path, tracking)
    print()
    print(f"Done. {downloaded_count} downloaded directly, {needs_proxy_count} need the university proxy.")
    print(f"See {tracking_path} for the full breakdown.")


if __name__ == "__main__":
    main()
