import asyncio
import os
from datetime import datetime, timezone

from playwright.async_api import async_playwright

# Config

HOME_URL       = "https://m.betking.com/en-ng"
LOGIN_API_PATH = "/islands/_actions/login/"

BASE      = "https://m.betking.com"
TODAY_URL = f"{BASE}/en-ng/sports/table-tennis/today"

USERNAME = "09120183273"
PASSWORD = "Edmond99@"

SIGN_IN_BUTTON_SELECTOR = 'button[data-testid="signInButton"]'
LOGIN_FORM_SELECTOR     = 'dialog[open] form[data-testid="signIn"]'
USERNAME_INPUT_SELECTOR = 'dialog[open] input[name="username"]'
PASSWORD_INPUT_SELECTOR = 'dialog[open] input[name="password"]'
SUBMIT_BUTTON_SELECTOR  = 'dialog[open] form[data-testid="signIn"] button[type="submit"]'
COOKIE_BANNER_SELECTOR  = '#cookiebanner'
COOKIE_ACCEPT_TEXT      = 'ACCEPT ALL COOKIES'

MATCH_STATS_SELECTOR    = '[data-testid="match-stats-area"]'
SPORTRADAR_HOST         = 'widgets.fn.sportradar.com'

# Must match main.py's ROW_ID so both scripts read/write the same bet_arb row.
ROW_ID = 1

TIMEOUT             = 300_000
MATCH_STATS_WAIT_MS = 60_000
SR_REQUEST_WAIT_MS  = 45_000
SR_POLL_INTERVAL_S  = 0.5
SR_MAX_ATTEMPTS     = 2   # first open, then one close+reopen retry


# DB Helpers

def _load_db_config():
    """Look for db.txt next to this script first, then in the parent
    directory (betking/db.txt) — mirrors main.py's lookup so both
    scripts find the same config regardless of layout."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "db.txt"),
        os.path.join(os.path.dirname(here), "db.txt"),
    ]
    for path in candidates:
        if os.path.exists(path):
            config = {}
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, _, v = line.partition("=")
                        config[k.strip()] = v.strip().strip('"')
            return config
    raise FileNotFoundError(f"db.txt not found in any of: {candidates}")


def _get_supabase():
    from supabase import create_client
    config = _load_db_config()
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f") + "+00"


# Cookie consent

async def _accept_cookies(page, wait_ms: int = 0) -> bool:
    """Click the 'ACCEPT ALL COOKIES' consent button if it's shown.

    - wait_ms > 0 : wait up to that long for the button to appear
      (used right after page load while the dialog is still rendering).
    - wait_ms == 0: quick non-blocking visibility check (used before
      every click so we don't add latency when no dialog is present).

    Tries button role first, then link role, then any element whose
    text contains the label (handles divs with click handlers)."""
    locators = [
        page.get_by_role("button", name=COOKIE_ACCEPT_TEXT, exact=False),
        page.get_by_role("link",   name=COOKIE_ACCEPT_TEXT, exact=False),
        page.get_by_text(COOKIE_ACCEPT_TEXT, exact=False),
    ]
    for loc in locators:
        try:
            first = loc.first
            if wait_ms > 0:
                await first.wait_for(state="visible", timeout=wait_ms)
            elif not await first.is_visible():
                continue
            await first.click(timeout=5_000)
            print(f"[login] accepted cookies via '{COOKIE_ACCEPT_TEXT}' button")
            return True
        except Exception:
            continue
    return False


async def _strip_cookie_banner(page) -> None:
    """Last-resort fallback: if the banner is still covering the page
    and the accept button isn't clickable (e.g. text changed, or the
    click handler is broken), just remove it from the DOM outright."""
    try:
        await page.evaluate(
            "document.getElementById('cookiebanner')?.remove()"
        )
    except Exception:
        pass


async def _click(page, selector: str, timeout: int) -> None:
    """Before clicking anything, first check for the cookie-consent
    dialog and click 'ACCEPT ALL COOKIES' if present. Then strip any
    leftover banner from the DOM, then click the target. If the normal
    click still times out, retry once with force=True."""
    await _accept_cookies(page)             # quick check, no wait
    await _strip_cookie_banner(page)
    try:
        await page.click(selector, timeout=timeout)
    except Exception:
        await _accept_cookies(page)
        await _strip_cookie_banner(page)
        await page.click(selector, timeout=timeout, force=True)


async def _click_stats_button(page) -> None:
    """Click the first match-stats button with the same safety net as
    _click (accept cookies, strip banner, force on retry)."""
    await _accept_cookies(page)
    await _strip_cookie_banner(page)
    try:
        await page.locator(MATCH_STATS_SELECTOR).first.click(timeout=15_000)
    except Exception:
        await _accept_cookies(page)
        await _strip_cookie_banner(page)
        await page.locator(MATCH_STATS_SELECTOR).first.click(
            timeout=15_000, force=True
        )


# Sportradar token extraction

def _extract_token_from_url(url: str):
    """Pull the T=... value out of a widgets.fn.sportradar.com URL.

    The value itself can contain '~', '/', '=' and other chars (it's an
    HMAC-signed token), so we only split on '&' to isolate each query
    parameter, then check for a leading 'T='. No URL-decoding needed —
    the browser sends it unencoded and main.py rebuilds '?T={token}'
    verbatim."""
    if "?" not in url:
        return None
    query = url.split("?", 1)[1]
    for part in query.split("&"):
        if part.startswith("T="):
            return part[2:]
    return None


async def _scan_for_token(sr_urls, timeout_s: float):
    """Poll the captured sportradar URLs for up to timeout_s seconds
    and return the first T= token found, or None on timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        for url in list(sr_urls):
            token = _extract_token_from_url(url)
            if token:
                print("[login] sr_token   : found T= param in captured request")
                return token
        await asyncio.sleep(SR_POLL_INTERVAL_S)
    return None


async def _extract_sr_token(page) -> str:
    """Navigate to the today page, click the first match-stats button,
    and capture the sportradar widget token from any request the page
    makes to widgets.fn.sportradar.com.

    A page-level 'request' listener accumulates every request to the
    sportradar widget host for the entire duration of the retry loop.

    If the first open doesn't yield a request carrying T= within
    SR_REQUEST_WAIT_MS, the stats button is clicked again (closing the
    widget) then clicked once more (re-opening it) — the listener stays
    attached throughout, so any request fired during the close/reopen
    cycle is also captured."""
    print("[login] opening today's matches page ...")
    try:
        await page.goto(TODAY_URL, wait_until="domcontentloaded", timeout=TIMEOUT)
    except Exception as e:
        print(f"[login] sr_token   : goto failed ({e})")
        return None

    # Consent dialog may reappear on the new page.
    await _accept_cookies(page, wait_ms=3_000)

    print("[login] waiting for a match stats button ...")
    try:
        await page.wait_for_selector(
            MATCH_STATS_SELECTOR, state="visible", timeout=MATCH_STATS_WAIT_MS
        )
    except Exception as e:
        print(f"[login] sr_token   : no match-stats-area found ({e})")
        return None

    # Collect every request to the sportradar widget host. The listener
    # lives for the whole retry loop, so nothing fired during the
    # close/reopen cycle is missed. URLs are kept silent here — only
    # counts are printed on the failure path.
    sr_urls: list = []

    def _capture(req):
        try:
            if SPORTRADAR_HOST in req.url:
                sr_urls.append(req.url)
        except Exception:
            pass

    page.on("request", _capture)

    try:
        for attempt in range(1, SR_MAX_ATTEMPTS + 1):
            if attempt == 1:
                print("[login] clicking first match stats button ...")
            else:
                # First 45s poll failed → toggle the widget closed, then
                # reopen it, still listening for sportradar requests.
                print(
                    f"[login] attempt {attempt}: closing + reopening stats widget ..."
                )
                try:
                    await _click_stats_button(page)
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"[login] attempt {attempt}: close click failed ({e})")
                try:
                    await _click_stats_button(page)
                except Exception as e:
                    print(f"[login] attempt {attempt}: reopen click failed ({e})")
                    continue

            if attempt == 1:
                await _click_stats_button(page)

            print(
                f"[login] attempt {attempt}: polling {SR_REQUEST_WAIT_MS // 1000}s "
                f"for a T= request ..."
            )
            token = await _scan_for_token(sr_urls, SR_REQUEST_WAIT_MS / 1000.0)
            if token:
                return token

            print(
                f"[login] attempt {attempt}: no T= request yet "
                f"({len(sr_urls)} sportradar url(s) captured so far)"
            )

        if not sr_urls:
            print("[login] sr_token   : no sportradar requests captured")
        else:
            print(
                f"[login] sr_token   : {len(sr_urls)} sportradar request(s) "
                f"captured but none had T= param"
            )
        return None
    finally:
        try:
            page.remove_listener("request", _capture)
        except Exception:
            pass


# Login

async def login() -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        # Original viewport — restored.
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        print("[login] opening home page ...")
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=TIMEOUT)

        # Give the consent dialog time to render, then accept it.
        print("[login] checking for cookie consent dialog ...")
        accepted = await _accept_cookies(page, wait_ms=5_000)
        if not accepted:
            print("[login] no consent dialog found (or already accepted) — continuing")

        print("[login] clicking sign-in button ...")
        await _click(page, SIGN_IN_BUTTON_SELECTOR, TIMEOUT)

        print("[login] waiting for login dialog ...")
        await page.wait_for_selector(LOGIN_FORM_SELECTOR, timeout=TIMEOUT)

        # Angular sometimes re-renders the consent banner when the login
        # dialog opens — re-check before interacting with the form.
        await _accept_cookies(page)

        await page.fill(USERNAME_INPUT_SELECTOR, USERNAME, timeout=TIMEOUT)
        await page.fill(PASSWORD_INPUT_SELECTOR, PASSWORD, timeout=TIMEOUT)

        try:
            async with page.expect_response(
                lambda r: LOGIN_API_PATH in r.url and r.request.method == "POST",
                timeout=TIMEOUT,
            ) as resp_info:
                print("[login] submitting login form ...")
                await _click(page, SUBMIT_BUTTON_SELECTOR, TIMEOUT)
            response = await resp_info.value
        except Exception as e:
            print(f"[login] failed     : login request err : {e}")
            await browser.close()
            return False

        if response.status != 200:
            print(f"[login] failed     : login request returned HTTP {response.status}")
            await browser.close()
            return False

        print("[login] login succeeded — waiting for page to finish loading ...")

        try:
            await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        except Exception:
            pass

        # --- Extract cookie (as before) ---
        cookies = await context.cookies()
        cookie  = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        if not cookie:
            await browser.close()
            print("[login] failed     : no cookies captured")
            return False

        # --- Extract sportradar token from today page ---
        sr_token = await _extract_sr_token(page)
        if sr_token:
            print(f"[login] sr_token   : extracted ({len(sr_token)} chars)")
        else:
            print("[login] sr_token   : extraction failed — will save cookie only")

        await browser.close()

    # --- Persist to bet_arb ---
    update_data = {"cookie": cookie}
    if sr_token:
        update_data["sr_token"] = sr_token

    try:
        sb = _get_supabase()
        res = (
            sb.table("bet_arb")
            .update(update_data)
            .eq("id", ROW_ID)
            .execute()
        )
        if not res.data:
            print(f"[login] db error   : no bet_arb row with id={ROW_ID}")
            return False
        cols = ", ".join(update_data.keys())
        print(f"[login] success    : {cols} saved to bet_arb.id={ROW_ID}")
    except Exception as e:
        print(f"[login] db error   : {e}")
        return False

    return True


if __name__ == "__main__":
    asyncio.run(login())