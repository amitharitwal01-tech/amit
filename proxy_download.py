"""Stage 2: for papers Stage 1 couldn't fetch openly, log in through your
university's proxy/SSO and download them using your own institutional
access. You'll be prompted for your credentials each run — nothing is
stored on disk.
"""
import csv
import getpass
import os
import re
import sys
import time
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

    # Wiley: confirmed via the page's own citation_pdf_url meta tag that
    # the PDF always lives at the bare onlinelibrary.wiley.com domain,
    # regardless of which journal-imprint subdomain (advanced.,
    # chemistry-europe., etc.) the article page itself uses.
    if doi_prefix == "10.1002":
        return apply_hostname_mangling_proxy(f"https://onlinelibrary.wiley.com/doi/pdf/{doi}", suffix)

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


def save_if_valid_pdf(data, dest_path):
    # A real PDF file always starts with this magic number. Checking it
    # is the difference between "downloaded" meaning something and a
    # corrupted file getting silently marked as done.
    if not data or not data.startswith(b"%PDF-"):
        return False
    with open(dest_path, "wb") as f:
        f.write(data)
    return True


def validate_saved_pdf(dest_path):
    try:
        with open(dest_path, "rb") as f:
            valid = f.read(5) == b"%PDF-"
    except OSError:
        return False
    if not valid:
        try:
            os.remove(dest_path)
        except OSError:
            pass
    return valid


def fetch_pdf_if_thats_what_this_url_is(context, url, dest_path, timeout=20000):
    try:
        resp = context.request.get(url, timeout=timeout)
        if resp.ok and "pdf" in resp.headers.get("content-type", "").lower():
            return save_if_valid_pdf(resp.body(), dest_path)
    except Exception:
        pass
    return False


def page_is_showing_pdf(page, timeout_ms=8000):
    # When Chrome's built-in viewer is displaying a PDF (rather than an
    # HTML page), the document itself reports this. Actively waits
    # rather than checking once — there can be a brief redirect/loading
    # gap between the navigation resolving and the PDF viewer taking
    # over, and checking too early reads "text/html" and gives up on a
    # page that was about to be the right one.
    try:
        page.wait_for_function("document.contentType === 'application/pdf'", timeout=timeout_ms)
        return True
    except Exception:
        return False


def print_displayed_pdf(page, context, playwright_instance, dest_path):
    # Only ever called when page_is_showing_pdf() is true: this exports
    # the exact PDF Chrome is already correctly rendering (Chrome's own
    # download icon here is part of its native viewer UI, not the page
    # DOM — nothing to click; exporting is the only way to grab it).
    try:
        page.pdf(path=dest_path)
        if validate_saved_pdf(dest_path):
            return True
    except Exception:
        pass

    # Chrome's automated print-to-PDF has historically only worked
    # reliably in headless mode, and this browser runs headed on purpose
    # (so you can log in / clear bot-checks). Fall back to a short-lived
    # headless clone of this session (same cookies) just to export this
    # one already-confirmed PDF.
    if playwright_instance is None:
        return False
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

    return validate_saved_pdf(dest_path)


def goto_and_capture_direct_download(page, context, url, dest_path, nav_timeout, download_wait_ms=5000, referer=None, playwright_instance=None):
    # page.goto() sends no Referer by default, unlike a real link click —
    # some publisher proxies (Wiley confirmed) bounce a referer-less
    # request to the PDF URL back to the article's abstract page as an
    # anti-hotlinking measure. Passing referer= mimics "clicked Download
    # from this article page" when we already have one to point at.
    goto_kwargs = {"wait_until": "domcontentloaded", "timeout": nav_timeout}
    if referer:
        goto_kwargs["referer"] = referer

    try:
        with page.expect_download(timeout=download_wait_ms) as download_info:
            page.goto(url, **goto_kwargs)
        download_info.value.save_as(dest_path)
        if validate_saved_pdf(dest_path):
            return True
    except Exception:
        pass

    # No "download" event doesn't mean no PDF — Chrome's built-in viewer
    # renders PDFs inline instead. If that's what's on screen, export it
    # directly rather than making a second network request for the same
    # file (which can get blocked even when the original navigation that
    # already fetched it successfully wasn't).
    if page_is_showing_pdf(page) and print_displayed_pdf(page, context, playwright_instance, dest_path):
        return True

    return fetch_pdf_if_thats_what_this_url_is(context, page.url, dest_path, nav_timeout)


def fetch_or_navigate_to_pdf(page, context, url, dest_path, nav_timeout=20000, playwright_instance=None):
    # A raw background fetch (context.request) doesn't look like real
    # browser traffic and can get blocked by Cloudflare-style protection
    # even when a full navigation to the same URL would get through (and
    # correctly trigger the bot-check pause if one appears). Try the fast
    # path first, fall back to a real navigation if that didn't work —
    # with a referer, since we're already sitting on the article page
    # this URL came from.
    if fetch_pdf_if_thats_what_this_url_is(context, url, dest_path):
        return True
    return goto_and_capture_direct_download(
        page, context, url, dest_path, nav_timeout,
        referer=page.url, playwright_instance=playwright_instance,
    )


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


DOWNLOAD_NAME_PATTERN = re.compile(r"download|full[\s-]?text\s*pdf|view\s*pdf|\bpdf\b", re.IGNORECASE)

# Confirmed from actual page markup: some readers (Wiley's included) put
# the icon's label in a sibling tooltip span rather than an aria-label —
# <button><svg>...</svg><span class="gsr-btn-tp ...">Download</span></button>
# — but that span may only exist in the DOM after hovering, and the icon
# isn't necessarily wrapped in a real <button>/<a> (custom component kits
# often use a styled <div> with a JS click handler instead). The SVG path
# data itself is always present regardless of hover state or wrapper tag,
# so match on that and walk up to whatever's actually clickable.
KNOWN_ICON_PATH_PREFIXES = (
    "M15.7 13.8V16.5H5.2V13.8H3.5V16.5C3.5 17.4",  # Wiley reader "Download" icon
)


def nearest_interactive_ancestor(locator):
    ancestor = locator.locator(
        "xpath=ancestor::*[self::button or self::a or @role='button' or @tabindex][1]"
    )
    try:
        if ancestor.count() > 0:
            return ancestor.first
    except Exception:
        pass
    return locator.locator("xpath=..")


def find_download_control_by_known_markup(page, timeout=4000):
    for prefix in KNOWN_ICON_PATH_PREFIXES:
        path_locator = page.locator(f"path[d^='{prefix}']")
        try:
            if path_locator.count() > 0:
                path_locator.first.wait_for(state="attached", timeout=timeout)
                return nearest_interactive_ancestor(path_locator.first)
        except Exception:
            continue

    span_locator = page.locator("span.gsr-btn-tp", has_text=re.compile(r"download", re.IGNORECASE))
    try:
        if span_locator.count() > 0:
            span_locator.first.wait_for(state="attached", timeout=timeout)
            return nearest_interactive_ancestor(span_locator.first)
    except Exception:
        pass

    return None


def find_download_locator_by_accessibility(page, timeout=6000):
    # Uses the browser's own accessibility-name computation (aria-label,
    # aria-labelledby, title, or associated/hidden text) rather than raw
    # CSS attributes — catches icon-only controls a plain selector misses,
    # as long as the icon has *some* accessible name at all.
    for role in ("link", "button"):
        locator = page.get_by_role(role, name=DOWNLOAD_NAME_PATTERN)
        try:
            if locator.count() == 0:
                continue
            locator.first.wait_for(state="visible", timeout=timeout)
            return locator.first
        except Exception:
            continue
    return None


def find_download_icon_point(page, template_path, threshold=0.8):
    # Last resort: locate the download icon by its actual pixel
    # appearance (OpenCV template matching) when it has no accessible
    # name at all. Returns viewport (x, y) coordinates to click, or None.
    if not template_path or not os.path.exists(template_path):
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass

    try:
        screenshot_bytes = page.screenshot()
    except Exception:
        return None

    screenshot = cv2.imdecode(np.frombuffer(screenshot_bytes, np.uint8), cv2.IMREAD_COLOR)
    template = cv2.imread(template_path, cv2.IMREAD_COLOR)
    if screenshot is None or template is None:
        return None

    th, tw = template.shape[:2]
    if th > screenshot.shape[0] or tw > screenshot.shape[1]:
        return None

    result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < threshold:
        return None

    return (max_loc[0] + tw // 2, max_loc[1] + th // 2)


def click_and_capture(page, context, click_fn, dest_path, wait_seconds=15, playwright_instance=None):
    # Shared by both detection methods above: a click might trigger a
    # same-tab download, or open a new tab showing/streaming the PDF.
    result = {"done": False}

    def on_download(download):
        if result["done"]:
            return
        try:
            download.save_as(dest_path)
            result["done"] = validate_saved_pdf(dest_path)
        except Exception:
            pass

    def on_new_page(new_page):
        if result["done"]:
            return
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        if page_is_showing_pdf(new_page):
            result["done"] = print_displayed_pdf(new_page, context, playwright_instance, dest_path)
        if not result["done"]:
            try:
                resp = context.request.get(new_page.url)
                if resp.ok and "pdf" in resp.headers.get("content-type", "").lower():
                    result["done"] = save_if_valid_pdf(resp.body(), dest_path)
            except Exception:
                pass
        try:
            new_page.close()
        except Exception:
            pass

    page.on("download", on_download)
    context.on("page", on_new_page)
    try:
        click_fn()
    except Exception:
        page.remove_listener("download", on_download)
        context.remove_listener("page", on_new_page)
        return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline and not result["done"]:
        page.wait_for_timeout(250)
    page.remove_listener("download", on_download)
    context.remove_listener("page", on_new_page)

    if result["done"]:
        return True

    return fetch_pdf_if_thats_what_this_url_is(context, page.url, dest_path, timeout=8000)


def try_download_paper(
    page, context, row, target_urls, dest_path,
    icon_template_path="", icon_match_threshold=0.8, playwright_instance=None,
):
    # Layer 1: try each candidate entry point (proxy, LibKey, ...) in turn
    # — one might serve the PDF directly even if another errors out. Each
    # attempt after the first uses wherever the previous one landed as
    # the referer, so a direct-PDF-URL candidate looks like it was
    # reached by clicking through from the article page rather than a
    # cold, referer-less request (which some proxies reject).
    referer = None
    for url in target_urls:
        if goto_and_capture_direct_download(
            page, context, url, dest_path, nav_timeout=25000,
            referer=referer, playwright_instance=playwright_instance,
        ):
            return True
        referer = page.url

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
        if goto_and_capture_direct_download(
            page, context, page.url, dest_path, nav_timeout=15000,
            playwright_instance=playwright_instance,
        ):
            return True

    # Layer 2: scholarly metadata embedded in <head>, if the page has it.
    # Tries a background fetch first, falls back to a real navigation
    # (survives Cloudflare-style blocks on the lightweight fetch).
    meta_url = find_meta_pdf_url(page)
    if meta_url and fetch_or_navigate_to_pdf(page, context, meta_url, dest_path, playwright_instance=playwright_instance):
        return True

    # Layer 3: an embedded PDF viewer (<embed>/<iframe>) rather than a link.
    embed_url = find_embedded_pdf_url(page)
    if embed_url and fetch_or_navigate_to_pdf(page, context, embed_url, dest_path, playwright_instance=playwright_instance):
        return True

    # Layer 4: a download control matching known specific markup patterns
    # confirmed from real pages (e.g. Wiley's icon + hidden tooltip span).
    locator = find_download_control_by_known_markup(page)

    # Layer 5: failing that, a control found by its accessible name (works
    # for icon-only buttons that have an aria-label/title even without
    # visible text).
    if locator is None:
        locator = find_download_locator_by_accessibility(page)

    if locator is not None:
        if click_and_capture(
            page, context, lambda: locator.click(timeout=5000), dest_path,
            playwright_instance=playwright_instance,
        ):
            return True

    # Layer 6: the same icon located by its actual pixel appearance, for
    # icons with no accessible name or matching markup at all.
    icon_point = find_download_icon_point(page, icon_template_path, icon_match_threshold)
    if icon_point is not None:
        x, y = icon_point
        if click_and_capture(
            page, context, lambda: page.mouse.click(x, y), dest_path,
            playwright_instance=playwright_instance,
        ):
            return True

    # Nothing found anywhere on this page — one more chance to log in or
    # clear a bot-check if that's what's actually blocking things. Re-check
    # whatever's currently loaded rather than navigating anywhere again —
    # re-hitting a login-triggering URL after already clearing it once can
    # invalidate the just-established session (seen as a stale/expired
    # Shibboleth flow error).
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
        meta_url = find_meta_pdf_url(page)
        if meta_url and fetch_or_navigate_to_pdf(page, context, meta_url, dest_path, playwright_instance=playwright_instance):
            return True

    return False


def save_debug_snapshot(page, doi, debug_dir="debug"):
    # So a failed paper can be diagnosed from the saved files instead of
    # another manual DevTools round-trip: what the page actually looked
    # like, and its full HTML to grep for the real markup.
    try:
        os.makedirs(debug_dir, exist_ok=True)
        base = sanitize_filename(doi.replace("/", "_")) if doi else "unknown"
        page.screenshot(path=os.path.join(debug_dir, f"{base}.png"))
        with open(os.path.join(debug_dir, f"{base}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


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
    icon_cfg = config.get("download_icon", {})
    icon_template_path = icon_cfg.get("template_image", "")
    icon_match_threshold = icon_cfg.get("match_threshold", 0.8)

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
                succeeded = try_download_paper(
                    page, context, row, target_urls, dest_path,
                    icon_template_path, icon_match_threshold,
                    playwright_instance=p,
                )
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
                save_debug_snapshot(page, row.get("DOI", ""))
                mark_manual_check(row, f"Could not find a download control on {page.url}")
                failed_count += 1
                print("no download control found (saved to debug/ for inspection)")

            save_tracking(tracking_path, tracking)

        if not browser_died:
            context.close()
        elif i < len(pending):
            print(f"{len(pending) - i} paper(s) weren't attempted — just run this script again to pick up where it left off.")

    print()
    print(f"Done. {downloaded_count} downloaded via proxy, {failed_count} need a manual look (see notes in {tracking_path}).")


if __name__ == "__main__":
    main()
