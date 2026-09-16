import unittest
from unittest.mock import AsyncMock, patch

from amazon_mcp.server import _collect_order_history


def _order(number: str) -> dict:
    return {
        "order_number": number,
        "date": None,
        "total": None,
        "items": [],
        "status": None,
        "returnable": False,
    }


class FakePage:
    def __init__(self, pages):
        self.url = "https://www.amazon.com/gp/css/order-history"
        self._pages = iter(pages)
        self.goto = AsyncMock(side_effect=self._goto)
        self.wait_for_timeout = AsyncMock()

    async def _goto(self, url, **_kwargs):
        self.url = url

    async def evaluate(self, _script):
        return next(self._pages)


class CollectOrderHistoryTests(unittest.IsolatedAsyncioTestCase):
    @patch("amazon_mcp.server.check_captcha", new_callable=AsyncMock, return_value=None)
    async def test_follows_pagination_until_requested_count(self, _captcha):
        page = FakePage(
            [
                {
                    "orders": [_order(str(i)) for i in range(1, 11)],
                    "next_url": "https://www.amazon.com/orders?page=2",
                },
                {
                    "orders": [_order(str(i)) for i in range(11, 21)],
                    "next_url": "https://www.amazon.com/orders?page=3",
                },
            ]
        )

        orders = await _collect_order_history(page, 15)

        self.assertEqual(15, len(orders))
        self.assertEqual("15", orders[-1]["order_number"])
        page.goto.assert_awaited_once()

    @patch("amazon_mcp.server.check_captcha", new_callable=AsyncMock, return_value=None)
    async def test_stops_when_there_is_no_next_page(self, _captcha):
        page = FakePage(
            [{"orders": [_order("1"), _order("2")], "next_url": None}]
        )

        orders = await _collect_order_history(page, 50)

        self.assertEqual(["1", "2"], [o["order_number"] for o in orders])
        page.goto.assert_not_awaited()

    @patch("amazon_mcp.server.check_captcha", new_callable=AsyncMock, return_value=None)
    async def test_deduplicates_orders_across_pages(self, _captcha):
        page = FakePage(
            [
                {
                    "orders": [_order("1"), _order("2")],
                    "next_url": "https://www.amazon.com/orders?page=2",
                },
                {
                    "orders": [_order("2"), _order("3")],
                    "next_url": None,
                },
            ]
        )

        orders = await _collect_order_history(page, 10)

        self.assertEqual(["1", "2", "3"], [o["order_number"] for o in orders])

    @patch("amazon_mcp.server.check_captcha", new_callable=AsyncMock, return_value=None)
    async def test_continues_through_year_filters_newest_first(self, _captcha):
        page = FakePage(
            [
                {
                    "orders": [_order("1"), _order("2")],
                    "next_url": None,
                    "year_filter_urls": [
                        {
                            "year": 2025,
                            "url": "https://www.amazon.com/orders?orderFilter=year-2025",
                        },
                        {
                            "year": 2026,
                            "url": "https://www.amazon.com/orders?orderFilter=year-2026",
                        },
                    ],
                },
                {
                    # The current-year filter overlaps the last-three-month view.
                    "orders": [_order("1"), _order("2"), _order("3")],
                    "next_url": None,
                    "active_filter": "year-2026",
                },
                {
                    "orders": [_order("4"), _order("5")],
                    "next_url": None,
                    "active_filter": "year-2025",
                },
            ]
        )

        orders = await _collect_order_history(page, 5)

        self.assertEqual(["1", "2", "3", "4", "5"], [
            order["order_number"] for order in orders
        ])
        self.assertEqual(
            [
                unittest.mock.call(
                    "https://www.amazon.com/orders?orderFilter=year-2026",
                    wait_until="domcontentloaded",
                    timeout=30000,
                ),
                unittest.mock.call(
                    "https://www.amazon.com/orders?orderFilter=year-2025",
                    wait_until="domcontentloaded",
                    timeout=30000,
                ),
            ],
            page.goto.await_args_list,
        )

    @patch(
        "amazon_mcp.server.check_captcha",
        new_callable=AsyncMock,
        return_value="Amazon is showing a CAPTCHA.",
    )
    async def test_checks_for_captcha_after_navigation(self, _captcha):
        page = FakePage(
            [
                {
                    "orders": [_order("1")],
                    "next_url": "https://www.amazon.com/orders?page=2",
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "CAPTCHA"):
            await _collect_order_history(page, 10)


if __name__ == "__main__":
    unittest.main()
