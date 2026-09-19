from __future__ import annotations

from pathlib import Path


class LocalBrowser:
    """Local Playwright browser with a phone-sized viewport for task research.

    It intentionally exposes read-only operations. Any purchase, booking, or
    submission stays outside this tool and must return through bloom's explicit
    approval path.
    """

    def __init__(self, *, screenshots_dir: str | Path = "data/screenshots") -> None:
        self.screenshots_dir = Path(screenshots_dir)

    async def research(self, url: str) -> dict[str, str]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Browser tasks require `pip install -e .[tasks]` and `playwright install chromium`.") from exc

        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot = self.screenshots_dir / "source.png"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.add_style_tag(content="* { scrollbar-width: none !important; } [style*='position: fixed'], [style*='position: sticky'] { display: none !important; }")
                text = (await page.locator("body").inner_text())[:8_000]
                await page.screenshot(path=str(screenshot), full_page=False)
                return {"url": page.url, "text": text, "screenshot": str(screenshot)}
            finally:
                await browser.close()

