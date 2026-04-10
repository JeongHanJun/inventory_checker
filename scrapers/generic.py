"""
범용 스크래퍼 — Musinsa/29cm 외 사이트에 대한 fallback.
Playwright로 페이지를 열고 JSON 응답에서 옵션 배열을 탐색.
"""

import json
from urllib.parse import urlparse

from .base import BaseScraper, ProductInfo


class GenericScraper(BaseScraper):
    SITE_NAME = "Generic"

    async def scrape(self, url: str) -> ProductInfo:
        domain = urlparse(url).netloc

        captured: list[dict] = []
        next_data_raw = None
        product_name = ""

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                f"이 사이트({domain})는 지원되지 않습니다. "
                "현재 지원: 무신사, 29cm, Nike KR"
            )

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()

            async def on_response(resp: Response):
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                try:
                    data = await resp.json()
                    captured.append({"url": resp.url, "data": data})
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="networkidle", timeout=40000)
            except Exception:
                pass

            next_data_raw = await page.evaluate(
                "() => { const el = document.getElementById('__NEXT_DATA__'); "
                "return el ? el.textContent : null; }"
            )

            try:
                product_name = (await page.title()).split("|")[0].strip()
            except Exception:
                product_name = domain

            await browser.close()

        if next_data_raw:
            try:
                nd = json.loads(next_data_raw)
                opts = self._find_options_in_json(nd)
                if opts:
                    return ProductInfo(name=product_name, url=url,
                                       site=domain, options=opts)
            except Exception:
                pass

        for resp_obj in captured:
            try:
                opts = self._find_options_in_json(resp_obj["data"])
                if opts:
                    return ProductInfo(name=product_name, url=url,
                                       site=domain, options=opts,
                                       raw_debug={"source_url": resp_obj["url"]})
            except Exception:
                continue

        raise RuntimeError(
            f"지원되지 않는 사이트이거나 옵션 데이터를 찾을 수 없습니다. "
            f"(도메인: {domain}, 캡처된 응답: {len(captured)}개)"
        )
