"""Browser lifecycle and backend management for Amazon MCP.

Supports standard Playwright (default Chromium) and Camoufox (anti-detect Firefox).
Controlled via the AMAZON_BROWSER_BACKEND environment variable.
Includes profile ownership locking, lifecycle serialization, and clean handle recovery.
"""

from __future__ import annotations

import asyncio
import atexit
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Literal

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProfileInUseError(RuntimeError):
    """Raised when the requested browser profile directory is locked by another process."""


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
# Locks & Concurrency Guards
# ---------------------------------------------------------------------------

# Serializes all authenticated operations (login, reset, account/order tools).
_auth_lock = asyncio.Lock()

# Guards browser initialization against concurrent launches.
_browser_init_lock = asyncio.Lock()
_ctx_init_lock = asyncio.Lock()

# Profile lock file handle
_profile_lock_file = None
_profile_lock_path: str | None = None


def acquire_profile_lock(user_data_dir: str) -> None:
    """Acquire an application-level lock for user_data_dir using flock.

    Does not delete browser locks or kill other processes. Raises ProfileInUseError
    with actionable context if another process holds the profile.
    """
    global _profile_lock_file, _profile_lock_path

    if _profile_lock_file is not None:
        return

    os.makedirs(user_data_dir, exist_ok=True)
    lock_path = os.path.join(user_data_dir, ".amazon_mcp.lock")
    lock_file = open(lock_path, "a+")

    try:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        lock_file.seek(0)
        holder_info = lock_file.read().strip()
        lock_file.close()
        msg = f"Browser profile directory '{user_data_dir}' is currently in use"
        if holder_info:
            msg += f" ({holder_info})"
        msg += (
            ". Ensure no other Amazon MCP server or browser is using this profile, "
            "or configure a different AMAZON_USER_DATA_DIR."
        )
        raise ProfileInUseError(msg) from exc

    lock_file.seek(0)
    lock_file.truncate()
    now_str = datetime.now(timezone.utc).isoformat()
    lock_file.write(f"PID {os.getpid()} acquired at {now_str}\n")
    lock_file.flush()

    _profile_lock_file = lock_file
    _profile_lock_path = lock_path


def release_profile_lock() -> None:
    """Release the application-level lock for the profile directory."""
    global _profile_lock_file, _profile_lock_path

    if _profile_lock_file is not None:
        try:
            import fcntl

            fcntl.flock(_profile_lock_file.fileno(), fcntl.LOCK_UN)
            _profile_lock_file.close()
        except Exception:
            pass
        finally:
            _profile_lock_file = None
            _profile_lock_path = None


def authenticated_lock() -> asyncio.Lock:
    """Return the shared lock for serializing authenticated operations."""
    return _auth_lock


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

    async with _browser_init_lock:
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

            def _on_disconnected():
                global _browser
                _browser = None

            _browser.on("disconnected", lambda _: _on_disconnected())
            return _browser

        # Default: Playwright Chromium
        _pw = await async_playwright().start()
        channel = os.environ.get("BROWSER_CHANNEL")  # e.g. "msedge", "chrome"
        _browser = await _pw.chromium.launch(
            **({"channel": channel} if channel else {}),
            headless=True,
        )

        def _on_pw_disconnected():
            global _browser
            _browser = None

        _browser.on("disconnected", lambda _: _on_pw_disconnected())
        return _browser


async def create_page(block_media: bool = False) -> Page:
    """Create a new page in a fresh context, optionally blocking images/media."""
    browser = await get_browser()
    backend = get_browser_backend()

    if backend == "camoufox":
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
# Persistent Logged-in Context (for account / cart / checkout / orders)
# ---------------------------------------------------------------------------

_ctx: BrowserContext | None = None
_ctx_pw = None
_camoufox_ctx_cm = None


async def get_logged_in_context(headless: bool | None = None) -> BrowserContext:
    """Return the persistent, logged-in browser context (launching it if needed).

    All pages share the same cookies/session, persisted under USER_DATA_DIR.
    Guarded against concurrent launches and profile ownership conflicts.
    """
    global _ctx, _ctx_pw, _camoufox_ctx_cm

    if _ctx is not None:
        return _ctx

    async with _ctx_init_lock:
        if _ctx is not None:
            return _ctx

        user_data_dir = get_user_data_dir()
        acquire_profile_lock(user_data_dir)

        is_headless = HEADLESS if headless is None else headless
        backend = get_browser_backend()

        try:
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

                def _on_camoufox_close():
                    global _ctx
                    _ctx = None
                    release_profile_lock()

                _ctx.on("close", lambda _: _on_camoufox_close())
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
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            }
            if channel:
                kwargs["channel"] = channel

            _ctx = await _ctx_pw.chromium.launch_persistent_context(user_data_dir, **kwargs)

            def _on_pw_ctx_close():
                global _ctx
                _ctx = None
                release_profile_lock()

            _ctx.on("close", lambda _: _on_pw_ctx_close())

            await _ctx.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = window.chrome || { runtime: {} };
                """
            )
            await _ctx.route(_MEDIA_RE, lambda route: route.abort())
            return _ctx
        except Exception:
            # Clean up lock and handles on launch failure
            release_profile_lock()
            _ctx = None
            if _camoufox_ctx_cm is not None:
                try:
                    await _camoufox_ctx_cm.__aexit__(None, None, None)
                except Exception:
                    pass
                finally:
                    _camoufox_ctx_cm = None
            if _ctx_pw is not None:
                try:
                    await _ctx_pw.stop()
                except Exception:
                    pass
                finally:
                    _ctx_pw = None
            raise


async def close_logged_in_context() -> None:
    """Close the persistent context, flushing the session profile to disk.

    Releases the application-level profile lock.
    """
    global _ctx, _ctx_pw, _camoufox_ctx_cm

    try:
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
    finally:
        release_profile_lock()


async def close_all_browsers() -> None:
    """Close both anonymous and persistent browser instances and release locks."""
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


async def reset_session() -> dict[str, Any]:
    """Close this server's owned browser instances while preserving saved authentication.

    Acquires the authenticated-operation lock. Callers must not already hold it:
    asyncio.Lock is not reentrant, so a second acquire from the same task waits forever.

    Releases profile locks and flushes cookies to disk without replaying purchases
    or altering stored account credentials.
    """
    async with _auth_lock:
        await close_all_browsers()
        return {
            "session_reset": True,
            "message": "Browser session closed. Profile preserved on disk.",
            "user_data_dir": get_user_data_dir(),
        }


# Register synchronous cleanup at process exit
def _sync_cleanup() -> None:
    release_profile_lock()


atexit.register(_sync_cleanup)
