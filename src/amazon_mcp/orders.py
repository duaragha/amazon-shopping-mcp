"""Amazon Order Extraction, Search, and Document Management Module.

Provides DOM extraction of Amazon orders across history, search, and direct detail views.
Returns native structured MCP models and reports failures using ToolError.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from playwright.async_api import Page

from .browser import get_browser_backend

# ---------------------------------------------------------------------------
# Constants & Patterns
# ---------------------------------------------------------------------------

_ORDER_NUMBER_RE = re.compile(r"^D?\d{2,3}-\d{7}-\d{7}$")
ORDER_NUMBER_RE = _ORDER_NUMBER_RE
_MONEY_TOKEN_RE = re.compile(
    r"(?:(?:US|CA|CDN)\s*\$|(?:USD|CAD)\s*|\$|£|€)\s*[0-9][0-9,]*(?:\.[0-9]{2})?",
    re.I,
)
_DATE_PATTERN_RE = re.compile(
    r"\b(?:\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})\b"
)
_PRODUCT_IMAGE_RE = re.compile(r"^https://m\.media-amazon\.com/images/I/", re.I)

# ---------------------------------------------------------------------------
# Structured MCP Models
# ---------------------------------------------------------------------------


class OrderLink(BaseModel):
    """A labeled link extracted from an Amazon order card or document."""

    label: str = Field(description="Visible link text or label")
    url: str = Field(description="Full target URL")


class OrderSummary(BaseModel):
    """Structured representation of an Amazon order preserving displayed text."""

    order_number: str = Field(description="Amazon order number, e.g. 112-1234567-1234567")
    source_url: str = Field(description="Page URL where the order was extracted")
    card_text: str = Field(description="Full visible text of the order card preserving line breaks")
    links: list[OrderLink] = Field(default_factory=list, description="Labeled links within the order card")
    order_date: str | None = Field(default=None, description="Displayed order date, if present")
    order_total: str | None = Field(default=None, description="Displayed order total amount, if present")
    status: str | None = Field(default=None, description="Displayed delivery or order status, if present")
    items: list[str] = Field(default_factory=list, description="List of all visible item/product titles")
    ship_to: str | None = Field(default=None, description="Recipient name or shipping destination, if displayed")
    payment_method: str | None = Field(default=None, description="Displayed payment method or network, if present")
    seller: str | None = Field(default=None, description="Displayed seller information, if present")
    hsa_text: str | None = Field(default=None, description="Displayed HSA/FSA eligibility text, if present")
    returnable: bool = Field(default=False, description="Whether return or replacement options are displayed")


class OrderListResult(BaseModel):
    """Structured result returned by amazon_list_orders and amazon_search_orders."""

    count: int = Field(description="Total number of orders returned")
    orders: list[OrderSummary] = Field(description="List of extracted order summaries")
    stop_reason: str = Field(
        description="Reason order collection stopped: 'max_orders_reached', 'no_more_pages', 'page_budget_exhausted', 'no_orders_found', or 'partial_failure'"
    )
    continuation: str | None = Field(
        default=None, description="Continuation URL/token to fetch subsequent order pages"
    )


class OrderDetailsResult(BaseModel):
    """Structured result returned by amazon_get_order."""

    order: OrderSummary = Field(description="Extracted order details")


class OrderDocumentResult(BaseModel):
    """Structured result returned by amazon_get_order_document."""

    order_number: str = Field(description="Amazon order number")
    heading: str | None = Field(default=None, description="Primary document heading")
    title: str | None = Field(default=None, description="Page title")
    document_type: str = Field(
        description="Document classification: 'order_summary', 'invoice', or 'tax_invoice'"
    )
    body_text: str = Field(description="Extracted visible text from the printable document")
    links: list[OrderLink] = Field(default_factory=list, description="Labeled links in the document")
    source_url: str = Field(description="Source URL of the printable document")


class DownloadInvoiceResult(BaseModel):
    """Structured result returned by amazon_download_invoice."""

    downloaded: bool = Field(description="Whether the document was successfully saved")
    order_number: str = Field(description="Amazon order number")
    heading: str | None = Field(default=None, description="Primary document heading")
    document_type: str = Field(
        description="Captured document type: 'order_summary', 'invoice', or 'tax_invoice'"
    )
    output_format: str = Field(description="Document format: 'html' or 'pdf'")
    path: str = Field(description="Local file system path where the document was saved")
    size_bytes: int = Field(description="File size in bytes")
    source_url: str = Field(description="Source URL of the captured document")


class ResetSessionResult(BaseModel):
    """Structured result returned by amazon_reset_session."""

    session_reset: bool = Field(description="Whether the browser session was reset")
    message: str = Field(description="Status message")
    user_data_dir: str = Field(description="Path to user data directory preserved on disk")


# ---------------------------------------------------------------------------
# Client-Side DOM Extraction Script
# ---------------------------------------------------------------------------

SCRAPE_ORDERS_DOM_SCRIPT = """
() => {
    const clean = (s) => (s || '').replace(/[\\t ]+/g, ' ').replace(/\\r\\n|\\r/g, '\\n').trim();

    // Extract visible text while preserving newlines for block elements
    function extractVisibleText(el) {
        if (!el) return '';
        const blocks = new Set([
            'DIV', 'P', 'BR', 'LI', 'TR', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
            'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'UL', 'OL', 'TABLE'
        ]);
        const ignoredTags = new Set([
            'SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'NAV'
        ]);

        function walk(node) {
            if (!node) return '';
            if (node.nodeType === Node.TEXT_NODE) {
                return node.textContent || '';
            }
            if (node.nodeType !== Node.ELEMENT_NODE) return '';

            const tag = node.tagName.toUpperCase();
            if (ignoredTags.has(tag)) return '';

            // Check element visibility
            try {
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden') return '';
            } catch (_) {}

            if (node.getAttribute('aria-hidden') === 'true') return '';
            if (node.getAttribute('data-action') === 'a-popover-close') return '';

            let out = '';
            for (const child of node.childNodes) {
                out += walk(child);
            }

            if (blocks.has(tag)) {
                out = '\\n' + out + '\\n';
            }
            return out;
        }

        const raw = walk(el);
        return raw.split('\\n').map(l => l.trim()).filter(Boolean).join('\\n');
    }

    const baseUrl = (window.location.href && window.location.href !== 'about:blank')
        ? window.location.href
        : 'https://www.amazon.ca/gp/css/order-history';
    const baseOrigin = (window.location.origin && window.location.origin !== 'null')
        ? window.location.origin
        : 'https://www.amazon.ca';

    // Extract labeled links
    function extractLinks(container) {
        const links = [];
        const seenUrls = new Set();
        for (const a of container.querySelectorAll('a[href]')) {
            const label = clean(a.innerText || a.textContent);
            const href = a.getAttribute('href');
            if (!label || !href || href.startsWith('#') || href.startsWith('javascript:')) continue;
            try {
                const fullUrl = new URL(href, baseUrl).href;
                if (!seenUrls.has(fullUrl)) {
                    seenUrls.add(fullUrl);
                    links.push({ label, url: fullUrl });
                }
            } catch (_) {}
        }
        return links;
    }

    const orderNumRegex = /(?:D?\\d{2,3}-\\d{7}-\\d{7})/;

    // Find distinct root cards
    let cardElements = [
        ...document.querySelectorAll(
            '.order-card, .js-order-card, [data-component-type="orderCard"], ' +
            '.order, .yo-card, .a-box-group.order, #order-summary, .order-info'
        )
    ];

    // Filter to only those elements containing an order number
    cardElements = cardElements.filter(el => orderNumRegex.test(el.textContent || ''));

    // Fallback: If no cards match verified selectors, find order number nodes and their closest card container
    if (cardElements.length === 0) {
        const candidates = new Set();
        for (const el of document.querySelectorAll('b, span, div, a')) {
            const text = (el.textContent || '').trim();
            if (orderNumRegex.test(text) && text.length <= 40) {
                // Ascend to a suitable card container
                let cur = el.parentElement;
                let found = null;
                while (cur && cur !== document.body) {
                    const cls = (cur.className || '');
                    if (typeof cls === 'string' && (cls.includes('order') || cls.includes('box-group') || cls.includes('card'))) {
                        found = cur;
                        break;
                    }
                    if (cur.parentElement && (cur.parentElement.id === 'ordersContainer' || cur.parentElement.id === 'yourOrders')) {
                        found = cur;
                        break;
                    }
                    cur = cur.parentElement;
                }
                candidates.add(found || el.parentElement || el);
            }
        }
        cardElements = [...candidates];
    }

    // Deduplicate nested containers by keeping only the outermost containers per order number
    const orders = [];
    const seenOrderNumbers = new Set();

    for (const card of cardElements) {
        const fullRaw = card.textContent || '';
        const match = fullRaw.match(orderNumRegex);
        if (!match) continue;
        const orderNum = match[0];
        if (seenOrderNumbers.has(orderNum)) continue;
        seenOrderNumbers.add(orderNum);

        const cardText = extractVisibleText(card);
        const links = extractLinks(card);

        // Header / meta details
        const headerEl = card.querySelector('.order-header, [class*="order-header"], .a-box-group .a-box:first-child, [class*="order-info"]');
        const headerText = clean(headerEl ? headerEl.textContent : cardText);

        const dateMatch = headerText.match(/([0-9]{1,2}\\s+[A-Za-z]+\\s+[0-9]{4}|[A-Za-z]+\\s+[0-9]{1,2},?\\s+[0-9]{4})/);
        const orderDate = dateMatch ? dateMatch[0] : null;

        const totalMatch = headerText.match(/(?:(?:US|CA|CDN)\\s*\\$|(?:USD|CAD)\\s*|\\$|£|€)\\s?[0-9,]+\\.[0-9]{2}/i);
        const orderTotal = totalMatch ? totalMatch[0] : null;

        // Delivery / Order status without split/omission
        const statusEl = card.querySelector('.delivery-box .a-text-bold, [class*="delivery"] .a-text-bold, [class*="delivery-box"], .shipment-status, [class*="shipment-status"]');
        const status = statusEl ? clean(statusEl.textContent) : null;

        // All visible product titles (no arbitrary 5-item cutoff)
        const itemEls = [
            ...card.querySelectorAll(
                '.yohtmlc-product-title, .a-link-normal[href*="/product/"], .a-link-normal[href*="/dp/"], [class*="product-title"]'
            )
        ];
        const items = [...new Set(itemEls.map(e => clean(e.textContent)).filter(Boolean))];

        // Recipient / ship to
        const recipientEl = card.querySelector('.recipient, .yohtmlc-recipient, [class*="recipient"]');
        const shipTo = recipientEl ? clean(recipientEl.textContent) : null;

        // Payment info if displayed on card
        let payment = null;
        for (const line of cardText.split('\\n')) {
            if (/paying with|payment method/i.test(line)) {
                payment = clean(line);
                break;
            }
        }

        // Seller info if displayed
        let seller = null;
        for (const line of cardText.split('\\n')) {
            if (/sold by:/i.test(line)) {
                seller = clean(line);
                break;
            }
        }

        // HSA / FSA text if present on card
        let hsaText = null;
        for (const line of cardText.split('\\n')) {
            if (/\\b(?:hsa|fsa)\\b/i.test(line)) {
                hsaText = clean(line);
                break;
            }
        }

        // Return or replacement link existence
        const returnable = links.some(l => /return or replace|return item|replace item/i.test(l.label));

        orders.push({
            order_number: orderNum,
            source_url: window.location.href,
            card_text: cardText,
            links: links,
            order_date: orderDate,
            order_total: orderTotal,
            status: status,
            items: items,
            ship_to: shipTo,
            payment_method: payment,
            seller: seller,
            hsa_text: hsaText,
            returnable: returnable,
        });
    }

    // Detect empty results indicator
    const emptyEl = document.querySelector(
        '#ordersContainer .no-orders, .no-orders-message, .empty-orders, [data-testid="no-orders"], #yourOrders .no-orders'
    );
    const bodyText = (document.body?.innerText || '');
    const hasEmptyText = /you have not placed any orders|0 orders placed|we didn't find any orders/i.test(bodyText);
    const isEmptyResults = Boolean(emptyEl || (orders.length === 0 && hasEmptyText));

    // Next page link
    const nextLink = document.querySelector(
        'ul.a-pagination li.a-last:not(.a-disabled) a, .a-pagination a[aria-label*="next" i], a[rel="next"]'
    );
    const nextHref = nextLink ? nextLink.getAttribute('href') : null;
    let nextUrl = null;
    if (nextHref) {
        try {
            nextUrl = new URL(nextHref, baseUrl).href;
        } catch (_) {
            nextUrl = nextHref;
        }
    }

    // Year filters. The select's name is the parameter Amazon applies:
    // timeFilter on current amazon.com, orderFilter on older markup.
    const yearFilters = new Map();
    const timeFilter = document.querySelector('select#time-filter, select[name="timeFilter"], select[name="orderFilter"]');
    const filterParamName = timeFilter?.getAttribute('name') || 'orderFilter';
    const filterFormAction = timeFilter?.closest('form')?.getAttribute('action');

    const filterToken = (value) => {
        if (!value) return null;
        try {
            const u = new URL(value, baseUrl);
            const fromQuery = u.searchParams.get(filterParamName)
                || u.searchParams.get('timeFilter')
                || u.searchParams.get('orderFilter');
            if (fromQuery) return fromQuery;
        } catch (_) {}
        if (/^(?:year-\\d{4}|months-\\d+)$/.test(value)) return value;
        const yearMatch = String(value).match(/year-\\d{4}/);
        return yearMatch ? yearMatch[0] : null;
    };

    const addYearFilter = (value) => {
        const filter = filterToken(value);
        const match = filter?.match(/^year-(\\d{4})$/);
        if (!match) return;
        try {
            const u = new URL(filterFormAction || '/your-orders/orders', baseOrigin);
            u.search = '';
            u.searchParams.set(filterParamName, filter);
            yearFilters.set(Number(match[1]), u.href);
        } catch (_) {}
    };

    for (const opt of document.querySelectorAll('select#time-filter option, select[name="timeFilter"] option, select[name="orderFilter"] option')) {
        addYearFilter(opt.value);
    }
    for (const a of document.querySelectorAll('a[href*="orderFilter=year-"], a[href*="timeFilter=year-"]')) {
        addYearFilter(a.getAttribute('href'));
    }

    return {
        orders: orders,
        empty_results: isEmptyResults,
        next_url: nextUrl,
        active_filter: timeFilter ? filterToken(timeFilter.value) : null,
        year_filter_urls: [...yearFilters.entries()]
            .sort(([a], [b]) => b - a)
            .map(([year, url]) => ({ year, url })),
    };
}
"""

SCRAPE_DOCUMENT_DOM_SCRIPT = """
(orderNumber) => {
    const clean = (s) => (s || '').replace(/[\\t ]+/g, ' ').replace(/\\r\\n|\\r/g, '\\n').trim();

    function extractVisibleText(el) {
        if (!el) return '';
        const blocks = new Set([
            'DIV', 'P', 'BR', 'LI', 'TR', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
            'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'UL', 'OL', 'TABLE'
        ]);
        const ignoredTags = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG']);

        function walk(node) {
            if (!node) return '';
            if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
            if (node.nodeType !== Node.ELEMENT_NODE) return '';

            const tag = node.tagName.toUpperCase();
            if (ignoredTags.has(tag)) return '';

            try {
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden') return '';
            } catch (_) {}

            let out = '';
            for (const child of node.childNodes) out += walk(child);
            if (blocks.has(tag)) out = '\\n' + out + '\\n';
            return out;
        }

        const raw = walk(el);
        return raw.split('\\n').map(l => l.trim()).filter(Boolean).join('\\n');
    }

    const baseUrl = (window.location.href && window.location.href !== 'about:blank')
        ? window.location.href
        : 'https://www.amazon.ca/gp/css/order-history';

    function extractLinks(container) {
        const links = [];
        const seenUrls = new Set();
        for (const a of container.querySelectorAll('a[href]')) {
            const label = clean(a.innerText || a.textContent);
            const href = a.getAttribute('href');
            if (!label || !href || href.startsWith('#') || href.startsWith('javascript:')) continue;
            try {
                const fullUrl = new URL(href, baseUrl).href;
                if (!seenUrls.has(fullUrl)) {
                    seenUrls.add(fullUrl);
                    links.push({ label, url: fullUrl });
                }
            } catch (_) {}
        }
        return links;
    }

    const bodyText = extractVisibleText(document.body);
    const headingEl = document.querySelector('h1, h2, [role="heading"]');
    const heading = headingEl ? clean(headingEl.textContent) : null;
    const title = document.title ? clean(document.title) : null;

    const lower = `${heading} ${title} ${bodyText.slice(0, 500)}`.toLowerCase();
    let documentType = 'order_summary';
    if (/tax invoice|vat invoice/.test(lower)) {
        documentType = 'tax_invoice';
    } else if (/\\binvoice\\b/.test(lower) && !/view invoice/.test(lower)) {
        documentType = 'invoice';
    }

    const links = extractLinks(document.body);

    return {
        heading: heading,
        title: title,
        document_type: documentType,
        body_text: bodyText,
        links: links,
        contains_order_number: orderNumber ? bodyText.includes(orderNumber) : true,
        unavailable: /request invoice|invoice (?:is )?not available/i.test(bodyText),
        source_url: window.location.href,
    };
}
"""

# ---------------------------------------------------------------------------
# Readiness Check
# ---------------------------------------------------------------------------


async def wait_for_orders_ready(page: Page, timeout: int = 15000) -> None:
    """Wait for page content to settle instead of using arbitrary sleep."""
    await page.wait_for_function(
        """
        () => {
            if (document.querySelector('form[action*="validateCaptcha"], input#captchacharacters')) return true;
            if (document.querySelector('form[name="signIn"], input#ap_email, input#ap_password')) return true;
            if (document.querySelector('.order-card, .js-order-card, [data-component-type="orderCard"], .order, #ordersContainer .no-orders, .no-orders-message, [data-testid="no-orders"]')) return true;
            const text = document.body ? document.body.innerText : '';
            if (/\\b(D?\\d{2,3}-\\d{7}-\\d{7})\\b/.test(text)) return true;
            if (/You have not placed any orders|0 orders placed|We didn't find any orders/i.test(text)) return true;
            return false;
        }
        """,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Order Collection Engine (List & Search)
# ---------------------------------------------------------------------------

_ORDER_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_YEAR_FILTER_PARAMS = ("timeFilter", "orderFilter")


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return "https://www.amazon.com"


def _time_filter_year_url(start_url: str, year: int) -> str:
    """Year URL used when the history page does not advertise one.

    amazon.com ignores ``orderFilter`` on ``/gp/css/order-history``.
    ``/your-orders/orders?timeFilter=year-YYYY`` returns that year.
    """
    return f"{_origin_of(start_url)}/your-orders/orders?timeFilter=year-{year}"


def _advertised_year_url(year_filter_urls: list[dict[str, Any]] | None, year: int) -> str | None:
    """Return the year URL built from the live time-filter control, if present."""
    for entry in year_filter_urls or []:
        url = entry.get("url")
        try:
            entry_year = int(entry.get("year"))
        except (TypeError, ValueError):
            continue
        if entry_year == year and url:
            return str(url)
    return None


def _page_matches_year(data: dict[str, Any], year: int) -> bool:
    """Whether this page is actually limited to ``year``.

    A selected filter other than ``year-YYYY`` means Amazon did not apply the
    request. Dated orders outside ``year`` mean the same thing when the control
    merely echoes a query parameter it ignored.
    """
    active = data.get("active_filter")
    if isinstance(active, str) and active and active != f"year-{year}":
        return False
    for raw in data.get("orders") or []:
        match = _ORDER_YEAR_RE.search(raw.get("order_date") or "")
        if match and int(match.group(1)) != year:
            return False
    return True


# Amazon treats a fast walk of order history as automated. A few order reads
# in one sitting stay immediate. After more than a handful inside 20 minutes,
# later reads pause, and a long dump occasionally waits a couple of minutes.
# Each pause is drawn from a range so the gap is not the same length every
# time. The record lives only in this process. AMAZON_HUMAN_PAUSE=0 turns it off.
#
# One order-tool call is one action. The first five in the window do not wait.
# Later calls wait 8–20s. Every 15th call waits about 1.5–2.5 minutes instead.
PACE_WINDOW_S = 20 * 60
PACE_FREE_ACTIONS = 5
PACE_LONG_EVERY = 15
PACE_LONG_MIN_S = 90
PACE_LONG_MAX_S = 150
PACE_SHORT_MIN_S = 8
PACE_SHORT_MAX_S = 20

_order_action_times: deque[float] = deque()


@dataclass(frozen=True)
class OrderPace:
    """How long the current order-tool call should pause before loading pages."""

    initial_wait_s: float
    pause_between_pages: bool


def pacing_enabled() -> bool:
    """Return whether order-read pacing is on. ``AMAZON_HUMAN_PAUSE=0`` disables it."""
    return os.environ.get("AMAZON_HUMAN_PAUSE", "1") != "0"


def reset_order_pace() -> None:
    """Drop recorded order reads. Tests use this to isolate the pacing window."""
    _order_action_times.clear()


def begin_order_action(now: float | None = None) -> OrderPace:
    """Record one order read and choose a human-scale pause, if this burst needs one.

    A short session stays fast. Sustained order scraping is what Amazon flags,
    so only calls past the free tier wait, and the wait length varies.
    Pass ``now`` from tests. Production uses a monotonic clock. Actions older
    than 20 minutes drop out of the window before this call is counted.
    """
    if not pacing_enabled():
        return OrderPace(0, False)
    if now is None:
        now = time.monotonic()
    cutoff = now - PACE_WINDOW_S
    while _order_action_times and _order_action_times[0] <= cutoff:
        _order_action_times.popleft()
    _order_action_times.append(now)
    count = len(_order_action_times)
    if count <= PACE_FREE_ACTIONS:
        return OrderPace(0, False)
    if count % PACE_LONG_EVERY == 0:
        return OrderPace(random.uniform(PACE_LONG_MIN_S, PACE_LONG_MAX_S), True)
    return OrderPace(random.uniform(PACE_SHORT_MIN_S, PACE_SHORT_MAX_S), True)


async def wait_order_pace(page: Page, seconds: float) -> None:
    """Pause before an order navigation when the pacing window says to."""
    if seconds <= 0:
        return
    await page.wait_for_timeout(int(seconds * 1000))


def _url_keeps_year(url: str, year: int) -> bool:
    """Whether a pagination link still requests this calendar year."""
    token = f"year-{year}"
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return False
    return any(token in query.get(key, []) for key in _YEAR_FILTER_PARAMS)


async def _read_orders_page(
    page: Page,
    url: str,
    check_captcha_fn: Any,
    account_name_fn: Any,
    is_logged_in_fn: Any,
) -> dict[str, Any]:
    """Open one orders page and return the DOM scrape payload."""
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    captcha = await check_captcha_fn(page)
    if captcha:
        raise ToolError(captcha)

    name = await account_name_fn(page)
    if not is_logged_in_fn(name):
        raise ToolError("Not logged in. Run amazon_login first.")

    await wait_for_orders_ready(page)
    data = await page.evaluate(SCRAPE_ORDERS_DOM_SCRIPT)
    if not isinstance(data, dict):
        raise ToolError(
            "Unrecognized order history page layout. Amazon may have updated its markup."
        )
    return data


async def collect_orders(
    page: Page,
    start_url: str,
    max_orders: int,
    check_captcha_fn: Any,
    account_name_fn: Any,
    is_logged_in_fn: Any,
    year: int | None = None,
    continuation: str | None = None,
    max_pages: int = 5,
) -> OrderListResult:
    """Collect orders across paginated pages with a 5-page budget.

    A requested year opens the URL advertised by the order-history select
    (``timeFilter`` or ``orderFilter`` on ``/your-orders/orders``). The
    unfiltered history page used to read that control is not counted against
    the page budget, and its orders are not returned. Pages whose orders fall
    outside the year are not paginated.

    Preserves collected orders on partial navigation failures and returns clear stop reasons.
    """
    orders: list[OrderSummary] = []
    seen_ids: set[str] = set()
    visited_urls: set[str] = set()
    pending_year_urls: list[tuple[int, str]] = []
    queued_year_urls: set[str] = set()

    pace = begin_order_action()
    first_fetch = True

    async def read_paced(url: str) -> dict[str, Any]:
        nonlocal first_fetch
        if first_fetch:
            await wait_order_pace(page, pace.initial_wait_s)
            first_fetch = False
        elif pace.pause_between_pages:
            await wait_order_pace(
                page, random.uniform(PACE_SHORT_MIN_S, PACE_SHORT_MAX_S)
            )
        return await _read_orders_page(
            page,
            url,
            check_captcha_fn,
            account_name_fn,
            is_logged_in_fn,
        )

    initial_url = continuation or start_url
    if year is not None and not continuation:
        try:
            discovery = await read_paced(start_url)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Failed to navigate and extract orders: {exc}") from exc
        visited_urls.add(start_url)
        initial_url = (
            _advertised_year_url(discovery.get("year_filter_urls"), year)
            or _time_filter_year_url(start_url, year)
        )

    current_url = initial_url
    stop_reason = "no_more_pages"
    next_continuation_token: str | None = None
    pages_visited = 0
    accepted_pages = 0

    while pages_visited < max_pages and len(orders) < max_orders:
        pages_visited += 1
        visited_urls.add(current_url)

        try:
            data = await read_paced(current_url)
        except ToolError:
            raise
        except Exception as exc:
            if orders:
                return OrderListResult(
                    count=len(orders),
                    orders=orders,
                    stop_reason="partial_failure",
                    continuation=current_url,
                )
            raise ToolError(f"Failed to navigate and extract orders: {exc}") from exc

        # Collect year filters
        for yf in data.get("year_filter_urls", []):
            url = yf["url"]
            if url not in visited_urls and url not in queued_year_urls:
                queued_year_urls.add(url)
                pending_year_urls.append((yf["year"], url))
        pending_year_urls.sort(reverse=True)

        if year is not None and not _page_matches_year(data, year):
            # This page ignored the year. Do not keep its orders or follow a
            # pager that only copied the ignored parameter.
            alt = _advertised_year_url(data.get("year_filter_urls"), year)
            if alt and alt not in visited_urls:
                current_url = alt
                continue
            return OrderListResult(
                count=len(orders),
                orders=orders,
                stop_reason="no_more_pages" if orders else "no_orders_found",
                continuation=None,
            )

        extracted_raw = data.get("orders", [])
        is_empty = data.get("empty_results", False)
        accepted_pages += 1

        # Distinguish recognized empty results from unrecognized page layout
        if accepted_pages == 1 and not extracted_raw:
            if is_empty:
                return OrderListResult(
                    count=0,
                    orders=[],
                    stop_reason="no_orders_found",
                    continuation=None,
                )
            raise ToolError(
                "Unrecognized order history page layout. Amazon may have updated its markup."
            )

        for raw_item in extracted_raw:
            order_summary = OrderSummary(**raw_item)
            if order_summary.order_number not in seen_ids:
                seen_ids.add(order_summary.order_number)
                orders.append(order_summary)
                if len(orders) == max_orders:
                    stop_reason = "max_orders_reached"
                    break

        next_url = data.get("next_url")
        if year is not None and next_url and not _url_keeps_year(next_url, year):
            next_url = None
        next_continuation_token = next_url

        if len(orders) >= max_orders:
            stop_reason = "max_orders_reached"
            break

        if pages_visited >= max_pages:
            stop_reason = "page_budget_exhausted"
            break

        # Select next destination URL
        destination = None
        if next_url and next_url not in visited_urls:
            destination = next_url
        elif year is None and pending_year_urls:
            while pending_year_urls:
                _, candidate = pending_year_urls.pop(0)
                if candidate not in visited_urls:
                    destination = candidate
                    break

        if not destination:
            stop_reason = "no_more_pages"
            next_continuation_token = None
            break

        current_url = destination

    return OrderListResult(
        count=len(orders),
        orders=orders,
        stop_reason=stop_reason,
        continuation=next_continuation_token,
    )


# ---------------------------------------------------------------------------
# Direct Order Detail Extractor
# ---------------------------------------------------------------------------


async def fetch_order_details(
    page: Page,
    order_number: str,
    domain: str,
    check_captcha_fn: Any,
    account_name_fn: Any,
    is_logged_in_fn: Any,
) -> OrderDetailsResult:
    """Fetch details for a single Amazon order by order number."""
    order_number = order_number.strip()
    if not _ORDER_NUMBER_RE.fullmatch(order_number):
        raise ToolError(
            f"Invalid Amazon order number '{order_number}'. Expected format like '112-1234567-1234567'."
        )

    url = (
        f"https://www.amazon.{domain}/gp/your-account/order-details"
        f"?orderID={quote_plus(order_number)}"
    )

    pace = begin_order_action()
    await wait_order_pace(page, pace.initial_wait_s)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    captcha = await check_captcha_fn(page)
    if captcha:
        raise ToolError(captcha)

    name = await account_name_fn(page)
    if not is_logged_in_fn(name):
        raise ToolError("Not logged in. Run amazon_login first.")

    await wait_for_orders_ready(page)
    data = await page.evaluate(SCRAPE_ORDERS_DOM_SCRIPT)

    orders = data.get("orders", [])
    for order in orders:
        if order["order_number"] == order_number:
            return OrderDetailsResult(order=OrderSummary(**order))

    # If the standard cards list didn't match, extract page body directly
    if orders:
        return OrderDetailsResult(order=OrderSummary(**orders[0]))

    raise ToolError(
        f"Order '{order_number}' was not found in account history or the details page could not be parsed."
    )


# ---------------------------------------------------------------------------
# Order Document & Invoice Operations
# ---------------------------------------------------------------------------


async def fetch_order_document(
    page: Page,
    order_number: str,
    domain: str,
    check_captcha_fn: Any,
) -> OrderDocumentResult:
    """Inspect and extract visible content from an order's printable summary or invoice."""
    order_number = order_number.strip()
    if not _ORDER_NUMBER_RE.fullmatch(order_number):
        raise ToolError(
            f"Invalid Amazon order number '{order_number}'. Expected format like '112-1234567-1234567'."
        )

    url = (
        f"https://www.amazon.{domain}/gp/css/summary/print.html"
        f"?orderID={quote_plus(order_number)}"
    )

    pace = begin_order_action()
    await wait_order_pace(page, pace.initial_wait_s)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    captcha = await check_captcha_fn(page)
    if captcha:
        raise ToolError(captcha)

    # Check for authentication redirect
    sign_in = await page.query_selector(
        'form[name="signIn"], input#ap_email, input#ap_password'
    )
    if sign_in or re.search(r"/(?:ap/)?signin", page.url, re.I):
        raise ToolError("Not logged in. Run amazon_login first.")

    details = await page.evaluate(SCRAPE_DOCUMENT_DOM_SCRIPT, order_number)

    if details.get("unavailable"):
        raise ToolError("Amazon does not currently provide a printable invoice for this order.")
    if not details.get("contains_order_number"):
        raise ToolError(
            f"Amazon did not return a printable document for order '{order_number}'. "
            "Check that the order number belongs to this account and has shipped."
        )

    return OrderDocumentResult(
        order_number=order_number,
        heading=details.get("heading"),
        title=details.get("title"),
        document_type=details.get("document_type", "order_summary"),
        body_text=details.get("body_text", ""),
        links=[OrderLink(**l) for l in details.get("links", [])],
        source_url=details.get("source_url", page.url),
    )


async def download_order_invoice(
    page: Page,
    order_number: str,
    output_format: str,
    domain: str,
    invoice_dir: str,
    check_captcha_fn: Any,
) -> DownloadInvoiceResult:
    """Download the printable order summary or invoice as HTML (default) or PDF."""
    order_number = order_number.strip()
    if not _ORDER_NUMBER_RE.fullmatch(order_number):
        raise ToolError(
            f"Invalid Amazon order number '{order_number}'. Expected format like '112-1234567-1234567'."
        )

    output_format = output_format.strip().lower()
    if output_format not in {"html", "pdf"}:
        raise ToolError("output_format must be 'html' (default) or 'pdf'.")

    if output_format == "pdf":
        if get_browser_backend() == "camoufox":
            # TODO: Revisit native Camoufox PDF export when its Juggler/Playwright
            # integration supports printing. Track:
            # https://github.com/daijro/camoufox/issues/593
            # This issue discusses future PDF support; verify page.pdf() works before
            # removing the backend guard.
            raise ToolError(
                "PDF export is currently only supported with the Chromium/Playwright backend. "
                "Use output_format='html' for Camoufox."
            )

    # Allow images for document summary
    await page.route(_PRODUCT_IMAGE_RE, lambda route: route.continue_())

    doc_info = await fetch_order_document(page, order_number, domain, check_captcha_fn)

    destination_dir = Path(invoice_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        destination_dir.chmod(0o700)
    except Exception:
        pass

    filename = f"amazon-{doc_info.document_type}-{order_number}.{output_format}"
    destination = destination_dir / filename

    if output_format == "pdf":
        try:
            await page.wait_for_function(
                """
                () => [...document.querySelectorAll('img[alt]:not([alt=""])')]
                    .every(img => img.complete && img.naturalWidth > 0)
                """,
                timeout=5000,
            )
        except Exception:
            pass

        await page.add_style_tag(content="#navFooter { display: none !important; }")
        await page.pdf(
            path=str(destination),
            format="Letter",
            print_background=True,
            prefer_css_page_size=True,
            margin={
                "top": "0.4in",
                "right": "0.4in",
                "bottom": "0.4in",
                "left": "0.4in",
            },
        )
    else:
        content = await page.content()
        destination.write_text(content, encoding="utf-8")

    try:
        destination.chmod(0o600)
    except Exception:
        pass

    return DownloadInvoiceResult(
        downloaded=True,
        order_number=order_number,
        heading=doc_info.heading,
        document_type=doc_info.document_type,
        output_format=output_format,
        path=str(destination),
        size_bytes=destination.stat().st_size,
        source_url=doc_info.source_url,
    )
