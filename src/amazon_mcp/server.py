"""Amazon Shopping MCP Server — search, compare, review, and buy Amazon products.

Anonymous tools (search / details / reviews) run in throwaway headless contexts.
The buying tools (login / addresses / payment methods / cart / checkout) run in a
single *persistent* logged-in browser profile so your Amazon session survives
between calls. You log in once via `amazon_login`; cookies are saved to disk.

Security note: this never asks for or stores raw card numbers. It uses the cards
and addresses already saved in your Amazon account, and only ever sees the last 4
digits Amazon itself displays.
"""

import asyncio
import json
import os
import re
from urllib.parse import quote_plus

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Where the logged-in Chromium profile (cookies/session) lives on disk.
USER_DATA_DIR = os.environ.get(
    "AMAZON_USER_DATA_DIR", os.path.expanduser("~/.amazon-mcp/user-data")
)
# Default storefront for account/cart/checkout flows.
DEFAULT_DOMAIN = os.environ.get("AMAZON_DOMAIN", "ca")
# Run the logged-in context headless. Login itself is always headful.
HEADLESS = os.environ.get("AMAZON_HEADLESS", "1") != "0"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
_MEDIA_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|mp4|webm)(\?|$)", re.I
)

# ---------------------------------------------------------------------------
# Browser management
# ---------------------------------------------------------------------------

_browser: Browser | None = None
_pw = None


async def get_browser() -> Browser:
    global _browser, _pw
    if _browser is None or not _browser.is_connected():
        _pw = await async_playwright().start()
        channel = os.environ.get("BROWSER_CHANNEL")  # e.g. "msedge", "chrome"
        _browser = await _pw.chromium.launch(
            **({"channel": channel} if channel else {}),
            headless=True,
        )
    return _browser


async def create_page(block_media: bool = False) -> Page:
    browser = await get_browser()
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="en-CA",
    )
    if block_media:
        await context.route(_MEDIA_RE, lambda route: route.abort())
    page = await context.new_page()
    return page


# ---------------------------------------------------------------------------
# Persistent logged-in context (for account / cart / checkout)
# ---------------------------------------------------------------------------

_ctx: BrowserContext | None = None
_ctx_pw = None


async def get_logged_in_context(headless: bool | None = None) -> BrowserContext:
    """Return the persistent, logged-in browser context (launching it if needed).

    All pages share the same cookies/session, persisted under USER_DATA_DIR.
    """
    global _ctx, _ctx_pw
    if _ctx is not None:
        return _ctx
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    if _ctx_pw is None:
        _ctx_pw = await async_playwright().start()
    channel = os.environ.get("BROWSER_CHANNEL")
    kwargs = {
        "headless": HEADLESS if headless is None else headless,
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
    _ctx = await _ctx_pw.chromium.launch_persistent_context(USER_DATA_DIR, **kwargs)
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

    The Chromium profile is locked while open, so login (which needs a second,
    headful window on the same profile) must close it first; the next call to
    get_logged_in_context() transparently reopens it.
    """
    global _ctx, _ctx_pw
    if _ctx is not None:
        try:
            await _ctx.close()
        finally:
            _ctx = None
    if _ctx_pw is not None:
        try:
            await _ctx_pw.stop()
        finally:
            _ctx_pw = None


async def _account_name(page: Page) -> str | None:
    """Return the 'Hello, <name>' account label, or None if not present."""
    el = await page.query_selector("#nav-link-accountList-nav-line-1")
    if not el:
        return None
    return (await el.inner_text()).strip()


def _is_logged_in(name: str | None) -> bool:
    return bool(name) and "sign in" not in name.lower()


async def _dismiss_popups(page: Page) -> None:
    """Best-effort dismissal of add-on / warranty / interstitial popups."""
    for sel in (
        "#attachSiNoCoverage a",
        "#attachSiNoCoverage input",
        "#siNoCoverage-announce",
        'input[aria-labelledby="attachSiNoCoverage-announce"]',
        "#a-popover-content-3 .a-button-close",
        'button[data-action="a-popover-close"]',
    ):
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click(timeout=2000)
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def _add_via_buying_options(page: Page) -> str | None:
    """For items with no featured buy box: open "See all buying options" and add
    the first offer to the cart. Returns a label on success, None on failure.
    """
    opened = False
    for sel in (
        "#buybox-see-all-buying-choices a",
        "#buybox-see-all-buying-choices-announce",
        'a[title*="See All Buying Options"]',
        "#buybox-see-all-buying-choices",
    ):
        el = await page.query_selector(sel)
        if el:
            await el.click()
            opened = True
            break
    if not opened:
        return None
    # Wait for the All-Offers-Display panel, then add the first offer.
    try:
        await page.wait_for_selector(
            "#aod-offer, #all-offers-display, #aod-offer-list", timeout=8000
        )
    except Exception:
        return None
    await page.wait_for_timeout(800)
    for sel in (
        '#aod-offer input[name="submit.addToCart"]',
        "#aod-offer .a-button-input",
        '#aod-offer-list input[name="submit.addToCart"]',
        'input[name="submit.addToCart"]',
    ):
        btn = await page.query_selector(sel)
        if btn:
            await btn.click()
            return "buying-options offer"
    return None


async def check_captcha(page: Page) -> str | None:
    # Detect the bot wall by its actual form/input — NOT by fuzzy title words.
    # (Substring matching falsely flagged product titles like "...Syrup Bottle"
    # because "Bottle" contains "bot".)
    if await page.query_selector(
        'form[action*="validateCaptcha"], input#captchacharacters'
    ):
        return (
            "Amazon is showing a CAPTCHA / bot-check page. Try again in a moment."
        )
    title = (await page.title()).lower()
    if title.startswith("robot check") or title.startswith("sorry! something went wrong"):
        return (
            "Amazon is showing a CAPTCHA / bot-check page. Try again in a moment."
        )
    return None


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("amazon-shopping")


@mcp.tool()
async def amazon_search(
    query: str, domain: str = "ca", max_results: int = 20
) -> str:
    """Search Amazon for products.

    Returns listings with title, price, rating, review count, Prime status,
    and product URL.

    Args:
        query: Search terms, e.g. "wireless noise cancelling headphones"
        domain: Amazon domain — "ca" for Canada, "com" for US (default: ca)
        max_results: Maximum number of results to return (default: 20)
    """
    page = await create_page(block_media=True)
    try:
        url = f"https://www.amazon.{domain}/s?k={quote_plus(query)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})

        await page.wait_for_selector(
            'div[data-component-type="s-search-result"]', timeout=10000
        )

        results = await page.evaluate(
            """
            (maxResults) => {
                const items = document.querySelectorAll(
                    'div[data-component-type="s-search-result"]'
                );
                const results = [];
                for (const item of Array.from(items).slice(0, maxResults)) {
                    const asin = item.dataset.asin;
                    if (!asin) continue;

                    const titleEl = item.querySelector('h2 span');
                    if (!titleEl) continue;

                    const priceEl = item.querySelector('.a-price .a-offscreen');
                    const ratingEl = item.querySelector('.a-icon-alt');
                    const reviewLinkEl = item.querySelector(
                        'a[href*="customerReviews"], a[href*="Reviews"]'
                    );
                    const reviewCount = reviewLinkEl?.textContent?.trim()
                        || reviewLinkEl?.getAttribute('aria-label')
                        || '0';
                    const h2 = item.querySelector('h2');
                    const linkEl = h2?.closest('a') || h2?.parentElement;
                    const imgEl = item.querySelector('img.s-image');
                    const primeEl = item.querySelector(
                        '[aria-label*="Prime"], .s-prime, .aok-relative.s-icon-text-medium'
                    );

                    const href = linkEl?.getAttribute?.('href') || '';
                    const fullUrl = href.startsWith('http')
                        ? href
                        : href
                            ? window.location.origin + href
                            : '';

                    results.push({
                        asin,
                        title: titleEl.textContent.trim(),
                        price: priceEl?.textContent?.trim() || 'N/A',
                        rating: ratingEl?.textContent?.trim() || 'N/A',
                        review_count: reviewCount,
                        url: fullUrl,
                        image: imgEl?.src || '',
                        prime: !!primeEl,
                    });
                }
                return results;
            }
            """,
            max_results,
        )

        return json.dumps(
            {
                "query": query,
                "domain": domain,
                "result_count": len(results),
                "products": results,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.context.close()


@mcp.tool()
async def amazon_product_details(urls: list[str]) -> str:
    """Get detailed product information from one or more Amazon product pages.

    Scrapes specs, features, description, price, available colors/sizes,
    brand, availability, and image URLs. Accepts multiple URLs and fetches
    them in parallel for efficient comparison.

    Args:
        urls: List of Amazon product page URLs
    """

    async def scrape_one(url: str) -> dict:
        page = await create_page(block_media=False)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            captcha = await check_captcha(page)
            if captcha:
                return {"url": url, "error": captcha}

            await page.wait_for_selector("#productTitle", timeout=10000)

            details = await page.evaluate(
                """
                () => {
                    const text = (sel) => {
                        const el = document.querySelector(sel);
                        return el ? el.textContent.trim() : null;
                    };
                    const texts = (sel) =>
                        Array.from(document.querySelectorAll(sel))
                            .map(el => el.textContent.trim())
                            .filter(Boolean);

                    // --- Basic info ---
                    const title = text('#productTitle');
                    const brand = text('#bylineInfo') || text('.po-brand .a-span9 span');

                    // --- Price ---
                    const price =
                        text('#corePriceDisplay_desktop_feature_div .a-offscreen') ||
                        text('.a-price .a-offscreen') ||
                        text('#priceblock_ourprice') ||
                        'N/A';
                    const listPrice = text('.basisPrice .a-offscreen') || null;

                    // --- Rating ---
                    const rating =
                        text('#acrPopover span.a-icon-alt') ||
                        text('i.a-icon-star span.a-icon-alt') ||
                        'N/A';
                    const reviewCount = text('#acrCustomerReviewText') || '0';

                    // --- Feature bullets ---
                    const features = texts(
                        '#feature-bullets ul li:not(.aok-hidden) span.a-list-item'
                    );

                    // --- Description ---
                    const description =
                        text('#productDescription p') ||
                        text('#productDescription span') ||
                        text('#productDescription') ||
                        null;

                    // --- Tech specs (multiple table formats) ---
                    const specs = {};
                    document
                        .querySelectorAll(
                            '#productDetails_techSpec_section_1 tr, ' +
                            '#prodDetails tr, ' +
                            'table.a-keyvalue tr, ' +
                            '#poExpander tr'
                        )
                        .forEach(row => {
                            const key = (
                                row.querySelector('th') ||
                                row.querySelector('td:first-child')
                            )?.textContent?.trim();
                            const val = row
                                .querySelector('td:last-child')
                                ?.textContent?.trim();
                            if (key && val && key !== val) specs[key] = val;
                        });

                    // Detail bullets (alternative layout)
                    document
                        .querySelectorAll(
                            '#detailBullets_feature_div li span.a-list-item'
                        )
                        .forEach(li => {
                            const spans = li.querySelectorAll('span');
                            if (spans.length >= 2) {
                                const key = spans[0].textContent
                                    .replace(/[:\\s]+$/g, '')
                                    .trim();
                                const val = spans[1].textContent.trim();
                                if (key && val) specs[key] = val;
                            }
                        });

                    // --- Color variants ---
                    const colors = [];
                    document
                        .querySelectorAll('#variation_color_name li img')
                        .forEach(el => {
                            const c = el.getAttribute('alt');
                            if (c) colors.push(c);
                        });
                    document
                        .querySelectorAll(
                            '#variation_color_name option, ' +
                            '#native_dropdown_selected_color_name option'
                        )
                        .forEach(el => {
                            const c = el.textContent.trim();
                            if (c && c !== 'Select' && c !== '') colors.push(c);
                        });
                    const selectedColor =
                        text('#variation_color_name .selection') || null;

                    // --- Size variants ---
                    const sizes = [];
                    document
                        .querySelectorAll(
                            '#variation_size_name li span.a-size-base, ' +
                            '#variation_size_name option, ' +
                            '#native_dropdown_selected_size_name option'
                        )
                        .forEach(el => {
                            const s = el.textContent.trim();
                            if (s && !s.startsWith('Select')) sizes.push(s);
                        });

                    // --- Availability ---
                    const availability =
                        text('#availability span') ||
                        text('#availability') ||
                        null;

                    // --- Images ---
                    const imgSet = new Set();
                    document
                        .querySelectorAll(
                            '#altImages img, #imageBlock img, #landingImage'
                        )
                        .forEach(img => {
                            let src =
                                img.dataset?.oldHires || img.src || '';
                            if (
                                !src ||
                                src.includes('sprite') ||
                                src.includes('grey-pixel') ||
                                src.includes('loading')
                            )
                                return;
                            src = src.replace(/\\._[A-Za-z0-9_]+_\\./, '.');
                            imgSet.add(src);
                        });
                    const mainImg = document.querySelector('#landingImage');
                    if (mainImg) {
                        const hi = mainImg.dataset?.oldHires || mainImg.src;
                        if (hi) imgSet.add(hi);
                    }

                    // --- "About this item" (A+ content) ---
                    const aboutItems = texts(
                        '#aplus_feature_div .aplus-v2 p, #aplus_feature_div li'
                    );

                    return {
                        title,
                        brand,
                        price,
                        listPrice,
                        rating,
                        reviewCount,
                        features,
                        description,
                        aboutItems: aboutItems.length > 0 ? aboutItems : null,
                        specs,
                        colors: [...new Set(colors)],
                        selectedColor,
                        sizes: [...new Set(sizes)],
                        availability,
                        images: [...imgSet].slice(0, 6),
                    };
                }
                """
            )

            details["url"] = url
            asin_match = re.search(r"/dp/([A-Z0-9]{10})", url)
            if asin_match:
                details["asin"] = asin_match.group(1)
            return details
        except Exception as e:
            return {"url": url, "error": str(e)}
        finally:
            await page.context.close()

    sem = asyncio.Semaphore(8)

    async def bounded(u: str):
        async with sem:
            return await scrape_one(u)

    results = await asyncio.gather(*[bounded(u) for u in urls])
    return json.dumps({"products": list(results)}, indent=2)


@mcp.tool()
async def amazon_product_reviews(url: str, max_reviews: int = 15) -> str:
    """Get reviews for an Amazon product.

    Scrapes reviews from the product page itself (the dedicated reviews page
    requires sign-in). Returns rating distribution, overall rating, and
    individual reviews with title, body, rating, date, and verified status.

    Args:
        url: Amazon product page URL
        max_reviews: Max reviews to fetch (default: 15)
    """
    page = await create_page(block_media=True)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})

        await page.wait_for_selector("#productTitle", timeout=10000)

        data = await page.evaluate(
            """
            (maxReviews) => {
                // --- Rating distribution from histogram ---
                const distribution = {};
                document
                    .querySelectorAll('#histogramTable li, #histogramTable tr')
                    .forEach(el => {
                        const link = el.querySelector('a[aria-label]');
                        if (link) {
                            const label = link.getAttribute('aria-label');
                            if (label) {
                                const match = label.match(
                                    /(\\d+)\\s*percent.*?(\\d+)\\s*star/i
                                );
                                if (match) {
                                    distribution[match[2] + ' star'] =
                                        match[1] + '%';
                                }
                            }
                        }
                    });

                // --- Overall rating ---
                const overallRating =
                    document
                        .querySelector('[data-hook="rating-out-of-text"]')
                        ?.textContent?.trim() || '';
                const totalReviews =
                    document
                        .querySelector('[data-hook="total-review-count"]')
                        ?.textContent?.trim() || '';

                // --- Individual reviews ---
                const reviews = [];
                document
                    .querySelectorAll('[data-hook="review"]')
                    .forEach((rev, i) => {
                        if (i >= maxReviews) return;

                        const ratingEl = rev.querySelector(
                            'i[data-hook="review-star-rating"] span'
                        );
                        // Title is in the last <span> inside the title link,
                        // after the star-rating icon and a letter-space span.
                        const titleSpans = rev.querySelectorAll(
                            '[data-hook="review-title"] span'
                        );
                        const titleEl =
                            titleSpans.length > 0
                                ? titleSpans[titleSpans.length - 1]
                                : null;
                        const bodyEl = rev.querySelector(
                            '[data-hook="review-body"] span'
                        );
                        const dateEl = rev.querySelector(
                            '[data-hook="review-date"]'
                        );
                        const helpfulEl = rev.querySelector(
                            '[data-hook="helpful-vote-statement"]'
                        );
                        const verifiedEl =
                            rev.querySelector(
                                '[data-hook="avp-badge"]'
                            ) ||
                            rev.querySelector(
                                '[data-hook="avp-badge-linkless"]'
                            );

                        reviews.push({
                            rating:
                                ratingEl?.textContent?.trim() || '',
                            title:
                                titleEl?.textContent?.trim() || '',
                            body:
                                bodyEl?.textContent
                                    ?.trim()
                                    ?.slice(0, 1500) || '',
                            date:
                                dateEl?.textContent?.trim() || '',
                            helpful:
                                helpfulEl?.textContent?.trim() || '',
                            verified: !!verifiedEl,
                        });
                    });

                return {
                    overallRating,
                    totalReviews,
                    distribution,
                    reviews,
                };
            }
            """,
            max_reviews,
        )

        asin_match = re.search(r"/dp/([A-Z0-9]{10})", url)
        if asin_match:
            data["asin"] = asin_match.group(1)
        data["url"] = url
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.context.close()


@mcp.tool()
async def amazon_login_status(domain: str = DEFAULT_DOMAIN) -> str:
    """Check whether the saved Amazon session is still logged in.

    Args:
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        await page.goto(
            f"https://www.amazon.{domain}/", wait_until="domcontentloaded", timeout=30000
        )
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})
        name = await _account_name(page)
        return json.dumps(
            {
                "logged_in": _is_logged_in(name),
                "account": name if _is_logged_in(name) else None,
                "hint": None
                if _is_logged_in(name)
                else "Run amazon_login to sign in (opens a real browser window).",
            }
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.close()


@mcp.tool()
async def amazon_login(domain: str = DEFAULT_DOMAIN, timeout_seconds: int = 240) -> str:
    """Open a real (visible) browser window so you can sign in to Amazon by hand.

    Handles password + 2FA / OTP manually — those can't and shouldn't be
    automated. Once you're signed in, the session is saved to disk and every
    other buying tool reuses it; you won't need to log in again until it expires.

    Args:
        domain: Amazon domain — "ca" for Canada, "com" for US.
        timeout_seconds: How long to wait for you to finish logging in.
    """
    # A headful login window needs exclusive use of the profile dir, so release
    # any headless context first.
    await close_logged_in_context()
    ctx = await get_logged_in_context(headless=False)
    page = await ctx.new_page()
    try:
        await page.goto(
            f"https://www.amazon.{domain}/gp/sign-in.html",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        waited = 0
        while waited < timeout_seconds:
            try:
                name = await _account_name(page)
            except Exception:
                name = None
            if _is_logged_in(name):
                await close_logged_in_context()  # flush cookies to disk
                return json.dumps(
                    {"logged_in": True, "account": name, "message": "Signed in. Session saved."}
                )
            await page.wait_for_timeout(3000)
            waited += 3
        await close_logged_in_context()
        return json.dumps(
            {
                "logged_in": False,
                "error": f"Did not detect a completed login within {timeout_seconds}s. "
                "Run amazon_login again and finish signing in.",
            }
        )
    except Exception as e:
        await close_logged_in_context()
        return json.dumps({"error": str(e)})


@mcp.tool()
async def amazon_list_addresses(domain: str = DEFAULT_DOMAIN) -> str:
    """List the shipping addresses saved in your Amazon account.

    Each address gets an `index` you can pass to amazon_place_order, plus the
    full text so you can refer to it by name/street. Requires being logged in.

    Args:
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        await page.goto(
            f"https://www.amazon.{domain}/a/addresses",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})
        name = await _account_name(page)
        if not _is_logged_in(name):
            return json.dumps({"error": "Not logged in. Run amazon_login first."})

        addresses = await page.evaluate(
            """
            () => {
                const out = [];
                const cards = document.querySelectorAll(
                    '.address-book-entry, [class*="address-tile"], li.a-spacing-base .a-box-inner'
                );
                cards.forEach(card => {
                    const isDefault = !!card.querySelector(
                        '[class*="default"], .a-color-success'
                    );
                    const lines = Array.from(
                        card.querySelectorAll('.a-row, li, div')
                    )
                        .map(el => el.childNodes.length &&
                            el.querySelector('.a-row, li, div')
                                ? '' : (el.textContent || '').trim())
                        .filter(Boolean);
                    const text = card.innerText
                        ? card.innerText.replace(/\\s*\\n\\s*/g, ', ').trim()
                        : lines.join(', ');
                    if (text && text.length > 5 &&
                        !/^Add address/i.test(text)) {
                        out.push({ text, default: isDefault });
                    }
                });
                // Dedupe by text.
                const seen = new Set();
                return out.filter(a => {
                    if (seen.has(a.text)) return false;
                    seen.add(a.text);
                    return true;
                });
            }
            """
        )
        for i, a in enumerate(addresses):
            a["index"] = i
        return json.dumps(
            {
                "count": len(addresses),
                "addresses": addresses,
                "note": "Pass `index` (or distinctive text) as address_hint to amazon_place_order."
                if addresses
                else "No addresses parsed — Amazon may have changed its layout. Try amazon_login again.",
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.close()


@mcp.tool()
async def amazon_list_payment_methods(domain: str = DEFAULT_DOMAIN) -> str:
    """List the payment cards saved in your Amazon wallet (last 4 digits only).

    Never exposes full card numbers — Amazon doesn't show them and neither does
    this tool. Each card gets an `index` you can pass to amazon_place_order.

    Args:
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        await page.goto(
            f"https://www.amazon.{domain}/cpe/yourpayments/wallet",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})
        name = await _account_name(page)
        if not _is_logged_in(name):
            return json.dumps({"error": "Not logged in. Run amazon_login first."})

        cards = await page.evaluate(
            """
            () => {
                const out = [];
                const tiles = document.querySelectorAll(
                    '.pmts-wallet-tab-content .a-box, [class*="instrument"], .pmts-payments-instrument-details'
                );
                tiles.forEach(t => {
                    const txt = (t.innerText || '').trim();
                    if (!txt) return;
                    const last4 = (txt.match(/(?:ending in|\\*{2,}\\s*|••••\\s*)(\\d{4})/i)
                        || txt.match(/(\\d{4})(?!.*\\d{4})/));
                    const expiry = txt.match(/(0?[1-9]|1[0-2])\\s*\\/\\s*(\\d{2,4})/);
                    const network = (txt.match(/\\b(Visa|Mastercard|Amex|American Express|Discover)\\b/i) || [])[0];
                    if (last4 || network) {
                        out.push({
                            last4: last4 ? last4[1] : null,
                            network: network || null,
                            expiry: expiry ? expiry[0] : null,
                            label: txt.replace(/\\s*\\n\\s*/g, ' ').slice(0, 120),
                        });
                    }
                });
                const seen = new Set();
                return out.filter(c => {
                    const k = (c.network || '') + (c.last4 || '');
                    if (seen.has(k)) return false;
                    seen.add(k);
                    return true;
                });
            }
            """
        )
        for i, c in enumerate(cards):
            c["index"] = i
        return json.dumps(
            {
                "count": len(cards),
                "payment_methods": cards,
                "note": "Pass `index` (or e.g. 'Visa 1234') as payment_hint to amazon_place_order."
                if cards
                else "No cards parsed — Amazon may have changed its layout, or your wallet is empty.",
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.close()


@mcp.tool()
async def amazon_add_to_cart(
    asin: str | None = None,
    url: str | None = None,
    quantity: int = 1,
    domain: str = DEFAULT_DOMAIN,
) -> str:
    """Add a product to your Amazon cart. Requires being logged in.

    Args:
        asin: 10-char product ASIN (e.g. "B08N5WRWNW"). Either this or url.
        url: Full product page URL. Either this or asin.
        quantity: How many to add (default 1).
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    if not asin and not url:
        return json.dumps({"error": "Provide either an asin or a url."})
    if not url:
        url = f"https://www.amazon.{domain}/dp/{asin}"

    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})

        title = await page.query_selector("#productTitle")
        title_text = (await title.inner_text()).strip() if title else None

        if quantity and quantity > 1:
            try:
                await page.select_option("#quantity", str(quantity), timeout=3000)
            except Exception:
                pass  # some listings have no quantity selector

        atc = await page.query_selector("#add-to-cart-button")
        offer_used = None
        if atc:
            await atc.click()
        else:
            # No featured buy box — the item sells via "See all buying options".
            # Open the offers panel and add the first (top / cheapest-shown) offer.
            offer_used = await _add_via_buying_options(page)
            if not offer_used:
                avail = await page.query_selector("#availability")
                msg = (await avail.inner_text()).strip() if avail else "unknown"
                return json.dumps(
                    {
                        "error": "No add-to-cart button and no buyable offer found "
                        "(item may be unavailable or seller-restricted).",
                        "availability": msg,
                        "url": url,
                    }
                )
        await page.wait_for_timeout(1500)
        await _dismiss_popups(page)

        count_el = await page.query_selector("#nav-cart-count")
        cart_count = (await count_el.inner_text()).strip() if count_el else None
        return json.dumps(
            {
                "added": True,
                "title": title_text,
                "quantity": quantity,
                "via": offer_used or "buy box",
                "cart_count": cart_count,
                "url": url,
            }
        )
    except Exception as e:
        return json.dumps({"error": str(e), "url": url})
    finally:
        await page.close()


@mcp.tool()
async def amazon_view_cart(domain: str = DEFAULT_DOMAIN) -> str:
    """View your Amazon cart: line items, quantities, and subtotal.

    Args:
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        await page.goto(
            f"https://www.amazon.{domain}/gp/cart/view.html",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})
        name = await _account_name(page)
        if not _is_logged_in(name):
            return json.dumps({"error": "Not logged in. Run amazon_login first."})

        data = await page.evaluate(
            """
            () => {
                // Scope strictly to the active-cart container so we don't pick up
                // "Saved for later" rows or recommendation/"buy again" carousels.
                const root =
                    document.querySelector('#sc-active-cart') ||
                    document.querySelector('#activeCartViewForm') ||
                    document.querySelector('[data-name="Active Items"]');
                const items = [];
                if (root) {
                    // Real cart line items carry a data-asin; carousels/ads don't.
                    root.querySelectorAll('.sc-list-item[data-asin]').forEach(it => {
                        const asin = it.getAttribute('data-asin');
                        if (!asin) return;
                        const title = it.querySelector(
                            '.sc-product-title, .a-truncate-cut, .sc-grid-item-product-title, [data-feature-id="item-title"]'
                        )?.textContent?.trim();
                        const price = it.querySelector(
                            '.sc-product-price, .sc-badge-price-to-pay, .a-price .a-offscreen'
                        )?.textContent?.trim();
                        const qtyEl = it.querySelector(
                            '.sc-quantity-textfield, input.sc-update-quantity, .a-dropdown-prompt, [data-action="a-stepper"] input'
                        );
                        const qty = qtyEl
                            ? (qtyEl.value || qtyEl.textContent || '').trim()
                            : null;
                        if (title) items.push({ asin, title, price: price || null, quantity: qty });
                    });
                }
                // Dedupe by asin (cart can render hidden duplicate nodes).
                const seen = new Set();
                const deduped = items.filter(i => {
                    if (seen.has(i.asin)) return false;
                    seen.add(i.asin);
                    return true;
                });
                const subtotal =
                    document.querySelector('#sc-subtotal-amount-activecart .sc-price')?.textContent?.trim() ||
                    document.querySelector('#sc-subtotal-amount-buybox .sc-price')?.textContent?.trim() ||
                    document.querySelector('#sc-subtotal-amount-activecart')?.textContent?.trim() ||
                    document.querySelector('#sc-subtotal-amount-buybox')?.textContent?.trim() ||
                    null;
                const countText =
                    document.querySelector('#sc-subtotal-label-activecart, #sc-subtotal-label-buybox')
                        ?.textContent?.trim() || null;
                return { items: deduped, subtotal, countText };
            }
            """
        )
        data["item_count"] = len(data.get("items", []))
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.close()


@mcp.tool()
async def amazon_place_order(
    confirm: bool = False,
    address_hint: str | None = None,
    payment_hint: str | None = None,
    domain: str = DEFAULT_DOMAIN,
) -> str:
    """Proceed through checkout for the items in your cart and (optionally) place the order.

    SAFETY: by default (confirm=False) this does NOT place the order. It walks to
    the final checkout screen, applies your address/payment choices, and returns
    the order summary (items, ship-to, payment, total). Review it, then call again
    with confirm=true to actually place the order and spend money.

    Args:
        confirm: Must be true to actually place the order. Default false = preview only.
        address_hint: Which saved address to ship to — an index from
            amazon_list_addresses, or distinctive text (name/street) to match.
        payment_hint: Which saved card to use — an index from
            amazon_list_payment_methods, or text like "Visa 1234".
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        # Cart -> proceed to checkout.
        await page.goto(
            f"https://www.amazon.{domain}/gp/cart/view.html",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})
        name = await _account_name(page)
        if not _is_logged_in(name):
            return json.dumps({"error": "Not logged in. Run amazon_login first."})

        ptc = None
        for sel in (
            'input[name="proceedToRetailCheckout"]',
            "#sc-buy-box-ptc-button input",
            "#sc-buy-box-ptc-button",
            'a[href*="/gp/buy/spc"]',
        ):
            ptc = await page.query_selector(sel)
            if ptc:
                break
        if not ptc:
            return json.dumps(
                {"error": "Couldn't find 'Proceed to checkout' — is the cart empty?"}
            )
        await ptc.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)

        applied = await _apply_checkout_selections(page, address_hint, payment_hint)
        summary = await _read_checkout_summary(page)
        summary["selection_applied"] = applied

        if not confirm:
            summary["placed"] = False
            summary["next_step"] = (
                "Review the above. To actually place this order, call amazon_place_order "
                "again with confirm=true (same address_hint / payment_hint)."
            )
            return json.dumps(summary, indent=2)

        # confirm=True -> place the order.
        place_btn = None
        for sel in (
            'input[name="placeYourOrder1"]',
            "#placeYourOrder input",
            "#submitOrderButtonId input",
            "#bottomSubmitOrderButtonId input",
            '#placeOrder input[type="submit"]',
        ):
            place_btn = await page.query_selector(sel)
            if place_btn:
                break
        if not place_btn:
            summary["placed"] = False
            summary["error"] = (
                "Reached checkout but couldn't find the 'Place your order' button "
                "(Amazon may want a missing address/payment/CVV, or changed its layout)."
            )
            return json.dumps(summary, indent=2)

        await place_btn.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2500)

        confirmation = await page.evaluate(
            """
            () => {
                const body = document.body.innerText || '';
                const placed = /(order placed|thank you|order confirmation|your order)/i.test(body);
                const m = body.match(/Order\\s*#?\\s*([0-9-]{10,})/i);
                return { placed, orderNumber: m ? m[1] : null };
            }
            """
        )
        return json.dumps(
            {
                "placed": bool(confirmation.get("placed")),
                "order_number": confirmation.get("orderNumber"),
                "summary": summary,
                "message": "Order placed."
                if confirmation.get("placed")
                else "Clicked place-order but couldn't confirm success — check your Amazon orders page.",
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.close()


async def _apply_checkout_selections(
    page: Page, address_hint: str | None, payment_hint: str | None
) -> dict:
    """Pick the requested saved address/card on amazon.ca's "Chewbacca" checkout.

    Each section has a "Change" link (a.expand-panel-button) that navigates to a
    sub-page (/shipaddress or /pay) listing saved options; you pick one and click
    "Use this address/payment method" (#checkout-primary-continue-button-id) to
    return. Amazon pre-selects defaults, so a hint that already matches is left
    as-is. Returns what was attempted per section.
    """
    result = {"address": "default", "payment": "default"}
    if address_hint is not None:
        result["address"] = await _change_checkout_section(
            page,
            section_re="delivering to|shipping address|deliver to",
            already_sel="#deliver-to-address-text",
            option_box=".address-book-entry, [class*='address'] .a-radio, .a-radio",
            hint=str(address_hint),
        )
    if payment_hint is not None:
        result["payment"] = await _change_checkout_section(
            page,
            section_re="paying with|payment method",
            already_sel=None,  # payment display has no stable id; always verify on /pay
            option_box=".pmts-instrument-box",
            hint=str(payment_hint),
        )
    return result


async def _change_checkout_section(
    page: Page, section_re: str, already_sel: str | None, option_box: str, hint: str
) -> str:
    """Drive one Chewbacca "Change" panel to select the option matching `hint`.

    `hint` is matched as text (a card's last-4 or address name/street). A bare 1-2
    digit hint is treated as a positional index into the option list.
    """
    try:
        # Fast path: the currently-shown selection already matches the hint.
        if already_sel:
            cur = await page.query_selector(already_sel)
            if cur and hint.lower() in ((await cur.inner_text()) or "").lower():
                return f"already set to '{hint}'"

        # Find which "Change" link belongs to this section (closest ancestor whose
        # text matches wins, so the address link doesn't match via a shared parent),
        # then click it as a real element — Chewbacca's SPA nav ignores synthetic
        # in-page .click() but fires on a trusted Playwright click.
        idx = await page.evaluate(
            """
            (re) => {
                const rx = new RegExp(re, 'i');
                const links = [...document.querySelectorAll('a.expand-panel-button')];
                let best = -1, bestDepth = 999;
                links.forEach((a, i) => {
                    let sec = a, d = 0;
                    while (sec && d < 8) {
                        if (rx.test(sec.textContent || '')) {
                            if (d < bestDepth) { bestDepth = d; best = i; }
                            break;
                        }
                        sec = sec.parentElement; d++;
                    }
                });
                return best;
            }
            """,
            section_re,
        )
        if idx is None or idx < 0:
            return f"couldn't find Change link for /{section_re}/ — left default"
        links = await page.query_selector_all("a.expand-panel-button")
        await links[idx].click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2500)

        boxes = await page.query_selector_all(option_box)
        if not boxes:
            return f"no options listed to match '{hint}'"

        target = None
        h = hint.strip()
        if h.isdigit() and len(h) <= 2:  # short number => positional index
            idx = int(h)
            target = boxes[idx] if 0 <= idx < len(boxes) else None
            if target is None:
                return f"index {idx} out of range (have {len(boxes)})"
        else:  # match by last-4 / text
            digits = re.findall(r"\d{4}", h)
            needle = re.sub(r"\s+", " ", h).lower()
            for b in boxes:
                text = ((await b.inner_text()) or "").lower()
                if (digits and digits[0] in text) or (needle and needle in text):
                    target = b
                    break
            if target is None:
                return f"no option matched '{hint}' — left default"

        # Tick the option's radio, then confirm with "Use this ...".
        radio = await target.query_selector('input[type="radio"]')
        await (radio or target).click()
        await page.wait_for_timeout(800)
        for sel in (
            "#checkout-primary-continue-button-id",
            "#checkout-secondary-continue-button-id",
            "#checkout-primary-continue-button-id-announce",
        ):
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                break
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2500)
        return f"selected '{hint}'"
    except Exception as e:
        return f"selection error: {e}"


async def _read_checkout_summary(page: Page) -> dict:
    """Scrape amazon.ca's "Chewbacca" place-order screen: ship-to, payment, totals."""
    return await page.evaluate(
        """
        () => {
            const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim() || null;
            const t = (sel) => clean(document.querySelector(sel)?.textContent);

            const shipTo = t('#deliver-to-address-text');

            // Payment shows as "Paying with <network> <last4>" in a panel heading.
            let payment = null;
            for (const e of document.querySelectorAll('*')) {
                if (e.children.length <= 3 && /Paying with/i.test(e.textContent || '')) {
                    const m = (e.textContent || '').match(/Paying with[^{]*/i);
                    if (m) { payment = clean(m[0]); break; }
                }
            }

            // Order total + itemized breakdown.
            const orderTotal = (t('#subtotals-marketplace-tango-bottom') || '')
                .replace(/order total:?/i, '').trim() || null;
            const breakdown = t('#subtotals-marketplace-table');

            return {
                ship_to: shipTo,
                payment_method: payment,
                order_total: orderTotal,
                breakdown: breakdown,
            };
        }
        """
    )


@mcp.tool()
async def amazon_list_orders(max_orders: int = 10, domain: str = DEFAULT_DOMAIN) -> str:
    """List your recent Amazon orders.

    Returns each order's number, date, total, items, delivery status, and whether
    it currently shows a "Return or replace items" option. Requires being logged in.

    Args:
        max_orders: How many recent orders to return (default 10).
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        await page.goto(
            f"https://www.amazon.{domain}/gp/css/order-history",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})
        name = await _account_name(page)
        if not _is_logged_in(name):
            return json.dumps({"error": "Not logged in. Run amazon_login first."})
        await page.wait_for_timeout(1500)

        orders = await page.evaluate(
            """
            (maxOrders) => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const cards = [...document.querySelectorAll(
                    '.order-card, .js-order-card, [class*="order-card"]'
                )];
                const out = [];
                for (const c of cards.slice(0, maxOrders)) {
                    const full = clean(c.textContent);
                    const num = (c.querySelector('.yohtmlc-order-id')?.textContent || full)
                        .match(/(D?\\d{2,3}-\\d{7}-\\d{7})/);
                    // Header columns: order placed date + total are in the top bar.
                    const header = clean(
                        c.querySelector('.order-header, [class*="order-header"], .a-box-group .a-box:first-child')
                            ?.textContent
                    );
                    const date = (header.match(
                        /([0-9]{1,2}\\s+[A-Z][a-z]+\\s+[0-9]{4}|[A-Z][a-z]+\\s+[0-9]{1,2},?\\s+[0-9]{4})/
                    ) || [])[0] || null;
                    const total = (header.match(/(?:CDN\\$|\\$|US\\$)\\s?[0-9,]+\\.[0-9]{2}/) || [])[0] || null;
                    const titles = [...c.querySelectorAll(
                        '.yohtmlc-product-title, .a-link-normal[href*="/product/"], [class*="product-title"]'
                    )].map(e => clean(e.textContent)).filter(Boolean).slice(0, 5);
                    const status = clean(
                        c.querySelector('.delivery-box .a-text-bold, [class*="delivery"] .a-text-bold, .delivery-box')
                            ?.textContent
                    ).split(/(?=[A-Z][a-z])/)[0] || null;
                    const returnable = [...c.querySelectorAll('a')].some(
                        a => /return or replace|return item|replace item/i.test(a.textContent || '')
                    );
                    out.push({
                        order_number: num ? num[1] : null,
                        date, total,
                        items: titles,
                        status: status || null,
                        returnable,
                    });
                }
                return out;
            }
            """,
            max_orders,
        )
        return json.dumps({"count": len(orders), "orders": orders}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.close()


@mcp.tool()
async def amazon_view_returns(domain: str = DEFAULT_DOMAIN) -> str:
    """View the status of your Amazon returns.

    For each in-progress / past return: item, status, refund amount, the date you
    must return it by, and the drop-off / return location. Requires being logged in.

    NOTE: read-only and best-effort — the return-status DOM is only confirmed once
    you actually have an active return; selectors may need a live pass.

    Args:
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        # Started/processing returns live on the returns-filtered order history,
        # not the (often empty) /spr or Return Center pages.
        await page.goto(
            f"https://www.amazon.{domain}/gp/your-account/order-history?orderFilter=returns",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})
        name = await _account_name(page)
        if not _is_logged_in(name):
            return json.dumps({"error": "Not logged in. Run amazon_login first."})
        await page.wait_for_timeout(2500)

        returns = await page.evaluate(
            """
            () => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const cards = [...document.querySelectorAll(
                    '.order-card, .js-order-card, [class*="order-card"]'
                )];
                const out = [];
                for (const c of cards) {
                    const full = clean(c.textContent);
                    // Short status line (e.g. "Return started", "Refund issued").
                    const bolds = [...c.querySelectorAll('.a-text-bold')]
                        .map(e => clean(e.textContent))
                        .filter(Boolean);
                    const status = bolds
                        .filter(t => t.length < 40 &&
                            /return|refund|received|in transit|drop ?off|processed|completed/i.test(t))
                        .sort((a, b) => a.length - b.length)[0] || null;
                    // Only keep cards that are actually returns/refunds.
                    const isReturn = !!status || /return started|refund (?:issued|of)|return (?:received|requested)/i.test(full);
                    if (!isReturn) continue;

                    const num = (full.match(/(D?\\d{2,3}-\\d{7}-\\d{7})/) || [])[1] || null;
                    const title = clean(
                        c.querySelector('.yohtmlc-product-title, .a-link-normal[href*="/product/"]')?.textContent
                    ).slice(0, 80) || null;
                    const refund = (full.match(/refund of\\s+((?:CDN\\$|\\$|US\\$)\\s?[0-9,]+\\.[0-9]{2})/i) || [])[1] || null;
                    const canTrack = /view return\\/refund status|view return label/i.test(full);

                    out.push({
                        order_number: num,
                        item: title,
                        status: status || "Return in progress",
                        refund: refund,
                        refund_note: refund ? null : "amount shown after Amazon receives the item",
                        details_available: canTrack,
                    });
                }
                return out;
            }
            """
        )
        return json.dumps(
            {
                "count": len(returns),
                "returns": returns,
                "note": "No returns found."
                if not returns
                else "Refund amount / return-by date / drop-off location live behind each "
                "return's 'View Return/Refund Status' page — not shown in this summary.",
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.close()


@mcp.tool()
async def amazon_start_return(
    order_number: str,
    confirm: bool = False,
    reason: str | None = None,
    domain: str = DEFAULT_DOMAIN,
) -> str:
    """Start a return for an item in one of your orders.

    SAFETY: like checkout, this has a confirm gate. With confirm=False (default) it
    opens the return wizard for the order and reports what it finds — returnable
    items, the reason options, and any refund / return-by / drop-off details — WITHOUT
    submitting. Review, then call again with confirm=true to submit the return.

    NOTE: UNVERIFIED end-to-end. Amazon's return wizard (reason dropdown, refund
    method, drop-off choice) can only be reverse-engineered against a real delivered,
    returnable item — which requires a live shakedown. Treat the submit path as
    not-yet-proven until then.

    Args:
        order_number: The order to return from (e.g. "702-6813027-3844263").
        confirm: Must be true to actually submit the return. Default false = preview.
        reason: Return reason text to match against the dropdown (e.g. "no longer needed").
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        await page.goto(
            f"https://www.amazon.{domain}/gp/css/order-history",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        name = await _account_name(page)
        if not _is_logged_in(name):
            return json.dumps({"error": "Not logged in. Run amazon_login first."})
        await page.wait_for_timeout(1500)

        # Find the "Return or replace items" link on the matching order card.
        link = await page.evaluate(
            """
            (orderNum) => {
                const cards = [...document.querySelectorAll(
                    '.order-card, .js-order-card, [class*="order-card"]'
                )];
                for (const c of cards) {
                    if (!(c.textContent || '').includes(orderNum)) continue;
                    const a = [...c.querySelectorAll('a')].find(
                        x => /return or replace|return item|replace item/i.test(x.textContent || '')
                    );
                    return a ? a.href : 'NO_RETURN_LINK';
                }
                return 'ORDER_NOT_FOUND';
            }
            """,
            order_number,
        )
        if link == "ORDER_NOT_FOUND":
            return json.dumps(
                {"error": f"Order {order_number} not found in recent history."}
            )
        if link == "NO_RETURN_LINK":
            return json.dumps(
                {
                    "error": f"Order {order_number} has no return option right now "
                    "(not delivered yet, outside the return window, or non-returnable).",
                    "returnable": False,
                }
            )

        await page.goto(link, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)

        found = await page.evaluate(
            """
            () => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                // The return-reason control is a native <select> whose id ends in
                // "questionnaire-widget-native-dropdown" (NOT the site search dropdown).
                const reasonSel = document.querySelector(
                    'select[id*="questionnaire"], select.a-native-dropdown[id*="native-dropdown"]'
                );
                const reasons = reasonSel
                    ? [...reasonSel.querySelectorAll('option')]
                        .map(o => clean(o.textContent))
                        .filter(t => t && !/choose a response/i.test(t))
                    : [];
                // Item being returned: capture the descriptor lines (size/colour/sold by).
                const itemBlock = clean(
                    (document.querySelector(
                        '[class*="return-item"], [class*="ReturnItem"], [class*="item-info"], .a-fixed-left-grid'
                    ) || {}).textContent
                );
                const refund = (document.body.innerText.match(/(?:refund[^$]*?)((?:CDN\\$|\\$|US\\$)\\s?[0-9,]+\\.[0-9]{2})/i) || [])[1] || null;
                return {
                    item: itemBlock ? itemBlock.slice(0, 200) : null,
                    reason_options: reasons,
                    refund_estimate: refund,
                };
            }
            """
        )

        if not confirm:
            return json.dumps(
                {
                    "placed": False,
                    "order_number": order_number,
                    "return_wizard_url": page.url,
                    "returning_item": found.get("item"),
                    "reason_options": found.get("reason_options"),
                    "refund_estimate": found.get("refund_estimate"),
                    "next_step": "Preview only. The full multi-step submit (reason → method → "
                    "drop-off → confirm) is unverified; we should walk it live against this "
                    "real return before trusting confirm=true.",
                },
                indent=2,
            )

        return json.dumps(
            {
                "placed": False,
                "error": "Submit path is intentionally not wired blind. Run this preview "
                "live with the item in hand so we can reverse-engineer the reason/method/"
                "drop-off steps safely, like we did for checkout.",
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        await page.close()


@mcp.tool()
async def amazon_subscribe(
    asin: str | None = None,
    url: str | None = None,
    frequency: str | None = None,
    confirm: bool = False,
    domain: str = DEFAULT_DOMAIN,
) -> str:
    """Subscribe to a product via Amazon Subscribe & Save (recurring delivery).

    SAFETY — this sets up a RECURRING charge. With confirm=False (default) it only
    PREVIEWS: expands the Subscribe & Save option and returns the S&S price, the
    discount, and the delivery frequency WITHOUT subscribing. confirm=true is
    required to actually create the subscription.

    NOTE: the submit path is best-effort and NOT verified end-to-end (verifying
    would mean creating a real recurring subscription on the account). Treat
    confirm=true as live-untested until a real subscription is intentionally made.

    Args:
        asin: 10-char product ASIN. Either this or url.
        url: full product page URL. Either this or asin.
        frequency: desired delivery cadence to match, e.g. "1 month", "2 months".
            If omitted, Amazon's default cadence is used.
        confirm: must be true to actually subscribe. Default false = preview only.
        domain: Amazon domain — "ca" for Canada, "com" for US.
    """
    if not asin and not url:
        return json.dumps({"error": "Provide either an asin or a url."})
    if not url:
        url = f"https://www.amazon.{domain}/dp/{asin}"

    ctx = await get_logged_in_context()
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        captcha = await check_captcha(page)
        if captcha:
            return json.dumps({"error": captcha})
        name = await _account_name(page)
        if not _is_logged_in(name):
            return json.dumps({"error": "Not logged in. Run amazon_login first."})

        title_el = await page.query_selector("#productTitle")
        title = (await title_el.inner_text()).strip() if title_el else None

        # Expand the "Subscribe & Save" accordion so its controls render.
        await page.evaluate(
            """
            () => {
                const rows = [...document.querySelectorAll(
                    '.a-accordion-row, [id*="ccordionRow"], .a-box'
                )];
                for (const r of rows) {
                    if (/subscribe & save|subscribe and save/i.test(r.textContent || '')) {
                        (r.querySelector('a, input[type=radio], [role=button]') || r).click();
                        return;
                    }
                }
            }
            """
        )
        await page.wait_for_timeout(2000)

        sub_btn = await page.query_selector("#rcx-subscribe-submit-button")
        if not sub_btn:
            return json.dumps(
                {
                    "error": "This item isn't offering Subscribe & Save right now "
                    "(not S&S-eligible, or no active subscription offer).",
                    "subscribable": False,
                    "url": url,
                }
            )

        # Best-effort frequency selection (Amazon's default is used if this misses).
        freq_applied = "default"
        if frequency:
            try:
                fl = frequency.lower()
                sels = await page.query_selector_all("select")
                for sel in sels:
                    opts = await sel.query_selector_all("option")
                    for o in opts:
                        if fl in ((await o.inner_text()) or "").lower():
                            await sel.select_option(value=await o.get_attribute("value"))
                            freq_applied = frequency
                            break
                    if freq_applied != "default":
                        break
            except Exception:
                freq_applied = "default (couldn't set requested frequency)"

        preview = await page.evaluate(
            """
            () => {
                const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                // Discount % and cadence render in the text around the subscribe button.
                const btn = document.querySelector('#rcx-subscribe-submit-button');
                let scope = btn;
                for (let i = 0; i < 6 && scope; i++) scope = scope.parentElement;
                const txt = clean((scope || document).textContent);
                const discount = (txt.match(/save (?:up to )?\\d+%|\\d+% off/i) || [])[0] || null;
                const freq = (txt.match(/(?:deliver every|every)\\s*[\\w ]{0,18}(?:week|month)s?/i) || [])[0] || null;
                // Base price from the main buy-box (S&S = base minus the discount).
                const price = clean((document.querySelector(
                    '#corePriceDisplay_desktop_feature_div .a-offscreen, #corePrice_feature_div .a-offscreen, .a-price .a-offscreen'
                ) || {}).textContent) || null;
                return { base_price: price, discount, frequency: freq };
            }
            """
        )
        preview["title"] = title
        preview["frequency_requested"] = freq_applied

        if not confirm:
            preview["subscribed"] = False
            preview["next_step"] = (
                "Preview only — no subscription created. To actually subscribe "
                "(recurring charge), call amazon_subscribe again with confirm=true."
            )
            return json.dumps(preview, indent=2)

        await sub_btn.click()
        await page.wait_for_timeout(2500)
        ok = await page.evaluate(
            """
            () => /subscription|subscribed|you'll receive|first delivery|manage your subscriptions/i
                .test(document.body.innerText || '')
            """
        )
        return json.dumps(
            {
                "subscribed": bool(ok),
                "title": title,
                "frequency": preview.get("frequency"),
                "base_price": preview.get("base_price"),
                "message": "Subscription created."
                if ok
                else "Clicked subscribe but couldn't confirm — check Your Subscribe & Save Items.",
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e), "url": url})
    finally:
        await page.close()


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
