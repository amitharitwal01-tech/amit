"""Stage 2: for papers Stage 1 couldn't fetch openly, log in through your
university's proxy/SSO and download them using your own institutional
access. You'll be prompted for your credentials each run — nothing is
stored on disk.
"""
import csv
import getpass
import os
import sys
import time
from datetime import datetime, timezone

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from doi_resolver import CONFIG_PATH, TRACKING_FIELDS, sanitize_filename, load_config as _load_config

PROFILE_DIR = os.path.join(os.getcwd(), "browser_profile")

# Matches a real full-text link if one's already in the HTML, or a rendered
# "Download PDF" / "View PDF" button on JS-heavy pages like LibKey's.
DOWNLOAD_CONTROL_SELECTOR = (
    "a[href*='full-text-file'], a[href$='.pdf'], a[href*='/pdf/'], "
    "a:has-text('Download PDF'), button:has-text('Download PDF'), "
    "a:has-text('View PDF'), button:has-text('View PDF'), "
    "a:has-text('Download'), button:has-text('Download')"
)


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


def build_target_url(row, config):
    doi = (row.get("DOI") or "").strip()
    library_id = config.get("libkey", {}).get("library_id", "")
    if library_id and doi:
        return f"https://libkey.io/libraries/{library_id}/{doi}"

    proxy_cfg = config.get("proxy", {})
    prefix = proxy_cfg.get("url_prefix", "")
    source_url = row.get("Source_URL", "")
    return f"{prefix}{source_url}" if prefix else source_url


def goto_and_capture_direct_download(page, url, dest_path, nav_timeout, download_wait_ms=5000):
    # goto() runs to completion first (up to nav_timeout), then exiting the
    # expect_download block waits up to download_wait_ms more for a download
    # event — short, since it fires in sync with the response, not later.
    # Any failure here (nav timeout, no download, bad URL) just means "not
    # a direct download" — the caller falls back to scraping the page.
    try:
        with page.expect_download(timeout=download_wait_ms) as download_info:
            page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
        download_info.value.save_as(dest_path)
        return True
    except Exception:
        return False


def looks_like_login_page(page):
    return page.query_selector("input[type='password']") is not None


def find_download_element(page, timeout=15000):
    # wait_for_selector actively polls the DOM, so this covers pages like
    # LibKey's that render their download button client-side well after
    # domcontentloaded fires.
    try:
        page.wait_for_selector(DOWNLOAD_CONTROL_SELECTOR, timeout=timeout)
    except PlaywrightTimeoutError:
        return None
    return page.query_selector(DOWNLOAD_CONTROL_SELECTOR)


def click_and_capture_download(page, context, element, dest_path, wait_seconds=15):
    # A click here might trigger a same-tab download, or open a new tab
    # showing/streaming the PDF — listen for both instead of guessing.
    result = {"done": False}

    def on_download(download):
        if result["done"]:
            return
        try:
            download.save_as(dest_path)
            result["done"] = True
        except Exception:
            pass

    def on_new_page(new_page):
        if result["done"]:
            return
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        try:
            resp = context.request.get(new_page.url)
            if resp.ok and "pdf" in resp.headers.get("content-type", "").lower():
                with open(dest_path, "wb") as f:
                    f.write(resp.body())
                result["done"] = True
        except Exception:
            pass
        finally:
            try:
                new_page.close()
            except Exception:
                pass

    page.on("download", on_download)
    context.on("page", on_new_page)
    try:
        try:
            element.click(timeout=5000)
        except Exception:
            return False
        deadline = time.time() + wait_seconds
        while time.time() < deadline and not result["done"]:
            page.wait_for_timeout(250)
        return result["done"]
    finally:
        page.remove_listener("download", on_download)
        context.remove_listener("page", on_new_page)


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

    downloaded_count = 0
    failed_count = 0

    first_fallback_url = build_target_url(pending[0], config)

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

            target_url = build_target_url(row, config)
            doi_part = row["DOI"].replace("/", "_") if row["DOI"] else sanitize_filename(title)
            dest_path = os.path.join(downloads_dir, sanitize_filename(doi_part) + ".pdf")

            if goto_and_capture_direct_download(page, target_url, dest_path, nav_timeout=25000):
                row["Status"] = "downloaded_via_proxy"
                row["PDF_Path"] = dest_path
                row["Notes"] = ""
                row["Last_Updated"] = datetime.now(timezone.utc).isoformat()
                downloaded_count += 1
                print("downloaded")
                save_tracking(tracking_path, tracking)
                continue

            el = find_download_element(page)

            if el is None and looks_like_login_page(page):
                print("needs a fresh login")
                input(
                    "Please log in again in the browser window, then press "
                    "Enter here to continue..."
                )
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    pass
                el = find_download_element(page)

            if el is None:
                row["Status"] = "manual_check_needed"
                row["Notes"] = f"Could not find a download button on {page.url}"
                row["Last_Updated"] = datetime.now(timezone.utc).isoformat()
                failed_count += 1
                print("no download button found")
                save_tracking(tracking_path, tracking)
                continue

            if click_and_capture_download(page, context, el, dest_path):
                row["Status"] = "downloaded_via_proxy"
                row["PDF_Path"] = dest_path
                row["Notes"] = ""
                downloaded_count += 1
                print("downloaded")
            else:
                row["Status"] = "manual_check_needed"
                row["Notes"] = f"Found a download button but the download didn't complete: {page.url}"
                failed_count += 1
                print("download failed")

            row["Last_Updated"] = datetime.now(timezone.utc).isoformat()
            save_tracking(tracking_path, tracking)

        context.close()

    print()
    print(f"Done. {downloaded_count} downloaded via proxy, {failed_count} need a manual look (see notes in {tracking_path}).")


if __name__ == "__main__":
    main()
