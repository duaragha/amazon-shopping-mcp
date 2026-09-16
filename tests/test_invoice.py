import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from amazon_mcp.server import (
    _parse_hsa_fsa_eligibility,
    amazon_download_invoice,
    amazon_order_hsa_eligibility,
)


class FakePage:
    def __init__(self, details):
        self.url = "about:blank"
        self.details = details
        self.route = AsyncMock()
        self.goto = AsyncMock(side_effect=self._goto)
        self.query_selector = AsyncMock(return_value=None)
        self.evaluate = AsyncMock(return_value=details)
        self.wait_for_function = AsyncMock()
        self.add_style_tag = AsyncMock()
        self.pdf = AsyncMock(side_effect=self._pdf)
        self.content = AsyncMock(return_value="<html><h1>Order Summary</h1></html>")
        self.close = AsyncMock()

    async def _goto(self, url, **_kwargs):
        self.url = url

    async def _pdf(self, *, path, **_kwargs):
        Path(path).write_bytes(b"%PDF-1.7\nfake invoice\n")


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.new_page = AsyncMock(return_value=page)


class HsaFsaParserTests(unittest.TestCase):
    def test_classifies_partially_eligible_order(self):
        result = _parse_hsa_fsa_eligibility(
            "Grand Total: $54.29 "
            "FSA or HSA eligible amount (includes taxes & shipping): $27.28"
        )

        self.assertEqual("$27.28", result["hsa_fsa_eligible_amount"])
        self.assertEqual("$54.29", result["order_total"])
        self.assertEqual("partial", result["hsa_fsa_eligibility_scope"])
        self.assertFalse(result["entire_order_hsa_fsa_eligible"])
        self.assertTrue(result["hsa_fsa_amount_includes_tax_and_shipping"])

    def test_classifies_fully_eligible_order(self):
        result = _parse_hsa_fsa_eligibility(
            "Order Total: US$ 12.00 "
            "HSA/FSA eligible (inc. tax and shipping): US$ 12.00"
        )

        self.assertEqual("full", result["hsa_fsa_eligibility_scope"])
        self.assertTrue(result["entire_order_hsa_fsa_eligible"])

    def test_classifies_explicit_zero_as_none(self):
        result = _parse_hsa_fsa_eligibility(
            "Grand Total: $40.00 FSA or HSA eligible amount: $0.00"
        )

        self.assertEqual("none", result["hsa_fsa_eligibility_scope"])
        self.assertFalse(result["entire_order_hsa_fsa_eligible"])

    def test_missing_eligibility_text_is_unknown_not_zero(self):
        result = _parse_hsa_fsa_eligibility("Grand Total: $40.00")

        self.assertIsNone(result["hsa_fsa_eligible_amount"])
        self.assertEqual("unknown", result["hsa_fsa_eligibility_scope"])
        self.assertIsNone(result["entire_order_hsa_fsa_eligible"])


class DownloadInvoiceTests(unittest.IsolatedAsyncioTestCase):
    @patch("amazon_mcp.server.get_logged_in_context", new_callable=AsyncMock)
    async def test_rejects_invalid_order_number_before_opening_browser(self, browser):
        result = json.loads(await amazon_download_invoice("not-an-order"))

        self.assertIn("Invalid Amazon order number", result["error"])
        browser.assert_not_awaited()

    async def test_saves_printable_order_summary_as_private_pdf(self):
        page = FakePage(
            {
                "heading": "Order Summary",
                "title": "Order Details",
                "document_type": "order_summary",
                "contains_order_number": True,
                "unavailable": False,
                "body_text": (
                    "Order 112-1234567-1234567 Grand Total: $54.29 "
                    "FSA or HSA eligible amount "
                    "(includes taxes & shipping): $27.28"
                ),
            }
        )
        context = FakeContext(page)
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "amazon_mcp.server.INVOICE_DIR", temp_dir
        ), patch(
            "amazon_mcp.server.get_logged_in_context",
            new_callable=AsyncMock,
            return_value=context,
        ), patch(
            "amazon_mcp.server.check_captcha",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = json.loads(
                await amazon_download_invoice(
                    "112-1234567-1234567", domain="com"
                )
            )

            destination = Path(result["path"])
            self.assertTrue(result["downloaded"])
            self.assertEqual("order_summary", result["document_type"])
            self.assertFalse(result["official_invoice"])
            self.assertEqual("application/pdf", result["mime_type"])
            self.assertEqual("$27.28", result["hsa_fsa_eligible_amount"])
            self.assertEqual("partial", result["hsa_fsa_eligibility_scope"])
            self.assertFalse(result["entire_order_hsa_fsa_eligible"])
            self.assertEqual(b"%PDF-1.7\nfake invoice\n", destination.read_bytes())
            self.assertEqual(0o600, os.stat(destination).st_mode & 0o777)
            page.pdf.assert_awaited_once()
            page.close.assert_awaited_once()

    async def test_reports_seller_invoice_request_without_writing_file(self):
        page = FakePage(
            {
                "heading": None,
                "title": "Order Details",
                "document_type": "order_summary",
                "contains_order_number": True,
                "unavailable": True,
            }
        )
        context = FakeContext(page)
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "amazon_mcp.server.INVOICE_DIR", temp_dir
        ), patch(
            "amazon_mcp.server.get_logged_in_context",
            new_callable=AsyncMock,
            return_value=context,
        ), patch(
            "amazon_mcp.server.check_captcha",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = json.loads(
                await amazon_download_invoice(
                    "112-1234567-1234567", domain="com"
                )
            )

            self.assertIn("does not currently provide an invoice", result["error"])
            self.assertEqual("Request the invoice from the seller in Your Orders.", result["action_required"])
            self.assertEqual([], list(Path(temp_dir).iterdir()))
            page.pdf.assert_not_awaited()


class OrderHsaEligibilityTests(unittest.IsolatedAsyncioTestCase):
    @patch("amazon_mcp.server.get_logged_in_context", new_callable=AsyncMock)
    async def test_rejects_invalid_order_number_before_opening_browser(self, browser):
        result = json.loads(await amazon_order_hsa_eligibility("not-an-order"))

        self.assertIn("Invalid Amazon order number", result["error"])
        browser.assert_not_awaited()

    async def test_reports_amount_and_partial_scope_without_saving_document(self):
        page = FakePage(
            {
                "heading": "Order Summary",
                "title": "Order Details",
                "document_type": "order_summary",
                "contains_order_number": True,
                "unavailable": False,
                "body_text": (
                    "Order 112-1234567-1234567 Grand Total: $54.29 "
                    "FSA or HSA eligible amount "
                    "(includes taxes & shipping): $27.28"
                ),
            }
        )
        context = FakeContext(page)
        with patch(
            "amazon_mcp.server.get_logged_in_context",
            new_callable=AsyncMock,
            return_value=context,
        ), patch(
            "amazon_mcp.server.check_captcha",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = json.loads(
                await amazon_order_hsa_eligibility(
                    "112-1234567-1234567", domain="com"
                )
            )

        self.assertEqual("$27.28", result["hsa_fsa_eligible_amount"])
        self.assertEqual("partial", result["hsa_fsa_eligibility_scope"])
        self.assertFalse(result["entire_order_hsa_fsa_eligible"])
        page.pdf.assert_not_awaited()
        page.content.assert_not_awaited()
        page.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
