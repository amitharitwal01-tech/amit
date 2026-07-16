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
from urllib.parse import urlparse

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from doi_resolver import CONFIG_PATH, TRACKING_FIELDS, sanitize_filename, load_config as _load_config

PROFILE_DIR = os.path.join(os.getcwd(), "browser_profile")

# Substring match against whatever host we're on (plain or hostname-mangled
# through a proxy — dots become dashes there, so both forms are checked).
# Nature is handled separately: its PDF path is article-slug-based, not
# DOI-based.
PUBLISHER_PDF_PATH_RULES = (
    ("wiley.com", "/doi/pdf/{doi}"),
    ("acs.org", "/doi/pdf/{doi}"),
    ("science.org", "/doi/pdf/{doi}"),
    ("springer.com", "/content/pdf/{doi}.pdf"),
    ("tandfonline.com", "/doi/epdf/{doi}"),
    ("pnas.org", "/doi/epdf/{doi}"),
)


def path_template_for_host(host):
    host = host.lower()
    for fragment, template in PUBLISHER_PDF_PATH_RULES:
        if fragment in host or fragment.replace(".", "-") in host:
            return template
    return None


def apply_hostname_mangling_proxy(url, suffix):
    # Rewrites e.g. https://pubs.acs.org/doi/pdf/<doi> into
    # https://pubs-acs-org<suffix>/doi/pdf/<doi> — the URL scheme some
    # institutional proxies use (dots in the hostname become dashes, then
    # the proxy's own domain is appended) instead of a URL-prefix proxy.
    parsed = urlparse(url)
    mangled_host = parsed.netloc.replace(".", "-") + suffix
    return parsed._replace(netloc=mangled_host).geturl()


def build_doi_proxy_url(doi, suffix):
    if not suffix or not doi:
        return None
    return f"https://doi-org{suffix}/{doi}"


# Used only when Stage 1 couldn't resolve the actual hosting domain for a
# DOI (network hiccup, etc.). CrossRef DOI prefixes are registered per
# publisher, so the prefix alone is a reasonable guess — but it can't know
# a specific journal imprint's subdomain (Wiley has dozens), which is why
# Stage 1's resolved Publisher_URL is always tried first.
DOI_PREFIX_TO_FALLBACK_HOST = {
    "10.1021": "pubs.acs.org",
    "10.1002": "onlinelibrary.wiley.com",
    "10.1126": "www.science.org",
    "10.1007": "link.springer.com",
    "10.1080": "tandfonline.com",
    "10.1073": "pnas.org",
}


def build_proxy_pdf_url(doi, publisher_host, suffix):
    if not doi or not suffix:
        return None

    doi_prefix = doi.split("/", 1)[0]

    # Nature-family: the DOI suffix *is* the article slug, and Nature
    # Portfolio journals sometimes resolve through Springer's domain — the
    # nature.com URL is preferred regardless of which host DOI resolution
    # actually landed on.
    if doi_prefix == "10.1038" and "/" in doi:
        slug = doi.split("/", 1)[1]
        return apply_hostname_mangling_proxy(f"https://www.nature.com/articles/{slug}.pdf", suffix)

    host = publisher_host or DOI_PREFIX_TO_FALLBACK_HOST.get(doi_prefix, "")
    if not host:
        return None
    if "nature.com" in host.lower():
        return None  # need the DOI prefix check above for the slug; nothing else to do

    path_template = path_template_for_host(host)
    if not path_template:
        return None

    plain_url = f"https://{host}{path_template.format(doi=doi)}"
    return apply_hostname_mangling_proxy(plain_url, suffix)


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


def build_candidate_urls(row, config):
    doi = (row.get("DOI") or "").strip()
    proxy_cfg = config.get("proxy", {})
    suffix = proxy_cfg.get("hostname_mangling_suffix", "")
    candidates = []

    if suffix:
        # Once a hostname-mangling proxy is configured, every candidate
        # must stay on a proxied domain — the plain journal site is what
        # trips the publisher's bot detection. No LibKey, no bare doi.org
        # fallback here, even as a last resort.
        publisher_host = urlparse(row.get("Publisher_URL", "") or "").netloc
        known_url = build_proxy_pdf_url(doi, publisher_host, suffix)
        if known_url:
            candidates.append(known_url)

        doi_proxy_url = build_doi_proxy_url(doi, suffix)
        if doi_proxy_url:
            candidates.append(doi_proxy_url)
    else:
        library_id = config.get("libkey", {}).get("library_id", "")
        if library_id and doi:
            candidates.append(f"https://libkey.io/libraries/{library_id}/{doi}")

        prefix = proxy_cfg.get("url_prefix", "")
        source_url = row.get("Source_URL", "")
        if prefix and source_url:
            candidates.append(f"{prefix}{source_url}")
        if source_url:
            candidates.append(source_url)

    seen = set()
    deduped = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def fetch_pdf_if_thats_what_this_url_is(context, url, dest_path, timeout=20000):
    try:
        resp = context.request.get(url, timeout=timeout)
        if resp.ok and "pdf" in resp.headers.get("content-type", "").lower():
            with open(dest_path, "wb") as f:
                f.write(resp.body())
            return True
    except Exception:
        pass
    return False


def goto_and_capture_direct_download(page, context, url, dest_path, nav_timeout, download_wait_ms=5000):
    # goto() runs to completion first (up to nav_timeout), then exiting the
    # expect_download block waits up to download_wait_ms more for a download
    # event — short, since it fires in sync with the response, not later.
    try:
        with page.expect_download(timeout=download_wait_ms) as download_info:
            page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
        download_info.value.save_as(dest_path)
        return True
    except Exception:
        pass

    # No "download" event doesn't mean no PDF — Chrome's built-in viewer
    # renders PDFs inline in the tab instead of downloading them. Whatever
    # URL the page landed on, re-request it directly (same session cookies)
    # and check if the response itself is actually a PDF.
    return fetch_pdf_if_thats_what_this_url_is(context, page.url, dest_path, nav_timeout)


def looks_like_login_page(page):
    return page.query_selector("input[type='password']") is not None


BOT_CHALLENGE_PHRASES = (
    "verify you are human",
    "checking your browser",
    "performing security verification",
    "attention required",
    "just a moment",
)


def looks_like_bot_challenge_page(page):
    # Cloudflare/similar anti-bot walls (common on Wiley, sometimes
    # others). Not something to script around — the fix is to pause and
    # let the person actually sitting at the browser tick the box.
    try:
        content = (page.content() or "").lower()
    except Exception:
        return False
    return any(phrase in content for phrase in BOT_CHALLENGE_PHRASES)


def describe_manual_step_needed(page):
    if looks_like_bot_challenge_page(page):
        return "a quick human/bot-check (tick the verification box)"
    if looks_like_login_page(page):
        return "a fresh login"
    return None


def find_meta_pdf_url(page):
    # The "citation_pdf_url" meta tag is a long-standing scholarly-metadata
    # convention (used by Google Scholar, Zotero, etc.) that most major
    # publisher platforms embed in <head> regardless of how their on-page
    # download UI happens to be built that week.
    meta = page.query_selector("meta[name='citation_pdf_url']")
    if meta:
        content = meta.get_attribute("content")
        if content:
            return content
    return None


def find_embedded_pdf_url(page):
    # Some readers stream the PDF into an <embed>/<iframe> rather than
    # exposing a clickable download link at all.
    for selector in ("embed[type='application/pdf']", "embed[src*='.pdf']", "iframe[src*='.pdf']"):
        el = page.query_selector(selector)
        if el:
            src = el.get_attribute("src")
            if src:
                return src
    for frame in page.frames:
        if frame.url and ".pdf" in frame.url.lower():
            return frame.url
    return None


def print_page_to_pdf(playwright_instance, context, page, dest_path, min_bytes=50_000):
    # Chromium's native print-to-PDF (same as Ctrl+P -> Save as PDF). A
    # too-small result usually means we printed a login wall or an
    # abstract-only page rather than the real article.
    try:
        page.pdf(path=dest_path)
        if os.path.getsize(dest_path) >= min_bytes:
            return True
    except Exception:
        pass

    # Some Chrome builds only support print-to-PDF in headless mode, and
    # this browser runs headed so you can watch/intervene. Fall back to a
    # short-lived headless clone of the current session (same cookies)
    # just to render this one page.
    try:
        cookies = context.cookies()
    except Exception:
        cookies = []

    headless_browser = None
    try:
        headless_browser = playwright_instance.chromium.launch(headless=True)
        headless_context = headless_browser.new_context()
        if cookies:
            headless_context.add_cookies(cookies)
        temp_page = headless_context.new_page()
        temp_page.goto(page.url, wait_until="domcontentloaded", timeout=20000)
        temp_page.pdf(path=dest_path)
    except Exception:
        return False
    finally:
        if headless_browser is not None:
            try:
                headless_browser.close()
            except Exception:
                pass

    try:
        return os.path.getsize(dest_path) >= min_bytes
    except OSError:
        return False


def try_download_paper(playwright_instance, page, context, row, target_urls, dest_path):
    # Layer 1: try each candidate entry point (proxy, LibKey, ...) in turn
    # — one might serve the PDF directly even if another errors out.
    for url in target_urls:
        if goto_and_capture_direct_download(page, context, url, dest_path, nav_timeout=25000):
            return True

    target_url = target_urls[0] if target_urls else ""

    reason = describe_manual_step_needed(page)
    if reason:
        print(f"needs {reason}")
        input(
            "Please handle it in the browser window, then press Enter "
            "here to continue..."
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        if goto_and_capture_direct_download(page, context, page.url, dest_path, nav_timeout=15000):
            return True

    # Layer 2: scholarly metadata embedded in <head>, if the page has it.
    # A background fetch, not a navigation — doesn't move the browser.
    meta_url = find_meta_pdf_url(page)
    if meta_url and fetch_pdf_if_thats_what_this_url_is(context, meta_url, dest_path):
        return True

    # Layer 3: an embedded PDF viewer (<embed>/<iframe>) rather than a
    # link. Also just a background fetch.
    embed_url = find_embedded_pdf_url(page)
    if embed_url and fetch_pdf_if_thats_what_this_url_is(context, embed_url, dest_path):
        return True

    # Layer 4: no download link found anywhere on this page (metadata,
    # embedded viewer). Not going to click around hunting for one — print
    # whatever's rendered here directly, unless it's a login/bot-check
    # wall (in which case pause once more for that first).
    reason = describe_manual_step_needed(page)
    if reason:
        print(f"needs {reason}")
        input(
            "Please handle it in the browser window, then press Enter "
            "here to continue..."
        )
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            pass
        meta_url = find_meta_pdf_url(page)
        if meta_url and fetch_pdf_if_thats_what_this_url_is(context, meta_url, dest_path):
            return True

    if describe_manual_step_needed(page) is None:
        if print_page_to_pdf(playwright_instance, context, page, dest_path):
            return True

    return False


def mark_downloaded(row, dest_path):
    row["Status"] = "downloaded_via_proxy"
    row["PDF_Path"] = dest_path
    row["Notes"] = ""
    row["Last_Updated"] = datetime.now(timezone.utc).isoformat()


def mark_manual_check(row, note):
    row["Status"] = "manual_check_needed"
    row["Notes"] = note
    row["Last_Updated"] = datetime.now(timezone.utc).isoformat()


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

    first_candidates = build_candidate_urls(pending[0], config)
    first_fallback_url = first_candidates[0] if first_candidates else ""

    with sync_playwright() as p:
        # Playwright's bundled Chromium, not your real installed Chrome —
        # more stable for automation (real Chrome needs a --no-sandbox
        # flag here that's caused the browser to crash mid-run).
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, accept_downloads=True
        )
        page = context.pages[0] if context.pages else context.new_page()

        print(
            "This browser window remembers your login between runs (saved "
            f"in {PROFILE_DIR}) — you shouldn't need to log in every time."
        )
        ensure_logged_in(page, login_cfg, username, password, first_fallback_url)

        browser_died = False

        for i, row in enumerate(pending, 1):
            title = row["Title"]
            print(f"[{i}/{len(pending)}] {title[:70]!r}", end=" ... ")

            target_urls = build_candidate_urls(row, config)
            doi_part = row["DOI"].replace("/", "_") if row["DOI"] else sanitize_filename(title)
            dest_path = os.path.join(downloads_dir, sanitize_filename(doi_part) + ".pdf")

            try:
                succeeded = try_download_paper(p, page, context, row, target_urls, dest_path)
            except Exception as e:
                if "closed" in str(e).lower() or "closed" in type(e).__name__.lower():
                    print(f"browser window closed unexpectedly ({type(e).__name__}) — stopping here")
                    mark_manual_check(row, f"Browser closed before this paper finished: {e}")
                    save_tracking(tracking_path, tracking)
                    browser_died = True
                    break
                mark_manual_check(row, f"Unexpected error: {e}")
                failed_count += 1
                print(f"error ({type(e).__name__})")
                save_tracking(tracking_path, tracking)
                continue

            if succeeded:
                mark_downloaded(row, dest_path)
                downloaded_count += 1
                print("downloaded")
            else:
                mark_manual_check(row, f"Could not download or print a PDF from {page.url}")
                failed_count += 1
                print("could not produce a PDF")

            save_tracking(tracking_path, tracking)

        if not browser_died:
            context.close()
        elif i < len(pending):
            print(f"{len(pending) - i} paper(s) weren't attempted — just run this script again to pick up where it left off.")

    print()
    print(f"Done. {downloaded_count} downloaded via proxy, {failed_count} need a manual look (see notes in {tracking_path}).")


if __name__ == "__main__":
    main()
