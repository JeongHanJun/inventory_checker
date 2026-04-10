"""
29cm 스크래퍼 - 직접 API 호출 (Playwright 미사용, ~1-3초)

확인된 API:
  GET https://bff-api.29cm.co.kr/api/v5/product-detail/{id}

확인된 옵션 구조:
  data.optionItems.layout  = ["COLOR", "SIZE"]
  data.optionItems.list    = [
    { title: "WHITE",                              ← 색상(1단계)
      list: [
        { title: "S", limitedQty: 986,             ← 사이즈(2단계) + 재고
          frontOptionStockStatus: "ON_STOCK",
          sellPrice: 0.0 }
      ]
    }, ...
  ]
  data.sellPrice = 26100   ← 기본 판매가
"""

import re
import httpx

from .base import BaseScraper, ProductInfo, ProductOption


class CM29Scraper(BaseScraper):
    SITE_NAME = "29cm"

    _URL_PATTERNS = [
        r'29cm\.co\.kr/products/(\d+)',
        r'29cm\.co\.kr/catalog/(\d+)',
        r'29cm\.co\.kr/product/detail\?itemNo=(\d+)',
    ]

    def _extract_id(self, url: str) -> str:
        for pat in self._URL_PATTERNS:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        raise ValueError(f"29cm URL에서 상품 ID를 추출할 수 없습니다: {url}")

    def _normalize_url(self, url: str) -> str:
        return f"https://www.29cm.co.kr/products/{self._extract_id(url)}"

    def _headers(self) -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": "https://www.29cm.co.kr/",
            "Origin": "https://www.29cm.co.kr",
        }

    async def scrape(self, url: str) -> ProductInfo:
        pid = self._extract_id(url)
        normalized = self._normalize_url(url)

        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0, headers=self._headers()
        ) as client:
            resp = await client.get(
                f"https://bff-api.29cm.co.kr/api/v5/product-detail/{pid}"
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"29cm API 오류 (status={resp.status_code})\n"
                    f"확인: http://localhost:8002/api/debug-json-29cm?pid={pid}"
                )
            raw = resp.json()

        # 응답 구조: {"result":"SUCCESS", "data": { 상품데이터 }}
        d = raw.get("data", raw)

        product_name = d.get("itemName") or f"29cm #{pid}"
        base_price   = int(d.get("sellPrice") or d.get("consumerPrice") or 0)

        options = self._parse_option_items(d, base_price)

        if not options:
            raise RuntimeError(
                f"29cm 옵션 파싱 실패 (ID: {pid})\n"
                f"확인: http://localhost:8002/api/debug-json-29cm?pid={pid}"
            )

        return ProductInfo(
            name=product_name,
            url=normalized,
            site=self.SITE_NAME,
            options=options,
        )

    # ── 옵션 파서 ─────────────────────────────────────────────────────────────

    def _parse_option_items(self, d: dict, base_price: int) -> list[ProductOption]:
        """
        data.optionItems.list 의 중첩 구조를 파싱.

        layout = ["COLOR", "SIZE"] → 2단계 (색상 > 사이즈)
        layout = ["SIZE"]          → 1단계 (사이즈만)
        layout = ["COLOR"]         → 1단계 (색상만)
        """
        oi = d.get("optionItems")
        if not isinstance(oi, dict):
            return []

        layout     = oi.get("layout", [])
        outer_list = oi.get("list", [])
        if not outer_list:
            return []

        options = []
        two_level = len(layout) >= 2  # COLOR > SIZE 형태

        for outer in outer_list:
            if not isinstance(outer, dict):
                continue

            outer_title = outer.get("title", "")
            inner_list  = outer.get("list") or []

            if two_level and inner_list:
                # 2단계: outer = 색상, inner = 사이즈
                for inner in inner_list:
                    if not isinstance(inner, dict):
                        continue
                    opt = self._make_option(
                        color=outer_title,
                        size=inner.get("title", ""),
                        item=inner,
                        base_price=base_price,
                    )
                    if opt:
                        options.append(opt)
            else:
                # 1단계: outer 자체가 최종 옵션
                # layout[0]이 SIZE면 사이즈, COLOR면 색상
                first_dim = layout[0] if layout else "SIZE"
                color = outer_title if first_dim == "COLOR" else ""
                size  = outer_title if first_dim != "COLOR" else ""
                opt = self._make_option(
                    color=color, size=size,
                    item=outer, base_price=base_price,
                )
                if opt:
                    options.append(opt)

        return options

    def _make_option(
        self, color: str, size: str, item: dict, base_price: int
    ) -> ProductOption | None:
        if not color and not size:
            return None

        # 재고
        limited_qty = item.get("limitedQty")
        try:
            stock = int(limited_qty) if limited_qty is not None else -1
        except (ValueError, TypeError):
            stock = -1

        # 품절 여부
        status  = item.get("frontOptionStockStatus", "")
        soldout = (status == "SOLD_OUT") or (stock == 0)
        if soldout:
            stock = 0

        # 가격 (extra price가 0이면 base_price 그대로)
        extra = item.get("sellPrice") or 0
        try:
            price = base_price + int(extra)
        except (ValueError, TypeError):
            price = base_price

        return ProductOption(
            color=color,
            size=size,
            stock=stock,
            price=price,
            soldout=soldout,
            option_id=str(item.get("optionNo", "")),
        )
