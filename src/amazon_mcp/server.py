"""Amazon Shopping MCP Server — search, compare, and review Amazon products."""

import asyncio
import json
import os
import re
from urllib.parse import quote_plus

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, Browser, Page

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
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-CA",
    )
    if block_media:
        await context.route(
            re.compile(
                r"\.(png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|mp4|webm)(\?|$)", re.I
            ),
            lambda route: route.abort(),
        )
    page = await context.new_page()
    return page


async def check_captcha(page: Page) -> str | None:
    title = await page.title()
    if any(w in title.lower() for w in ("robot", "captcha", "sorry", "bot")):
        return (
            "Amazon is showing a CAPTCHA / bot-check page. "
            "Try again in a moment."
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


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
