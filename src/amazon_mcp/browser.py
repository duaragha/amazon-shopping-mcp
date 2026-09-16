"""Browser lifecycle and backend management for Amazon MCP.

Supports standard Playwright (default) and Camoufox (anti-detect Firefox).
Controlled via the AMAZON_BROWSER_BACKEND environment variable.
"""

from __future__ import annotations

import os
import re
from typing import Literal

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

# ---------------------------------------------------------------------------
# Config & Defaults
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

_MEDIA_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|mp4|webm)(\?|$)", re.I
)

# Run the logged-in context headless. Login itself is always headful.
HEADLESS = os.environ.get("AMAZON_HEADLESS", "1") != "0"


def get_browser_backend() -> Literal["playwright", "camoufox"]:
    """Determine the active browser engine ('playwright' or 'camoufox')."""
    backend = (
        os.environ.get("AMAZON_BROWSER_BACKEND")
        or os.environ.get("AMAZON_BROWSER_ENGINE")
        or os.environ.get("BROWSER_BACKEND")
        or os.environ.get("AMAZON_BROWSER")
        or "playwright"
    ).strip().lower()

    if backend in ("camoufox", "firefox", "firefox_camoufox"):
        return "camoufox"
    return "playwright"


BROWSER_BACKEND = get_browser_backend()


def get_user_data_dir() -> str:
    """Return the profile directory for the persistent logged-in browser."""
    custom = os.environ.get("AMAZON_USER_DATA_DIR")
    if custom:
        return os.path.abspath(os.path.expanduser(custom))
    base = os.path.expanduser("~/.amazon-mcp/user-data")
    if get_browser_backend() == "camoufox":
        return os.path.join(base, "camoufox")
    return base


USER_DATA_DIR = get_user_data_dir()

# ---------------------------------------------------------------------------
# Anonymous Browser Management (for search / details / reviews)
# ---------------------------------------------------------------------------

_browser: Browser | None = None
_pw = None
_camoufox_browser_cm = None


async def get_browser() -> Browser:
    """Get or launch the shared browser instance for throwaway pages."""
    global _browser, _pw, _camoufox_browser_cm

    if _browser is not None and _browser.is_connected():
        return _browser

    backend = get_browser_backend()

    if backend == "camoufox":
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:
            raise ImportError(
                "Camoufox is configured as the browser backend, but 'camoufox' is not installed. "
                "Install it with: uv sync --extra camoufox (or pip install 'amazon-mcp[camoufox]') "
                "and ensure the browser binary is fetched via: camoufox fetch"
            ) from exc

        _camoufox_browser_cm = AsyncCamoufox(headless=True, geoip=True)
        _browser = await _camoufox_browser_cm.__aenter__()
        return _browser

    # Default: Playwright Chromium
    _pw = await async_playwright().start()
    channel = os.environ.get("BROWSER_CHANNEL")  # e.g. "msedge", "chrome"
    _browser = await _pw.chromium.launch(
        **({"channel": channel} if channel else {}),
        headless=True,
    )
    return _browser


async def create_page(block_media: bool = False) -> Page:
    """Create a new page in a fresh context, optionally blocking images/media."""
    browser = await get_browser()
    backend = get_browser_backend()

    if backend == "camoufox":
        # Camoufox pre-configures its stealth context with GeoIP & fingerprints
        page = await browser.new_page()
    else:
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-CA",
        )
        page = await context.new_page()

    if block_media:
        await page.context.route(_MEDIA_RE, lambda route: route.abort())

    return page


# ---------------------------------------------------------------------------
# Persistent Logged-in Context (for account / cart / checkout)
# ---------------------------------------------------------------------------

_ctx: BrowserContext | None = None
_ctx_pw = None
_camoufox_ctx_cm = None


async def get_logged_in_context(headless: bool | None = None) -> BrowserContext:
    """Return the persistent, logged-in browser context (launching it if needed).

    All pages share the same cookies/session, persisted under USER_DATA_DIR.
    """
    global _ctx, _ctx_pw, _camoufox_ctx_cm

    if _ctx is not None:
        return _ctx

    user_data_dir = get_user_data_dir()
    os.makedirs(user_data_dir, exist_ok=True)
    is_headless = HEADLESS if headless is None else headless
    backend = get_browser_backend()

    if backend == "camoufox":
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:
            raise ImportError(
                "Camoufox is configured as the browser backend, but 'camoufox' is not installed. "
                "Install it with: uv sync --extra camoufox (or pip install 'amazon-mcp[camoufox]') "
                "and ensure the browser binary is fetched via: camoufox fetch"
            ) from exc

        _camoufox_ctx_cm = AsyncCamoufox(
            headless=is_headless,
            persistent_context=True,
            user_data_dir=user_data_dir,
            geoip=True,
            locale="en-CA",
        )
        _ctx = await _camoufox_ctx_cm.__aenter__()
        await _ctx.route(_MEDIA_RE, lambda route: route.abort())
        return _ctx

    # Default: Playwright Chromium persistent context
    if _ctx_pw is None:
        _ctx_pw = await async_playwright().start()

    channel = os.environ.get("BROWSER_CHANNEL")
    kwargs = {
        "headless": is_headless,
        "user_agent": _USER_AGENT,
        "viewport": {"width": 1920, "height": 1080},
        "locale": "en-CA",
        # Anti-bot: drop the most obvious headless/automation tells. Pairs with the
        # init script below. The real-profile cookies do most of the trust-building;
        # this stops Amazon's fingerprinter from flagging the automation flags.
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    }
    if channel:
        kwargs["channel"] = channel

    _ctx = await _ctx_pw.chromium.launch_persistent_context(user_data_dir, **kwargs)

    # Mask the leftover webdriver/automation signals before any page script runs.
    await _ctx.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        window.chrome = window.chrome || { runtime: {} };
        """
    )
    # No logged-in flow needs images/fonts/video — drop them for speed.
    await _ctx.route(_MEDIA_RE, lambda route: route.abort())
    return _ctx


async def close_logged_in_context() -> None:
    """Close the persistent context, flushing the session profile to disk.

    The browser profile is locked while open, so login (which needs a second,
    headful window on the same profile) must close it first; the next call to
    get_logged_in_context() transparently reopens it.
    """
    global _ctx, _ctx_pw, _camoufox_ctx_cm

    if _camoufox_ctx_cm is not None:
        try:
            await _camoufox_ctx_cm.__aexit__(None, None, None)
        except Exception:
            pass
        finally:
            _camoufox_ctx_cm = None
            _ctx = None
    elif _ctx is not None:
        try:
            await _ctx.close()
        except Exception:
            pass
        finally:
            _ctx = None

    if _ctx_pw is not None:
        try:
            await _ctx_pw.stop()
        except Exception:
            pass
        finally:
            _ctx_pw = None


async def close_all_browsers() -> None:
    """Close both anonymous and persistent browser instances."""
    global _browser, _pw, _camoufox_browser_cm

    await close_logged_in_context()

    if _camoufox_browser_cm is not None:
        try:
            await _camoufox_browser_cm.__aexit__(None, None, None)
        except Exception:
            pass
        finally:
            _camoufox_browser_cm = None
            _browser = None
    elif _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        finally:
            _browser = None

    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:
            pass
        finally:
            _pw = None
