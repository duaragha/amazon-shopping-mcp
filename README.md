# Amazon Shopping MCP

A Model Context Protocol (MCP) server that lets Claude shop Amazon for you — end to end. It searches products, scrapes full specs and reviews, and (once you log in) can add items to your cart and place orders using the cards and addresses already saved in your Amazon account.

Built with Playwright for reliable browsing that doesn't get blocked like simple HTTP requests.

## Features

- **Search Amazon** — search any Amazon domain (.ca, .com, .co.uk, etc.) and get structured results with price, rating, review count, and Prime status
- **Product Details** — scrape full product pages for specs, features, description, available colors/sizes, brand, pricing (current + list price), images, and availability. Supports **parallel scraping** of multiple products at once for fast comparison
- **Product Reviews** — pull rating distribution (5-star breakdown), overall rating, and individual reviews with title, body, star rating, date, and verified purchase status
- **Buy products** — log in once, then list your saved addresses & cards, add to cart, view cart, and place orders. **Checkout requires an explicit confirm step** so nothing is ever bought without your go-ahead
- **No raw card numbers, ever** — purchases use the cards/addresses already in your Amazon account; the server only ever sees the last 4 digits Amazon itself displays
- **CAPTCHA Detection** — automatically detects when Amazon shows a bot check and returns a clear error instead of garbage data

## Tools

### Browse (no login required)

| Tool | Description |
|---|---|
| `amazon_search` | Search Amazon. Returns up to 20 results with title, price, rating, review count, Prime badge, ASIN, and product URL |
| `amazon_product_details` | Scrape one or more product pages in parallel. Returns specs, features, description, colors, sizes, brand, price, images, availability |
| `amazon_product_reviews` | Get reviews from a product page. Returns star distribution, overall rating, total review count, and individual reviews |

### Buy (requires `amazon_login` first)

| Tool | Description |
|---|---|
| `amazon_login` | Opens a **real, visible** browser window and waits until you sign in by hand (password and 2FA). Session is saved to disk and reused by every tool below |
| `amazon_login_status` | Check whether the saved session is still signed in, and as whom |
| `amazon_list_addresses` | List shipping addresses saved in your account, each with an `index` for checkout |
| `amazon_list_payment_methods` | List saved cards — **last 4 digits, network, and expiry only**, each with an `index` |
| `amazon_add_to_cart` | Add a product (by ASIN or URL) to your cart, with optional quantity |
| `amazon_view_cart` | View cart line items, quantities, and subtotal |
| `amazon_place_order` | Walk through checkout. Default = **preview only** (returns ship-to / payment / total). Call again with `confirm=true` to actually place the order |
| `amazon_subscribe` | Set up Subscribe & Save on a product. Default = **preview only** (base price / discount / cadence). `confirm=true` creates the **recurring** subscription |
| `amazon_reset_session` | Close active browser instances and release profile locks while preserving saved login credentials on disk |

### Orders & returns (requires `amazon_login` first)

| Tool | Description |
|---|---|
| `amazon_list_orders` | List orders across paginated and yearly history with a 5-page budget, continuation token, and stop reason |
| `amazon_search_orders` | Search past orders by keyword using Amazon's order search form |
| `amazon_get_order` | Fetch direct details, scoped card text, and labeled links for a specific order number |
| `amazon_get_order_document` | Extract visible text, labeled links, heading, and structured HSA/FSA eligibility from an order's printable summary or invoice document |
| `amazon_download_invoice` | Save an order's invoice or printable summary as HTML (default) or rendered PDF (Chromium), with private file permissions |
| `amazon_view_returns` | List in-progress / past returns with their status (from the returns-filtered order history) |
| `amazon_start_return` | Open the return wizard for an order. Default = **preview only** (returnable items + reason options); `confirm=true` to submit |

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Install

```bash
git clone https://github.com/duaragha/amazon-shopping-mcp.git
cd amazon-shopping-mcp

# Option A: Default with Playwright
uv sync
.venv/bin/playwright install chromium

# Option B: Optional Camoufox anti-detect browser (https://camoufox.com/)
uv sync --extra camoufox
uv run camoufox fetch
```

# Or use an existing browser for Playwright by setting BROWSER_CHANNEL:
# "chrome", "msedge", etc. — no Playwright install needed


### Add to Claude Code

```bash
# Using Playwright with bundled Chromium (default)
claude mcp add --scope user amazon-shopping -- /path/to/amazon-shopping-mcp/.venv/bin/python -m amazon_mcp

# Using Camoufox (anti-detect browser)
claude mcp add --scope user -e AMAZON_BROWSER_BACKEND=camoufox amazon-shopping -- /path/to/amazon-shopping-mcp/.venv/bin/python -m amazon_mcp

# Or using a specific Playwright browser (e.g. Edge, Chrome):
claude mcp add --scope user -e BROWSER_CHANNEL=msedge amazon-shopping -- /path/to/amazon-shopping-mcp/.venv/bin/python -m amazon_mcp
```

Replace `/path/to/amazon-shopping-mcp` with the actual path where you cloned the repo.

Then restart Claude Code (or start a new session) to pick up the new tools.

### Add to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "amazon-shopping": {
      "command": "/path/to/amazon-shopping-mcp/.venv/bin/python",
      "args": ["-m", "amazon_mcp"],
      "env": {
        "AMAZON_BROWSER_BACKEND": "camoufox"
      }
    }
  }
}
```
*(Omit the `"env"` block if you want to use the default Playwright engine.)*


## Usage

Once configured, just ask Claude naturally:

> "Find me the best webcam under $100 on Amazon"

Claude will:
1. **Search** Amazon for webcams
2. **Scrape details** for the top candidates (in parallel)
3. **Read reviews** for the finalists
4. **Compare** everything and give you a top 3 with reasoning and direct links

You can also be more specific:

> "Compare these two products on Amazon: [url1] and [url2]"

> "What are people complaining about in the reviews for [url]?"

> "Search Amazon US for mechanical keyboards under $150 and tell me which one has the best build quality"

### Buying something

First, sign in (one time per session lifetime):

> "Log me into Amazon"

This opens a real browser window — type your password and any 2FA code, and the session is saved. Then:

> "List my Amazon addresses and cards"

> "Add the Logitech MX Master 3S to my cart"

> "Check out shipping to my home address with the Visa — but show me the total before placing it"

Claude previews the order (items, ship-to, payment, total). Only when you confirm:

> "Yes, place it"

…does it actually buy. **Nothing is purchased without that explicit confirmation.**

### Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `AMAZON_BROWSER_BACKEND` | `playwright` | Browser engine to use: `playwright` (Chromium) or `camoufox` (anti-detect Firefox) |
| `AMAZON_DOMAIN` | `ca` | Default storefront for account/cart/checkout |
| `AMAZON_USER_DATA_DIR` | `~/.amazon-mcp/user-data` (`.../camoufox` for Camoufox) | Where the logged-in browser profile is stored |
| `AMAZON_HEADLESS` | `1` | Set `0` to watch the buying tools drive the browser (useful for debugging checkout) |
| `AMAZON_HUMAN_PAUSE` | `1` | Set `0` to disable the order-read pacing window |
| `BROWSER_CHANNEL` | _(unset)_ | Use an installed browser (`chrome`, `msedge`) instead of bundled Chromium (Playwright only) |
| `AMAZON_INVOICE_DIR` | `~/.amazon-mcp/invoices` | Private directory for downloaded invoices and printable order summaries |

### Parameters

**`amazon_search`**
- `query` — search terms (e.g. "wireless earbuds under $50")
- `domain` — Amazon domain suffix: `"ca"` (default), `"com"`, `"co.uk"`, etc.
- `max_results` — number of results to return (default: 20)

**`amazon_product_details`**
- `urls` — list of Amazon product page URLs (scraped in parallel, up to 8 concurrent)

**`amazon_product_reviews`**
- `url` — Amazon product page URL
- `max_reviews` — max reviews to return (default: 15)

**`amazon_list_orders`**
- `max_orders` — maximum orders to return (default: 10)
- `year` — optional 4-digit year filter (e.g. `2024`)
- `continuation_token` — token from a previous call to resume pagination
- `domain` — Amazon domain suffix: `"ca"` (default), `"com"`, etc.
- Scans order history with a 5-page budget per call, returning structured order summaries, `continuation_token`, and `stop_reason` (`target_reached`, `budget_exhausted`, `no_more_pages`, etc.).

**`amazon_search_orders`**
- `query` — search terms for past orders
- `max_orders` — maximum matching orders to return (default: 10)
- `domain` — Amazon domain suffix: `"ca"` (default), `"com"`, etc.
- Uses Amazon's native order search form with a 5-page budget and continuation support.

**`amazon_get_order`**
- `order_number` — Amazon order number (e.g. `112-1234567-1234567`)
- `domain` — Amazon domain suffix: `"ca"` (default), `"com"`, etc.
- Retrieves direct details, scoped order card text (preserving line breaks), and labeled links.

**`amazon_get_order_document`**
- `order_number` — Amazon order number (e.g. `112-1234567-1234567`)
- Extracts raw visible text, displayed heading, labeled links, and parsed structured HSA/FSA eligibility (`hsa_fsa` with boolean `is_eligible`, formatted `amount`, float `amount_value`, and `includes_tax_and_shipping`) from Amazon's printable order summary or invoice document.

**`amazon_download_invoice`**
- `order_number` — Amazon order number (e.g. `112-1234567-1234567`)
- `domain` — Amazon domain suffix: `"ca"` (default), `"com"`, etc.
- `output_format` — `"html"` (default) or `"pdf"`
- Saves the printable document or invoice to `AMAZON_INVOICE_DIR` with private permissions (`0o600`). PDF rendering is supported on Chromium (Playwright backend); Camoufox currently returns an unsupported-format error due to upstream limitations ([daijro/camoufox#593](https://github.com/daijro/camoufox/issues/593)).

**`amazon_reset_session`**
- Takes no parameters. Closes active browser instances and releases application profile locks without clearing saved authentication credentials on disk.

## How It Works

Uses Playwright with a headless browser to load actual Amazon pages, then extracts data from the DOM using JavaScript evaluation. This approach is more reliable than API-based scrapers because:

- Renders the full page like a real browser
- Handles dynamic content loaded via JavaScript
- Doesn't require any API keys or affiliate accounts
- Supports any Amazon domain

The server runs over stdio using the MCP protocol, so it works with any MCP-compatible client.

## Limitations & cautions

- **Buying tools automate a logged-in session, which is against Amazon's Terms of Service.** Heavy or bot-like use can get your account flagged or restricted. Use it sparingly and at your own risk.
- **Login is human-in-the-loop.** Password and 2FA/OTP are typed by you in a real browser window; they're never stored or automated. `amazon_login` therefore needs a graphical display.
- **Checkout DOM is heavily obfuscated and region-specific.** The address/payment selection and place-order selectors are best-effort and may need tweaking after Amazon layout changes — run with `AMAZON_HEADLESS=0` to watch and adjust. `amazon_place_order` never clicks "place order" unless you pass `confirm=true`.
- Amazon may occasionally show CAPTCHAs on heavy use — the server detects this and reports it
- Reviews are scraped from the product page (up to ~10-15 reviews) rather than the dedicated reviews page, which requires sign-in
- Product page layouts vary — some fields may be empty for certain products
- Scraping speed depends on your connection and Amazon's response time

## License

MIT
