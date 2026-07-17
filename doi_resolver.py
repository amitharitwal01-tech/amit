"""Stage 1: resolve DOIs for each paper in the Excel sheet and grab any
freely/legally available copy (via Unpaywall). Papers that have no open
access copy are left for Stage 2 (proxy_download.py).
"""
import csv
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
    "Title", "Authors", "DOI", "Status", "PDF_Path",
    "Source_URL", "Publisher_URL", "Notes", "Last_Updated",
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
            writer.writerow(row)


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
                print(f"[{i + 1}/{total}] {title[:70]!r} — already downloaded, skipping")
                downloaded_count += 1
                continue

        print(f"[{i + 1}/{total}] {title[:70]!r}", end=" ... ")

        doi = existing_doi
        if not doi:
            doi = normalize_doi(resolve_doi_via_crossref(title, authors, session, timeout))

        record = {
            "Title": title,
            "Authors": authors,
            "DOI": doi or "",
            "Status": "",
            "PDF_Path": "",
            "Source_URL": "",
            "Publisher_URL": "",
            "Notes": "",
            "Last_Updated": datetime.now(timezone.utc).isoformat(),
        }

        if not doi:
            record["Status"] = "no_doi_found"
            record["Notes"] = "Could not resolve a DOI from title/authors"
            print("no DOI found")
            tracking[title] = record
            time.sleep(delay)
            continue

        record["Source_URL"] = f"https://doi.org/{doi}"
        record["Publisher_URL"] = resolve_publisher_url(doi, session, timeout)
        unpaywall_data = query_unpaywall(doi, email, session, timeout)
        pdf_url = best_oa_pdf_url(unpaywall_data)

        if pdf_url:
            dest_path = os.path.join(downloads_dir, sanitize_filename(doi.replace("/", "_")) + ".pdf")
            if download_pdf(pdf_url, dest_path, session, timeout):
                record["Status"] = "downloaded"
                record["PDF_Path"] = dest_path
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
