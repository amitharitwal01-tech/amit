"""Stage 2: for papers Stage 1 couldn't fetch openly, log in through your
university's proxy/SSO and download them using your own institutional
access. You'll be prompted for your credentials each run — nothing is
stored on disk.
"""
import csv
import getpass
import os
import sys
from datetime import datetime, timezone

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from doi_resolver import CONFIG_PATH, TRACKING_FIELDS, sanitize_filename, load_config as _load_config

PDF_LINK_HINTS = [".pdf", "/pdf/", "pdfft", "download"]
PROFILE_DIR = os.path.join(os.getcwd(), "browser_profile")


def load_config():
    return _load_config()


def load_tracking(tracking_path):
    rows = {}
    if not os.path.exists(tracking_path):
        sys.exit(
            f"{tracking_path} not found. Run doi_resolver.py first (Stage 1)."
        )
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


def get_credentials(login_cfg):
    username = login_cfg.get("username") or input("University login ID: ").strip()
    password = getpass.getpass("Password (hidden as you type): ")
    return username, password


def attempt_login(page, login_cfg, username, password, fallback_url=None):
    login_url = login_cfg.get("proxy_login_url", "")
    if not login_url:
        if fallback_url:
            print(
                "No 'login.proxy_login_url' set — opening the first paper's "
                "page instead, so you have something to log in through."
            )
            page.goto(fallback_url, wait_until="domcontentloaded")
        else:
            print(
                "No 'login.proxy_login_url' set in config — please log in "
                "manually in the browser window that just opened."
            )
        return False

    page.goto(login_url, wait_until="domcontentloaded")

    user_sel = login_cfg.get("username_selector", "")
    pass_sel = login_cfg.get("password_selector", "")
    submit_sel = login_cfg.get("submit_selector", "")

    try:
        page.fill(user_sel, username, timeout=8000)
        page.fill(pass_sel, password, timeout=8000)
        page.click(submit_sel, timeout=8000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        print(
            "Couldn't find the expected login fields on this page — the "
            "selectors in the config probably don't match this site."
        )
        return False

    success_indicator = login_cfg.get("success_indicator", "")
    if success_indicator:
        return success_indicator in page.url or success_indicator in page.content()
    return True


def ensure_logged_in(page, login_cfg, username, password, fallback_url=None):
    logged_in = False
    try:
        logged_in = attempt_login(page, login_cfg, username, password, fallback_url)
    except Exception as e:
        print(f"Automatic login hit an error: {e}")

    if not logged_in:
        input(
            "Please finish logging in by hand in the browser window, then "
            "come back here and press Enter to continue..."
        )


def build_target_url(source_url, proxy_cfg):
    prefix = proxy_cfg.get("url_prefix", "")
    return f"{prefix}{source_url}" if prefix else source_url


def find_pdf_url(page):
    meta = page.query_selector("meta[name='citation_pdf_url']")
    if meta:
        content = meta.get_attribute("content")
        if content:
            return content, "click"

    for link in page.query_selector_all("a"):
        href = link.get_attribute("href") or ""
        lowered = href.lower()
        if any(hint in lowered for hint in PDF_LINK_HINTS):
            return href, "link"

    return None, None


def download_via_browser(context, page, pdf_url, dest_path):
    try:
        if pdf_url.startswith("http"):
            resp = context.request.get(pdf_url)
            if resp.ok and "pdf" in resp.headers.get("content-type", "").lower():
                with open(dest_path, "wb") as f:
                    f.write(resp.body())
                return True
    except Exception:
        pass

    try:
        with page.expect_download(timeout=15000) as download_info:
            page.goto(pdf_url)
        download = download_info.value
        download.save_as(dest_path)
        return True
    except Exception:
        return False


def main():
    config = load_config()
    tracking_path = config["paths"]["tracking_csv"]
    downloads_dir = config["paths"]["downloads_dir"]
    os.makedirs(downloads_dir, exist_ok=True)

    tracking = load_tracking(tracking_path)
    pending = [row for row in tracking.values() if row["Status"] == "needs_proxy"]

    if not pending:
        print("Nothing needs the proxy — every paper was already resolved in Stage 1.")
        return

    print(f"{len(pending)} paper(s) need the university proxy.")
    login_cfg = config.get("login", {})
    username, password = get_credentials(login_cfg)

    proxy_cfg = config.get("proxy", {})
    downloaded_count = 0
    failed_count = 0

    first_fallback_url = build_target_url(pending[0]["Source_URL"], proxy_cfg)

    with sync_playwright() as p:
        launch_kwargs = dict(headless=False, accept_downloads=True)
        try:
            # Prefer your real, installed Chrome — some institutional SSO
            # pages are pickier about the bundled test browser.
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR, channel="chrome", **launch_kwargs
            )
        except Exception:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR, **launch_kwargs
            )
        page = context.pages[0] if context.pages else context.new_page()

        print(
            "This browser window remembers your login between runs (saved "
            f"in {PROFILE_DIR}) — you shouldn't need to log in every time."
        )
        ensure_logged_in(page, login_cfg, username, password, first_fallback_url)

        for i, row in enumerate(pending, 1):
            title = row["Title"]
            print(f"[{i}/{len(pending)}] {title[:70]!r}", end=" ... ")

            target_url = build_target_url(row["Source_URL"], proxy_cfg)
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            except PlaywrightTimeoutError:
                row["Status"] = "manual_check_needed"
                row["Notes"] = f"Timed out loading {target_url}"
                row["Last_Updated"] = datetime.now(timezone.utc).isoformat()
                failed_count += 1
                print("timed out")
                save_tracking(tracking_path, tracking)
                continue

            pdf_url, _ = find_pdf_url(page)
            if not pdf_url:
                row["Status"] = "manual_check_needed"
                row["Notes"] = f"Could not locate a PDF link on {page.url}"
                row["Last_Updated"] = datetime.now(timezone.utc).isoformat()
                failed_count += 1
                print("no PDF link found")
                save_tracking(tracking_path, tracking)
                continue

            doi_part = row["DOI"].replace("/", "_") if row["DOI"] else sanitize_filename(title)
            dest_path = os.path.join(downloads_dir, sanitize_filename(doi_part) + ".pdf")

            if download_via_browser(context, page, pdf_url, dest_path):
                row["Status"] = "downloaded_via_proxy"
                row["PDF_Path"] = dest_path
                row["Notes"] = ""
                downloaded_count += 1
                print("downloaded")
            else:
                row["Status"] = "manual_check_needed"
                row["Notes"] = f"Found a likely PDF link but download failed: {pdf_url}"
                failed_count += 1
                print("download failed")

            row["Last_Updated"] = datetime.now(timezone.utc).isoformat()
            save_tracking(tracking_path, tracking)

        context.close()

    print()
    print(f"Done. {downloaded_count} downloaded via proxy, {failed_count} need a manual look (see notes in {tracking_path}).")


if __name__ == "__main__":
    main()
