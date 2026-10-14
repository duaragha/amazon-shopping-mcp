import asyncio
import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from amazon_mcp.browser import (
    ProfileInUseError,
    acquire_profile_lock,
    authenticated_lock,
    close_all_browsers,
    close_logged_in_context,
    create_page,
    get_browser,
    get_browser_backend,
    get_logged_in_context,
    get_user_data_dir,
    release_profile_lock,
    reset_session,
)


class BrowserBackendTests(unittest.TestCase):
    def test_default_backend_is_playwright(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_browser_backend(), "playwright")

    def test_backend_env_vars_precedence_and_aliases(self):
        with patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "camoufox"}):
            self.assertEqual(get_browser_backend(), "camoufox")

        with patch.dict(os.environ, {"AMAZON_BROWSER_ENGINE": "CAMOUFOX"}):
            self.assertEqual(get_browser_backend(), "camoufox")

        with patch.dict(os.environ, {"BROWSER_BACKEND": "firefox"}):
            self.assertEqual(get_browser_backend(), "camoufox")

        with patch.dict(os.environ, {"AMAZON_BROWSER": "firefox_camoufox"}):
            self.assertEqual(get_browser_backend(), "camoufox")

        with patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "playwright"}):
            self.assertEqual(get_browser_backend(), "playwright")

    def test_user_data_dir_default_and_custom(self):
        with patch.dict(os.environ, {"AMAZON_USER_DATA_DIR": "/custom/path"}):
            self.assertEqual(get_user_data_dir(), "/custom/path")

        with patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "playwright"}, clear=True):
            self.assertEqual(
                get_user_data_dir(), os.path.expanduser("~/.amazon-mcp/user-data")
            )

        with patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "camoufox"}, clear=True):
            self.assertEqual(
                get_user_data_dir(),
                os.path.join(os.path.expanduser("~/.amazon-mcp/user-data"), "camoufox"),
            )


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await close_all_browsers()

    async def asyncTearDown(self):
        await close_all_browsers()

    @patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "camoufox"})
    async def test_camoufox_not_installed_raises_helpful_error(self):
        with patch.dict("sys.modules", {"camoufox": None, "camoufox.async_api": None}):
            with self.assertRaises(ImportError) as ctx:
                await get_browser()
            self.assertIn("camoufox fetch", str(ctx.exception))

            with self.assertRaises(ImportError) as ctx:
                await get_logged_in_context()
            self.assertIn("camoufox fetch", str(ctx.exception))

    @patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "camoufox"})
    async def test_camoufox_get_browser_and_create_page(self):
        fake_page = MagicMock()
        fake_context = MagicMock()
        fake_context.new_page = AsyncMock(return_value=fake_page)
        fake_context.route = AsyncMock()

        fake_browser = MagicMock()
        fake_browser.is_connected.return_value = True
        fake_browser.new_context = AsyncMock(return_value=fake_context)

        fake_cm = MagicMock()
        fake_cm.__aenter__ = AsyncMock(return_value=fake_browser)
        fake_cm.__aexit__ = AsyncMock(return_value=None)

        fake_camoufox_module = MagicMock()
        fake_camoufox_module.AsyncCamoufox = MagicMock(return_value=fake_cm)

        with patch.dict("sys.modules", {"camoufox": MagicMock(), "camoufox.async_api": fake_camoufox_module}):
            fake_browser.new_page = AsyncMock(return_value=fake_page)
            fake_page.context = fake_context

            browser = await get_browser()
            self.assertEqual(browser, fake_browser)
            fake_camoufox_module.AsyncCamoufox.assert_called_once_with(headless=True, geoip=True)

            page = await create_page(block_media=True)
            self.assertEqual(page, fake_page)
            fake_browser.new_page.assert_awaited_once()
            fake_context.route.assert_awaited_once()

    @patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "camoufox"})
    async def test_camoufox_logged_in_context_lifecycle(self):
        fake_context = MagicMock()
        fake_context.route = AsyncMock()

        fake_cm = MagicMock()
        fake_cm.__aenter__ = AsyncMock(return_value=fake_context)
        fake_cm.__aexit__ = AsyncMock(return_value=None)

        fake_camoufox_module = MagicMock()
        fake_camoufox_module.AsyncCamoufox = MagicMock(return_value=fake_cm)

        with patch.dict("sys.modules", {"camoufox": MagicMock(), "camoufox.async_api": fake_camoufox_module}):
            ctx = await get_logged_in_context(headless=False)
            self.assertEqual(ctx, fake_context)
            fake_camoufox_module.AsyncCamoufox.assert_called_once_with(
                headless=False,
                persistent_context=True,
                user_data_dir=get_user_data_dir(),
                geoip=True,
                locale="en-CA",
            )
            fake_context.route.assert_awaited_once()

            await close_logged_in_context()
            fake_cm.__aexit__.assert_awaited_once()

    @patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "playwright"})
    async def test_playwright_create_page_and_context(self):
        fake_page = MagicMock()
        fake_context = MagicMock()
        fake_page.context = fake_context
        fake_context.new_page = AsyncMock(return_value=fake_page)
        fake_context.route = AsyncMock()

        fake_browser = MagicMock()
        fake_browser.is_connected.return_value = True
        fake_browser.new_context = AsyncMock(return_value=fake_context)

        fake_pw = MagicMock()
        fake_pw.chromium.launch = AsyncMock(return_value=fake_browser)

        with patch("amazon_mcp.browser.async_playwright") as mock_pw_init:
            pw_cm = MagicMock()
            pw_cm.start = AsyncMock(return_value=fake_pw)
            mock_pw_init.return_value = pw_cm

            browser = await get_browser()
            self.assertEqual(browser, fake_browser)

            page = await create_page(block_media=True)
            self.assertEqual(page, fake_page)
            fake_context.route.assert_awaited_once()


class ProfileLockTests(unittest.TestCase):
    def tearDown(self):
        release_profile_lock()

    def test_acquire_and_release_profile_lock(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(tmp_dir) / ".amazon_mcp.lock"
            acquire_profile_lock(tmp_dir)
            self.assertTrue(lock_path.is_file())
            content = lock_path.read_text(encoding="utf-8")
            self.assertIn(f"PID {os.getpid()}", content)
            release_profile_lock()

    def test_profile_in_use_error_when_locked_by_another_process(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(tmp_dir) / ".amazon_mcp.lock"
            with open(lock_path, "a+") as f:
                f.write("PID=99999\nSTART_TIME=2026-09-16T12:00:00Z\n")
                f.flush()
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaises(ProfileInUseError) as ctx:
                        acquire_profile_lock(tmp_dir)
                    self.assertIn("PID=99999", str(ctx.exception))
                    self.assertIn(tmp_dir, str(ctx.exception))
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class SessionResetAndConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await close_all_browsers()

    async def asyncTearDown(self):
        await close_all_browsers()

    async def test_reset_session_closes_browsers_and_preserves_profile(self):
        result = await reset_session()
        self.assertTrue(result["session_reset"])
        self.assertIn("user_data_dir", result)
        self.assertIn("Browser session closed", result["message"])

    async def test_amazon_reset_session_does_not_deadlock(self):
        from amazon_mcp.server import amazon_reset_session

        result = await asyncio.wait_for(amazon_reset_session(), timeout=2)
        self.assertTrue(result.session_reset)

    @patch.dict(os.environ, {"AMAZON_BROWSER_BACKEND": "playwright"})
    async def test_concurrent_get_browser_calls_share_initialization(self):
        fake_browser = MagicMock()
        fake_browser.is_connected.return_value = True

        fake_pw = MagicMock()
        launch_mock = AsyncMock(return_value=fake_browser)
        fake_pw.chromium.launch = launch_mock

        with patch("amazon_mcp.browser.async_playwright") as mock_pw_init:
            pw_cm = MagicMock()
            pw_cm.start = AsyncMock(return_value=fake_pw)
            mock_pw_init.return_value = pw_cm

            # Run 5 concurrent calls
            results = await asyncio.gather(
                get_browser(),
                get_browser(),
                get_browser(),
                get_browser(),
                get_browser(),
            )

            # All 5 return the same browser instance
            for b in results:
                self.assertEqual(b, fake_browser)

            # launch should only have been called once
            self.assertEqual(launch_mock.await_count, 1)

    async def test_authenticated_lock_serializes_operations(self):
        order = []

        async def worker(n: int, delay: float):
            async with authenticated_lock():
                order.append(f"start-{n}")
                await asyncio.sleep(delay)
                order.append(f"end-{n}")

        await asyncio.gather(worker(1, 0.05), worker(2, 0.01))
        # Worker 1 runs to completion before worker 2 enters
        self.assertEqual(order, ["start-1", "end-1", "start-2", "end-2"])
