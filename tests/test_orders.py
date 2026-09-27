import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.server.fastmcp.exceptions import ToolError
from playwright.async_api import async_playwright

from amazon_mcp.orders import (
    OrderDetailsResult,
    OrderListResult,
    OrderSummary,
    PACE_LONG_MAX_S,
    PACE_LONG_MIN_S,
    PACE_SHORT_MAX_S,
    PACE_SHORT_MIN_S,
    PACE_WINDOW_S,
    SCRAPE_ORDERS_DOM_SCRIPT,
    begin_order_action,
    collect_orders,
    fetch_order_details,
    reset_order_pace,
)
from amazon_mcp.server import (
    amazon_get_order,
    amazon_list_orders,
    amazon_search_orders,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class DOMExtractionTests(unittest.IsolatedAsyncioTestCase):
    """Tests executing SCRAPE_ORDERS_DOM_SCRIPT directly in a real headless browser."""

    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.page.close()
        await self.browser.close()
        await self.pw.stop()

    async def test_extracts_standard_orders_with_complete_info(self):
        html = _read_fixture("orders_standard.html")
        await self.page.set_content(html)
        result = await self.page.evaluate(SCRAPE_ORDERS_DOM_SCRIPT)

        self.assertFalse(result["empty_results"])
        orders = result["orders"]
        self.assertEqual(len(orders), 2)

        # Standard physical shipment order
        o1 = orders[0]
        self.assertEqual(o1["order_number"], "114-7654321-1234567")
        self.assertEqual(o1["order_date"], "12 September 2026")
        self.assertEqual(o1["order_total"], "$45.99")
        self.assertIn("Delivered today by 8 PM", o1["status"])
        self.assertIn("near the front door", o1["status"])  # Preserves status detail & line breaks
        self.assertEqual(
            o1["items"],
            ["Logitech Wireless Mouse", "USB-C Fast Charging Cable 6ft"],
        )
        self.assertEqual(o1["seller"], "Sold by: Amazon.com Services LLC")
        self.assertEqual(o1["payment_method"], "Paying with Visa 1234")
        self.assertTrue(o1["returnable"])
        self.assertTrue(len(o1["links"]) >= 3)
        self.assertTrue(any(link["label"] == "View invoice" for link in o1["links"]))

        # Digital order card
        o2 = orders[1]
        self.assertEqual(o2["order_number"], "D01-9876543-0123456")
        self.assertEqual(o2["order_date"], "August 15, 2026")
        self.assertEqual(o2["order_total"], "$129.50")
        self.assertEqual(o2["status"], "Delivered Aug 18, 2026")
        self.assertEqual(
            o2["items"],
            ["Mechanical Gaming Keyboard RGB", "Ergonomic Memory Foam Wrist Rest"],
        )
        self.assertEqual(o2["payment_method"], "Paying with Mastercard 5678")
        self.assertEqual(o2["hsa_text"], "HSA/FSA eligible amount: $0.00")
        self.assertFalse(o2["returnable"])

        # Pagination and year filters. This fixture still uses the legacy parameter.
        self.assertEqual(result["next_url"], "https://www.amazon.ca/your-orders/orders?page=2")
        self.assertEqual(result["active_filter"], "year-2026")
        year_filters = result["year_filter_urls"]
        self.assertEqual([yf["year"] for yf in year_filters], [2026, 2025, 2024])
        self.assertEqual(
            [yf["url"] for yf in year_filters],
            [
                "https://www.amazon.ca/your-orders/orders?orderFilter=year-2026",
                "https://www.amazon.ca/your-orders/orders?orderFilter=year-2025",
                "https://www.amazon.ca/your-orders/orders?orderFilter=year-2024",
            ],
        )

    async def test_year_filter_urls_use_time_filter_param(self):
        html = """
        <html><body>
          <form action="/your-orders/orders" method="get">
            <select id="time-filter" name="timeFilter">
              <option value="months-3" selected>past 3 months</option>
              <option value="year-2024">2024</option>
            </select>
          </form>
          <a href="/your-orders/orders?timeFilter=year-2024">2024</a>
          <div class="order-card">
            <span class="yohtmlc-order-id">112-6271263-4293829</span>
            <span>ORDER PLACED</span>
            <span class="value">December 31, 2024</span>
          </div>
        </body></html>
        """
        await self.page.set_content(html)
        result = await self.page.evaluate(SCRAPE_ORDERS_DOM_SCRIPT)

        self.assertEqual(result["active_filter"], "months-3")
        self.assertEqual(
            result["year_filter_urls"],
            [{
                "year": 2024,
                "url": "https://www.amazon.ca/your-orders/orders?timeFilter=year-2024",
            }],
        )

    async def test_extracts_empty_results_flag(self):
        html = _read_fixture("orders_empty.html")
        await self.page.set_content(html)
        result = await self.page.evaluate(SCRAPE_ORDERS_DOM_SCRIPT)

        self.assertTrue(result["empty_results"])
        self.assertEqual(len(result["orders"]), 0)

    async def test_distinguishes_unrecognized_layout(self):
        html = _read_fixture("orders_unrecognized.html")
        await self.page.set_content(html)
        result = await self.page.evaluate(SCRAPE_ORDERS_DOM_SCRIPT)

        self.assertFalse(result["empty_results"])
        self.assertEqual(len(result["orders"]), 0)

    async def test_extracts_order_details_view(self):
        html = _read_fixture("order_details.html")
        await self.page.set_content(html)
        result = await self.page.evaluate(SCRAPE_ORDERS_DOM_SCRIPT)

        self.assertEqual(len(result["orders"]), 1)
        o = result["orders"][0]
        self.assertEqual(o["order_number"], "114-7654321-1234567")
        self.assertEqual(
            o["items"],
            ["Logitech Wireless Mouse", "USB-C Fast Charging Cable 6ft"],
        )
        self.assertEqual(o["status"], "Delivered September 13, 2026")


def _make_dummy_order(order_num: str, order_date: str = "Jan 1, 2026") -> dict:
    return {
        "order_number": order_num,
        "source_url": f"https://www.amazon.ca/your-orders/orders?orderId={order_num}",
        "card_text": f"Order #{order_num}\nDelivered\nTotal: $10.00",
        "links": [{"label": "Order Details", "url": f"https://www.amazon.ca/orders/{order_num}"}],
        "order_date": order_date,
        "order_total": "$10.00",
        "status": "Delivered",
        "items": [f"Item for {order_num}"],
        "ship_to": "Test User",
        "payment_method": "Visa 1234",
        "seller": "Amazon.ca",
        "hsa_text": None,
        "returnable": False,
    }


class FakePageForOrders:
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0
        self.url = "https://www.amazon.ca/gp/css/order-history"
        self.goto = AsyncMock(side_effect=self._goto)
        self.wait_for_function = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.close = AsyncMock()

    async def _goto(self, url, **_kwargs):
        self.url = url

    async def evaluate(self, _script):
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {"orders": [], "empty_results": True, "next_url": None, "year_filter_urls": []}


class CollectOrdersUnitTests(unittest.IsolatedAsyncioTestCase):
    """Tests pagination, deduplication, 5-page budget, and partial failures in collect_orders."""

    async def asyncSetUp(self):
        reset_order_pace()
        self._pace_off = patch.dict(os.environ, {"AMAZON_HUMAN_PAUSE": "0"})
        self._pace_off.start()

    async def asyncTearDown(self):
        self._pace_off.stop()
        reset_order_pace()

    async def test_reaches_max_orders_and_returns_continuation(self):
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order(f"111-000000{i}-0000000") for i in range(1, 6)],
                "empty_results": False,
                "next_url": "https://www.amazon.ca/orders?page=2",
                "year_filter_urls": [],
            },
            {
                "orders": [_make_dummy_order(f"111-000000{i}-0000000") for i in range(6, 11)],
                "empty_results": False,
                "next_url": "https://www.amazon.ca/orders?page=3",
                "year_filter_urls": [],
            },
        ])

        res: OrderListResult = await collect_orders(
            page=page,
            start_url="https://www.amazon.ca/gp/css/order-history",
            max_orders=8,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(res.count, 8)
        self.assertEqual(len(res.orders), 8)
        self.assertEqual(res.stop_reason, "max_orders_reached")
        self.assertIsNotNone(res.continuation)
        self.assertIn("page=3", res.continuation)

    async def test_stops_when_no_more_pages(self):
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order("111-1111111-1111111")],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [],
            }
        ])

        res = await collect_orders(
            page=page,
            start_url="https://www.amazon.ca/gp/css/order-history",
            max_orders=10,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(res.count, 1)
        self.assertEqual(res.stop_reason, "no_more_pages")
        self.assertIsNone(res.continuation)

    async def test_page_budget_exhausted_stops_at_five_pages(self):
        # 6 pages available, but budget is 5
        pages_data = []
        for p in range(1, 7):
            pages_data.append({
                "orders": [_make_dummy_order(f"111-{p}000000-0000000")],
                "empty_results": False,
                "next_url": f"https://www.amazon.ca/orders?page={p + 1}",
                "year_filter_urls": [],
            })

        page = FakePageForOrders(pages_data)
        res = await collect_orders(
            page=page,
            start_url="https://www.amazon.ca/gp/css/order-history",
            max_orders=50,
            max_pages=5,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(res.count, 5)
        self.assertEqual(res.stop_reason, "page_budget_exhausted")
        self.assertEqual(res.continuation, "https://www.amazon.ca/orders?page=6")

    async def test_deduplicates_orders_across_pages(self):
        page = FakePageForOrders([
            {
                "orders": [
                    _make_dummy_order("111-1111111-1111111"),
                    _make_dummy_order("111-2222222-2222222"),
                ],
                "empty_results": False,
                "next_url": "https://www.amazon.ca/orders?page=2",
                "year_filter_urls": [],
            },
            {
                "orders": [
                    _make_dummy_order("111-2222222-2222222"),  # Duplicate
                    _make_dummy_order("111-3333333-3333333"),
                ],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [],
            },
        ])

        res = await collect_orders(
            page=page,
            start_url="https://www.amazon.ca/gp/css/order-history",
            max_orders=10,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(res.count, 3)
        self.assertEqual(
            [o.order_number for o in res.orders],
            ["111-1111111-1111111", "111-2222222-2222222", "111-3333333-3333333"],
        )

    async def test_year_filter_navigation(self):
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order("111-2026000-0000000")],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [
                    {"year": 2026, "url": "https://www.amazon.ca/orders?orderFilter=year-2026"},
                    {"year": 2025, "url": "https://www.amazon.ca/orders?orderFilter=year-2025"},
                ],
            },
            {
                "orders": [_make_dummy_order("111-2025000-0000000")],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [],
            },
        ])

        res = await collect_orders(
            page=page,
            start_url="https://www.amazon.ca/gp/css/order-history",
            max_orders=5,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(res.count, 2)
        self.assertEqual(res.orders[0].order_number, "111-2026000-0000000")
        self.assertEqual(res.orders[1].order_number, "111-2025000-0000000")

    async def test_year_opens_advertised_time_filter_url(self):
        start = "https://www.amazon.com/gp/css/order-history"
        year_url = "https://www.amazon.com/your-orders/orders?timeFilter=year-2024"
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order("111-2026000-0000001", "September 21, 2026")],
                "empty_results": False,
                "next_url": (
                    "https://www.amazon.com/your-orders/orders"
                    "?orderFilter=year-2024&page=1"
                ),
                "year_filter_urls": [
                    {
                        "year": 2026,
                        "url": "https://www.amazon.com/your-orders/orders?timeFilter=year-2026",
                    },
                    {"year": 2024, "url": year_url},
                ],
                "active_filter": "months-3",
            },
            {
                "orders": [
                    _make_dummy_order("112-6271263-4293829", "December 31, 2024"),
                    _make_dummy_order("112-4462647-9789048", "December 30, 2024"),
                ],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [],
                "active_filter": "year-2024",
            },
        ])

        res = await collect_orders(
            page=page,
            start_url=start,
            max_orders=10,
            year=2024,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(
            [call.args[0] for call in page.goto.await_args_list],
            [start, year_url],
        )
        self.assertEqual(
            [order.order_number for order in res.orders],
            ["112-6271263-4293829", "112-4462647-9789048"],
        )
        self.assertEqual(res.stop_reason, "no_more_pages")

    async def test_year_opens_advertised_order_filter_url(self):
        start = "https://www.amazon.ca/gp/css/order-history"
        year_url = "https://www.amazon.ca/your-orders/orders?orderFilter=year-2024"
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order("111-2026000-0000001", "September 21, 2026")],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [{"year": 2024, "url": year_url}],
                "active_filter": "months-3",
            },
            {
                "orders": [_make_dummy_order("112-1000000-0000001", "March 2, 2024")],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [],
                "active_filter": "year-2024",
            },
        ])

        res = await collect_orders(
            page=page,
            start_url=start,
            max_orders=10,
            year=2024,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(page.goto.await_args_list[1].args[0], year_url)
        self.assertEqual(res.orders[0].order_number, "112-1000000-0000001")

    async def test_year_skips_recent_orders_that_happen_to_match(self):
        start = "https://www.amazon.com/gp/css/order-history"
        year_url = "https://www.amazon.com/your-orders/orders?timeFilter=year-2026"
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order("111-2026000-0000001", "September 21, 2026")],
                "empty_results": False,
                "next_url": "https://www.amazon.com/your-orders/orders?page=2",
                "year_filter_urls": [{"year": 2026, "url": year_url}],
                "active_filter": "months-3",
            },
            {
                "orders": [_make_dummy_order("111-2026000-0000099", "January 15, 2026")],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [],
                "active_filter": "year-2026",
            },
        ])

        res = await collect_orders(
            page=page,
            start_url=start,
            max_orders=10,
            year=2026,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(
            [order.order_number for order in res.orders],
            ["111-2026000-0000099"],
        )
        self.assertEqual(page.goto.await_args_list[1].args[0], year_url)

    async def test_year_does_not_paginate_when_filter_is_ignored(self):
        start = "https://www.amazon.com/gp/css/order-history"
        fallback = "https://www.amazon.com/your-orders/orders?timeFilter=year-2024"
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order("111-2026000-0000001", "September 25, 2026")],
                "empty_results": False,
                "next_url": "https://www.amazon.com/gp/css/order-history?page=2",
                "year_filter_urls": [],
                "active_filter": None,
            },
            {
                "orders": [_make_dummy_order("111-2026000-0000002", "September 21, 2026")],
                "empty_results": False,
                "next_url": (
                    "https://www.amazon.com/your-orders/orders"
                    "?orderFilter=year-2024&page=1"
                ),
                "year_filter_urls": [],
                # The control can echo the requested year while the list stays recent.
                "active_filter": "year-2024",
            },
        ])

        res = await collect_orders(
            page=page,
            start_url=start,
            max_orders=10,
            year=2024,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(
            [call.args[0] for call in page.goto.await_args_list],
            [start, fallback],
        )
        self.assertEqual(res.count, 0)
        self.assertEqual(res.stop_reason, "no_orders_found")
        self.assertIsNone(res.continuation)

    async def test_year_follows_next_link_that_keeps_the_year(self):
        start = "https://www.amazon.com/gp/css/order-history"
        year_url = "https://www.amazon.com/your-orders/orders?timeFilter=year-2024"
        next_url = "https://www.amazon.com/your-orders/orders?timeFilter=year-2024&page=2"
        page = FakePageForOrders([
            {
                "orders": [],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [{"year": 2024, "url": year_url}],
                "active_filter": "months-3",
            },
            {
                "orders": [_make_dummy_order("112-6271263-4293829", "December 31, 2024")],
                "empty_results": False,
                "next_url": next_url,
                "year_filter_urls": [],
                "active_filter": "year-2024",
            },
            {
                "orders": [_make_dummy_order("112-4462647-9789048", "December 30, 2024")],
                "empty_results": False,
                "next_url": "https://www.amazon.com/your-orders/orders?page=3",
                "year_filter_urls": [],
                "active_filter": "year-2024",
            },
        ])

        res = await collect_orders(
            page=page,
            start_url=start,
            max_orders=10,
            year=2024,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(
            [call.args[0] for call in page.goto.await_args_list],
            [start, year_url, next_url],
        )
        self.assertEqual(
            [order.order_number for order in res.orders],
            ["112-6271263-4293829", "112-4462647-9789048"],
        )
        self.assertEqual(res.stop_reason, "no_more_pages")
        self.assertIsNone(res.continuation)

    async def test_year_continuation_is_not_rewritten(self):
        continuation = "https://www.amazon.com/your-orders/orders?timeFilter=year-2024"
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order("112-6271263-4293829", "December 31, 2024")],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [],
                "active_filter": "year-2024",
            }
        ])

        res = await collect_orders(
            page=page,
            start_url="https://www.amazon.com/gp/css/order-history",
            max_orders=10,
            year=2024,
            continuation=continuation,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(page.goto.await_args_list[0].args[0], continuation)
        self.assertEqual(page.goto.await_count, 1)
        self.assertEqual(res.count, 1)

    async def test_partial_failure_preserves_collected_results(self):
        page = FakePageForOrders([
            {
                "orders": [_make_dummy_order("111-1111111-1111111")],
                "empty_results": False,
                "next_url": "https://www.amazon.ca/orders?page=2",
                "year_filter_urls": [],
            },
            RuntimeError("Network connection reset"),
        ])

        res = await collect_orders(
            page=page,
            start_url="https://www.amazon.ca/gp/css/order-history",
            max_orders=10,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(res.count, 1)
        self.assertEqual(res.orders[0].order_number, "111-1111111-1111111")
        self.assertEqual(res.stop_reason, "partial_failure")
        self.assertEqual(res.continuation, "https://www.amazon.ca/orders?page=2")

    async def test_empty_results_returns_no_orders_found(self):
        page = FakePageForOrders([
            {
                "orders": [],
                "empty_results": True,
                "next_url": None,
                "year_filter_urls": [],
            }
        ])

        res = await collect_orders(
            page=page,
            start_url="https://www.amazon.ca/gp/css/order-history",
            max_orders=10,
            check_captcha_fn=AsyncMock(return_value=None),
            account_name_fn=AsyncMock(return_value="Test User"),
            is_logged_in_fn=lambda name: True,
        )

        self.assertEqual(res.count, 0)
        self.assertEqual(res.stop_reason, "no_orders_found")

    async def test_unrecognized_layout_raises_tool_error(self):
        page = FakePageForOrders([
            {
                "orders": [],
                "empty_results": False,
                "next_url": None,
                "year_filter_urls": [],
            }
        ])

        with self.assertRaises(ToolError) as ctx:
            await collect_orders(
                page=page,
                start_url="https://www.amazon.ca/gp/css/order-history",
                max_orders=10,
                check_captcha_fn=AsyncMock(return_value=None),
                account_name_fn=AsyncMock(return_value="Test User"),
                is_logged_in_fn=lambda name: True,
            )

        self.assertIn("unrecognized", str(ctx.exception).lower())


class OrderToolsEndpointTests(unittest.IsolatedAsyncioTestCase):
    """Tests MCP tool endpoints for orders."""

    async def test_amazon_get_order_validates_order_number(self):
        with self.assertRaises(ToolError) as ctx:
            await amazon_get_order("bad-order-id")
        self.assertIn("Invalid Amazon order number", str(ctx.exception))

    async def test_amazon_search_orders_validates_query(self):
        with self.assertRaises(ToolError) as ctx:
            await amazon_search_orders("   ")
        self.assertIn("query must not be empty", str(ctx.exception).lower())

    async def test_amazon_list_orders_validates_max_orders(self):
        with self.assertRaises(ToolError) as ctx:
            await amazon_list_orders(max_orders=0)
        self.assertIn("positive integer", str(ctx.exception))

        with self.assertRaises(ToolError) as ctx:
            await amazon_list_orders(max_orders=-5)
        self.assertIn("positive integer", str(ctx.exception))


def _pace_page(*next_urls: str | None) -> FakePageForOrders:
    pages = []
    for index, next_url in enumerate(next_urls, start=1):
        pages.append(
            {
                "orders": [_make_dummy_order(f"111-000000{index}-0000000")],
                "empty_results": False,
                "next_url": next_url,
                "year_filter_urls": [],
            }
        )
    return FakePageForOrders(pages)


async def _collect(page: FakePageForOrders) -> OrderListResult:
    return await collect_orders(
        page=page,
        start_url="https://www.amazon.com/gp/css/order-history",
        max_orders=10,
        check_captcha_fn=AsyncMock(return_value=None),
        account_name_fn=AsyncMock(return_value="Test User"),
        is_logged_in_fn=lambda name: True,
    )


class OrderPaceTests(unittest.TestCase):
    """The sliding window decides the wait without touching a browser."""

    def setUp(self):
        reset_order_pace()
        self._pace_on = patch.dict(os.environ, {"AMAZON_HUMAN_PAUSE": "1"})
        self._pace_on.start()

    def tearDown(self):
        self._pace_on.stop()
        reset_order_pace()

    def test_first_five_actions_do_not_wait(self):
        paces = [begin_order_action(now=0) for _ in range(5)]
        self.assertTrue(all(pace.initial_wait_s == 0 for pace in paces))
        self.assertTrue(all(not pace.pause_between_pages for pace in paces))

    def test_later_actions_wait_and_every_fifteenth_waits_longer(self):
        def midpoint(low: float, high: float) -> float:
            return (low + high) / 2

        with patch("amazon_mcp.orders.random.uniform", side_effect=midpoint):
            paces = [begin_order_action(now=0) for _ in range(16)]
        short = midpoint(PACE_SHORT_MIN_S, PACE_SHORT_MAX_S)
        longer = midpoint(PACE_LONG_MIN_S, PACE_LONG_MAX_S)
        self.assertEqual(paces[5].initial_wait_s, short)
        self.assertTrue(paces[5].pause_between_pages)
        self.assertEqual(paces[14].initial_wait_s, longer)
        self.assertNotEqual(paces[14].initial_wait_s, short)
        self.assertTrue(paces[14].pause_between_pages)
        self.assertEqual(paces[15].initial_wait_s, short)
        self.assertTrue(paces[15].pause_between_pages)

    def test_drawn_pauses_stay_inside_their_ranges(self):
        paces = [begin_order_action(now=0) for _ in range(15)]
        for pace in paces[5:14]:
            self.assertGreaterEqual(pace.initial_wait_s, PACE_SHORT_MIN_S)
            self.assertLessEqual(pace.initial_wait_s, PACE_SHORT_MAX_S)
        self.assertGreaterEqual(paces[14].initial_wait_s, PACE_LONG_MIN_S)
        self.assertLessEqual(paces[14].initial_wait_s, PACE_LONG_MAX_S)
        reset_order_pace()
        again = [begin_order_action(now=1) for _ in range(15)]
        self.assertNotEqual(paces[14].initial_wait_s, again[14].initial_wait_s)

    def test_actions_older_than_the_window_expire(self):
        for _ in range(6):
            begin_order_action(now=0)
        pace = begin_order_action(now=PACE_WINDOW_S + 1)
        self.assertEqual(pace.initial_wait_s, 0)
        self.assertFalse(pace.pause_between_pages)

    def test_disabled_pacing_does_not_record_actions(self):
        with patch.dict(os.environ, {"AMAZON_HUMAN_PAUSE": "0"}):
            for i in range(20):
                pace = begin_order_action(now=float(i))
                self.assertEqual(pace.initial_wait_s, 0)
                self.assertFalse(pace.pause_between_pages)
        pace = begin_order_action(now=0)
        self.assertEqual(pace.initial_wait_s, 0)
        self.assertFalse(pace.pause_between_pages)


class OrderPaceCollectTests(unittest.IsolatedAsyncioTestCase):
    """collect_orders applies the window once per call and between its pages."""

    async def asyncSetUp(self):
        reset_order_pace()

    async def asyncTearDown(self):
        reset_order_pace()

    async def test_sixth_call_waits_before_each_page(self):
        with (
            patch.dict(os.environ, {"AMAZON_HUMAN_PAUSE": "1"}),
            patch("amazon_mcp.orders.random.uniform", return_value=9),
        ):
            for _ in range(5):
                begin_order_action()
            page = _pace_page(
                "https://www.amazon.com/your-orders/orders?page=2",
                None,
            )
            await _collect(page)
        self.assertEqual(
            [call.args[0] for call in page.wait_for_timeout.await_args_list],
            [9000, 9000],
        )

    async def test_fresh_window_does_not_pause(self):
        with patch.dict(os.environ, {"AMAZON_HUMAN_PAUSE": "1"}):
            page = _pace_page(
                "https://www.amazon.com/your-orders/orders?page=2",
                None,
            )
            await _collect(page)
        self.assertEqual(page.wait_for_timeout.await_count, 0)
        self.assertEqual(page.goto.await_count, 2)

    async def test_fifteenth_call_waits_two_minutes(self):
        with patch.dict(os.environ, {"AMAZON_HUMAN_PAUSE": "1"}):
            for _ in range(14):
                begin_order_action()
            page = _pace_page(None)
            await _collect(page)
        wait_ms = page.wait_for_timeout.await_args_list[0].args[0]
        self.assertEqual(page.wait_for_timeout.await_count, 1)
        self.assertGreaterEqual(wait_ms, int(PACE_LONG_MIN_S * 1000))
        self.assertLessEqual(wait_ms, int(PACE_LONG_MAX_S * 1000))


if __name__ == "__main__":
    unittest.main()
