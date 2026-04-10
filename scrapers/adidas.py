"""
Adidas Korea 스크래퍼 — httpx 전용 (Playwright 불필요)

확인된 API 구조:
  상품 정보:
    www.adidas.co.kr/api/products/{pid}?sitePath=kr
    → .name                          (상품명)
    → .attribute_list.color          (색상명)
    → .pricing_information.currentPrice (현재가)
    ※ Mobile UA 필요: adidas/4.24.2 Android/14 Mobile

  재고 API:
    www.adidas.co.kr/api/products/{pid}/availability
    → .variation_list[].size             (사이즈명)
    → .variation_list[].availability     (재고 수량)
    → .variation_list[].availability_status (IN_STOCK / NOT_AVAILABLE)
    ※ sitePath 파라미터 없이 Mobile UA로만 호출

  URL 패턴:
    https://www.adidas.co.kr/{상품명-slug}/{pid}.html
    → pid: 영문+숫자 (예: JR5240, KJ8872)
"""

import re
import httpx

from .base import BaseScraper, ProductInfo, ProductOption


class AdidasScraper(BaseScraper):
    SITE_NAME = "Adidas"

    _PID_RE = re.compile(r"/([A-Z0-9]{4,10})\.html", re.IGNORECASE)

    _HEADERS = {
        "User-Agent": "adidas/4.24.2 Android/14 Mobile",
        "Accept": "application/json",
    }

    def _extract_pid(self, url: str) -> str:
        m = self._PID_RE.search(url)
        if not m:
            raise ValueError(f"Adidas URL에서 상품 ID를 추출할 수 없습니다: {url}")
        return m.group(1).upper()

    async def scrape(self, url: str) -> ProductInfo:
        pid = self._extract_pid(url)

        import asyncio

        async with httpx.AsyncClient(
            headers=self._HEADERS, timeout=15.0, follow_redirects=True
        ) as client:
            info_task = asyncio.create_task(self._fetch_product_info(client, pid))
            avail_task = asyncio.create_task(self._fetch_availability(client, pid))
            (name, color, price), variations = await asyncio.gather(info_task, avail_task)

        if not variations:
            raise RuntimeError(
                f"Adidas 재고 정보를 가져올 수 없습니다. (pid={pid})"
            )

        options = []
        for v in variations:
            stock = v.get("availability", 0)
            soldout = v.get("availability_status", "NOT_AVAILABLE") != "IN_STOCK"
            options.append(ProductOption(
                color=color or pid,
                size=v.get("size", ""),
                stock=stock,
                price=price,
                soldout=soldout,
                option_id=v.get("sku", ""),
            ))

        return ProductInfo(
            name=name or f"Adidas {pid}",
            url=url,
            site=self.SITE_NAME,
            options=options,
        )

    async def _fetch_product_info(
        self, client: httpx.AsyncClient, pid: str
    ) -> tuple[str, str, int]:
        """(상품명, 색상명, 현재가) 반환"""
        try:
            r = await client.get(
                f"https://www.adidas.co.kr/api/products/{pid}?sitePath=kr"
            )
            if r.status_code != 200:
                return "", "", 0
            d = r.json()
            name = d.get("name", "")
            attrs = d.get("attribute_list", {})
            color = attrs.get("color", "")
            pricing = d.get("pricing_information", {})
            price = int(pricing.get("currentPrice", pricing.get("standard_price", 0)))
            return name, color, price
        except Exception:
            return "", "", 0

    async def _fetch_availability(
        self, client: httpx.AsyncClient, pid: str
    ) -> list[dict]:
        """사이즈별 재고 목록 반환"""
        try:
            r = await client.get(
                f"https://www.adidas.co.kr/api/products/{pid}/availability"
            )
            if r.status_code != 200:
                return []
            return r.json().get("variation_list", [])
        except Exception:
            return []
