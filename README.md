# Amazon Shopping MCP

A Model Context Protocol (MCP) server that lets Claude browse Amazon for you. It searches products, scrapes full specs and details, reads reviews, and returns structured data so Claude can compare products and give you recommendations.

Built with Playwright (headless Edge) for reliable scraping that doesn't get blocked like simple HTTP requests.

## Features

- **Search Amazon** — search any Amazon domain (.ca, .com, .co.uk, etc.) and get structured results with price, rating, review count, and Prime status
- **Product Details** — scrape full product pages for specs, features, description, available colors/sizes, brand, pricing (current + list price), images, and availability. Supports **parallel scraping** of multiple products at once for fast comparison
- **Product Reviews** — pull rating distribution (5-star breakdown), overall rating, and individual reviews with title, body, star rating, date, and verified purchase status
- **CAPTCHA Detection** — automatically detects when Amazon shows a bot check and returns a clear error instead of garbage data
- **No account or API key needed** — just Playwright and a browser

## Tools

| Tool | Description |
|---|---|
| `amazon_search` | Search Amazon. Returns up to 20 results with title, price, rating, review count, Prime badge, ASIN, and product URL |
| `amazon_product_details` | Scrape one or more product pages in parallel. Returns specs, features, description, colors, sizes, brand, price, images, availability |
| `amazon_product_reviews` | Get reviews from a product page. Returns star distribution, overall rating, total review count, and individual reviews |

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Install

```bash
git clone https://github.com/duaragha/amazon-shopping-mcp.git
cd amazon-shopping-mcp
uv sync
```

Install a browser for Playwright:

```bash
# Use bundled Chromium (works everywhere, recommended)
.venv/bin/playwright install chromium

# Or use an existing browser by setting BROWSER_CHANNEL:
# "chrome", "msedge", etc. — no Playwright install needed
```

### Add to Claude Code

```bash
# Using bundled Chromium (default)
claude mcp add --scope user amazon-shopping -- /path/to/amazon-shopping-mcp/.venv/bin/python -m amazon_mcp

# Or to use a specific browser (e.g. Edge, Chrome):
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
      "args": ["-m", "amazon_mcp"]
    }
  }
}
```

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

## How It Works

Uses Playwright with a headless browser to load actual Amazon pages, then extracts data from the DOM using JavaScript evaluation. This approach is more reliable than API-based scrapers because:

- Renders the full page like a real browser
- Handles dynamic content loaded via JavaScript
- Doesn't require any API keys or affiliate accounts
- Supports any Amazon domain

The server runs over stdio using the MCP protocol, so it works with any MCP-compatible client.

## Limitations

- Amazon may occasionally show CAPTCHAs on heavy use — the server detects this and reports it
- Reviews are scraped from the product page (up to ~10-15 reviews) rather than the dedicated reviews page, which requires sign-in
- Product page layouts vary — some fields may be empty for certain products
- Scraping speed depends on your connection and Amazon's response time

## License

MIT
