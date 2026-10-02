import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.fastmcp.exceptions import ToolError
from playwright.async_api import async_playwright

from amazon_mcp.orders import (
    DownloadInvoiceResult,
    OrderDocumentResult,
    SCRAPE_DOCUMENT_DOM_SCRIPT,
    download_order_invoice,
    fetch_order_document,
    reset_order_pace,
)
from amazon_mcp.server import (
    amazon_download_invoice,
    amazon_get_order_document,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class DOMDocumentExtractionTests(unittest.IsolatedAsyncioTestCase):
    """Tests SCRAPE_DOCUMENT_DOM_SCRIPT directly against real printable fixtures."""

    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.page.close()
        await self.browser.close()
        await self.pw.stop()

    async def test_extracts_printable_document_info(self):
        html = _read_fixture("order_summary_printable.html")
        await self.page.set_content(html)
        result = await self.page.evaluate(SCRAPE_DOCUMENT_DOM_SCRIPT)

        self.assertEqual(result["heading"], "Final Details for Order #114-7654321-1234567")
        self.assertEqual(result["title"], "Amazon.com - Printable Order Summary")
        self.assertEqual(result["document_type"], "order_summary")
        self.assertFalse(result["unavailable"])
        self.assertTrue(result["contains_order_number"])
        self.assertIn("114-7654321-1234567", result["body_text"])
        self.assertIn("Logitech Wireless Mouse", result["body_text"])
        self.assertIn("HSA or FSA eligible amount", result["body_text"])
        self.assertIn("$27.28", result["body_text"])

        doc = OrderDocumentResult(
            order_number="114-7654321-1234567",
            heading=result.get("heading"),
            title=result.get("title"),
            document_type=result.get("document_type", "order_summary"),
            body_text=result.get("body_text", ""),
            source_url="https://www.amazon.com/gp/css/summary/print.html",
        )
        self.assertIsNotNone(doc.hsa_fsa)
        self.assertTrue(doc.hsa_fsa.is_eligible)
        self.assertEqual(doc.hsa_fsa.amount, "$27.28")
        self.assertEqual(doc.hsa_fsa.amount_value, 27.28)
        self.assertTrue(doc.hsa_fsa.includes_tax_and_shipping)
        self.assertEqual(
            doc.hsa_fsa.raw_text,
            "HSA or FSA eligible amount (includes taxes & shipping): $27.28",
        )


class FakeDocumentPage:
    def __init__(self, doc_data, content_html="<html><body><h1>Test Doc</h1></body></html>"):
        self.url = "https://www.amazon.ca/gp/css/summary/print.html?orderID=114-7654321-1234567"
        self.doc_data = doc_data
        self._content_html = content_html
        self.goto = AsyncMock()
        self.wait_for_function = AsyncMock()
        self.query_selector = AsyncMock(return_value=None)
        self.route = AsyncMock()
        self.add_style_tag = AsyncMock()
        self.content = AsyncMock(return_value=self._content_html)
        self.pdf = AsyncMock(side_effect=self._pdf)
        self.close = AsyncMock()

    async def evaluate(self, _script, *_args, **_kwargs):
        return self.doc_data

    async def _pdf(self, path=None, **_kwargs):
        if path:
            Path(path).write_bytes(b"%PDF-1.7\nSample Rendered PDF Document\n")


class _OrderPaceOff:
    """Keep invoice tests out of the process-wide order pacing window."""

    async def asyncSetUp(self):
        reset_order_pace()
        self._pace_off = patch.dict(os.environ, {"AMAZON_HUMAN_PAUSE": "0"})
        self._pace_off.start()

    async def asyncTearDown(self):
        self._pace_off.stop()
        reset_order_pace()


class OrderDocumentToolTests(_OrderPaceOff, unittest.IsolatedAsyncioTestCase):
    """Tests amazon_get_order_document and fetch_order_document."""

    async def test_rejects_invalid_order_number(self):
        with self.assertRaises(ToolError) as ctx:
            await amazon_get_order_document("invalid-order")
        self.assertIn("Invalid Amazon order number", str(ctx.exception))

    async def test_fetches_printable_order_document_success(self):
        page = FakeDocumentPage({
            "heading": "Order Summary",
            "title": "Printable Order Summary",
            "document_type": "order_summary",
            "contains_order_number": True,
            "unavailable": False,
            "body_text": "Order Placed: Sept 12, 2026\nOrder Total: $45.99",
            "links": [{"label": "Help", "url": "https://www.amazon.ca/help"}],
        })

        res: OrderDocumentResult = await fetch_order_document(
            page=page,
            order_number="114-7654321-1234567",
            domain="ca",
            check_captcha_fn=AsyncMock(return_value=None),
        )

        self.assertEqual(res.order_number, "114-7654321-1234567")
        self.assertEqual(res.heading, "Order Summary")
        self.assertEqual(res.document_type, "order_summary")
        self.assertIn("$45.99", res.body_text)
        self.assertEqual(len(res.links), 1)
        self.assertIsNone(res.hsa_fsa)

    async def test_fetches_printable_order_document_with_hsa_fsa(self):
        page = FakeDocumentPage({
            "heading": "Final Details for Order #114-7654321-1234567",
            "title": "Amazon.com - Printable Order Summary",
            "document_type": "order_summary",
            "contains_order_number": True,
            "unavailable": False,
            "body_text": (
                "Final Details for Order #114-7654321-1234567\n"
                "Order Total: $45.99\n"
                "HSA or FSA eligible amount (includes taxes & shipping): $27.28\n"
                "1 of: Logitech Wireless Mouse\n"
            ),
            "links": [],
        })

        res: OrderDocumentResult = await fetch_order_document(
            page=page,
            order_number="114-7654321-1234567",
            domain="com",
            check_captcha_fn=AsyncMock(return_value=None),
        )

        self.assertIsNotNone(res.hsa_fsa)
        self.assertTrue(res.hsa_fsa.is_eligible)
        self.assertEqual(res.hsa_fsa.amount, "$27.28")
        self.assertEqual(res.hsa_fsa.amount_value, 27.28)
        self.assertTrue(res.hsa_fsa.includes_tax_and_shipping)
        self.assertEqual(
            res.hsa_fsa.raw_text,
            "HSA or FSA eligible amount (includes taxes & shipping): $27.28",
        )

    async def test_unavailable_document_raises_tool_error(self):
        page = FakeDocumentPage({
            "heading": None,
            "title": "Amazon.ca",
            "document_type": "order_summary",
            "contains_order_number": False,
            "unavailable": True,
            "body_text": "An invoice is not available for this order.",
            "links": [],
        })

        with self.assertRaises(ToolError) as ctx:
            await fetch_order_document(
                page=page,
                order_number="114-7654321-1234567",
                domain="ca",
                check_captcha_fn=AsyncMock(return_value=None),
            )
        self.assertIn("does not currently provide", str(ctx.exception))


class DownloadInvoiceToolTests(_OrderPaceOff, unittest.IsolatedAsyncioTestCase):
    """Tests amazon_download_invoice permissions, HTML/PDF formats, and backend rejection."""

    async def test_rejects_invalid_order_number(self):
        with self.assertRaises(ToolError) as ctx:
            await amazon_download_invoice("bad-number")
        self.assertIn("Invalid Amazon order number", str(ctx.exception))

    async def test_rejects_invalid_format(self):
        with self.assertRaises(ToolError) as ctx:
            await amazon_download_invoice("114-7654321-1234567", output_format="docx")
        self.assertIn("Invalid output_format", str(ctx.exception))

    async def test_camoufox_explicitly_rejects_pdf_before_writing_file(self):
        page = FakeDocumentPage({
            "heading": "Order Summary",
            "title": "Printable Order Summary",
            "document_type": "order_summary",
            "contains_order_number": True,
            "unavailable": False,
            "body_text": "Order Details",
            "links": [],
        })

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "camoufox"}):
                with self.assertRaises(ToolError) as ctx:
                    await download_order_invoice(
                        page=page,
                        order_number="114-7654321-1234567",
                        output_format="pdf",
                        domain="ca",
                        invoice_dir=tmp_dir,
                        check_captcha_fn=AsyncMock(return_value=None),
                    )
                self.assertIn("Camoufox", str(ctx.exception))
                self.assertIn("output_format='html'", str(ctx.exception))
                # Ensure no files were written
                self.assertEqual(len(list(Path(tmp_dir).iterdir())), 0)
                page.pdf.assert_not_awaited()

    async def test_downloads_html_with_private_permissions(self):
        page = FakeDocumentPage(
            {
                "heading": "Final Details for Order #114-7654321-1234567",
                "title": "Amazon.ca - Printable Order Summary",
                "document_type": "order_summary",
                "contains_order_number": True,
                "unavailable": False,
                "body_text": "Order Details",
                "links": [],
            },
            content_html="<html><head><title>Test</title></head><body><h1>Order Summary</h1></body></html>",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            res: DownloadInvoiceResult = await download_order_invoice(
                page=page,
                order_number="114-7654321-1234567",
                output_format="html",
                domain="ca",
                invoice_dir=tmp_dir,
                check_captcha_fn=AsyncMock(return_value=None),
            )

            self.assertTrue(res.downloaded)
            self.assertEqual(res.output_format, "html")
            self.assertEqual(res.order_number, "114-7654321-1234567")
            self.assertEqual(res.heading, "Final Details for Order #114-7654321-1234567")

            saved_path = Path(res.path)
            self.assertTrue(saved_path.is_file())
            self.assertEqual(saved_path.suffix, ".html")

            # Check directory permissions (0o700) and file permissions (0o600)
            file_mode = stat.S_IMODE(saved_path.stat().st_mode)
            dir_mode = stat.S_IMODE(saved_path.parent.stat().st_mode)
            self.assertEqual(file_mode, 0o600)
            self.assertEqual(dir_mode, 0o700)

            content = saved_path.read_text(encoding="utf-8")
            self.assertIn("<h1>Order Summary</h1>", content)

    async def test_downloads_pdf_with_chromium_backend(self):
        page = FakeDocumentPage({
            "heading": "Final Details for Order #114-7654321-1234567",
            "title": "Amazon.ca - Printable Order Summary",
            "document_type": "order_summary",
            "contains_order_number": True,
            "unavailable": False,
            "body_text": "Order Details",
            "links": [],
        })

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "playwright"}):
                res: DownloadInvoiceResult = await download_order_invoice(
                    page=page,
                    order_number="114-7654321-1234567",
                    output_format="pdf",
                    domain="ca",
                    invoice_dir=tmp_dir,
                    check_captcha_fn=AsyncMock(return_value=None),
                )

                self.assertTrue(res.downloaded)
                self.assertEqual(res.output_format, "pdf")
                saved_path = Path(res.path)
                self.assertTrue(saved_path.is_file())
                self.assertEqual(saved_path.suffix, ".pdf")
                page.pdf.assert_awaited_once()

                # Verify permissions
                self.assertEqual(stat.S_IMODE(saved_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
