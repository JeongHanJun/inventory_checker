"""
Nike Korea 스크래퍼 — httpx 전용 (Playwright 불필요)

확인된 API 구조:
  상품 페이지 HTML:
    www.nike.com/kr/t/{slug}-{groupKey}/{styleColor}
    → <script type="application/ld+json"> ProductGroup
      .hasVariant[].color          (색상명, 현재 색상만 포함)
      .hasVariant[].size           (사이즈)
      .hasVariant[].offers.price   (가격 KRW)
      .hasVariant[].mpn            (styleColor 코드)
    ※ JSON-LD에는 InStock 사이즈만 포함됨

  재고 API:
    api.nike.com/discover/product_details_availability/v1/
      marketplace/KR/language/ko/
      consumerChannelId/{CHANNEL_ID}/groupKey/{groupKey}
    → .sizes[].localizedLabel    (사이즈명)
    → .sizes[].productCode       (styleColor)
    → .sizes[].availability.isAvailable (bool)
    → .sizes[].availability.ship (OOS / LOW / MEDIUM / HIGH)
    ※ 모든 색상의 모든 사이즈 포함 → productCode로 필터링 필요
"""

import re
import json
import httpx

from .base import BaseScraper, ProductInfo, ProductOption


class NikeScraper(BaseScraper):
    SITE_NAME = "Nike"

    _CHANNEL_ID = "d9a5bc42-4b9c-4976-858a-f159cf99c647"

    # URL 패턴: /kr/t/{slug}-{groupKey}/{styleColor}
    # groupKey: 8자리 영숫자 (URL에 없으면 HTML에서 추출), styleColor: 예) IF0756-323
    _URL_RE = re.compile(
        r"nike\.com/kr/t/[^/]+-([A-Za-z0-9]{8})/([A-Z0-9]+-[A-Z0-9]+)",
        re.IGNORECASE,
    )
    # styleColor만 URL 끝에서 추출 (fallback용)
    _STYLE_RE = re.compile(r"/([A-Z0-9]+-[A-Z0-9]+)(?:\?.*)?$", re.IGNORECASE)

    def _extract_ids(self, url: str) -> tuple[str | None, str]:
        """(groupKey|None, styleColor) 추출. groupKey는 HTML에서 추출해야 할 수 있음."""
        m = self._URL_RE.search(url)
        if m:
            return m.group(1), m.group(2)
        # Korean-encoded URL 등 groupKey가 URL에 없는 경우
        m2 = self._STYLE_RE.search(url)
        if m2:
            return None, m2.group(1).upper()
        raise ValueError(f"Nike URL에서 상품 ID를 추출할 수 없습니다: {url}")

    def _headers(self) -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9",
        }

    async def scrape(self, url: str) -> ProductInfo:
        group_key, style_color = self._extract_ids(url)

        import asyncio

        async with httpx.AsyncClient(
            headers=self._headers(), timeout=15.0, follow_redirects=True
        ) as client:
            if group_key is None:
                # groupKey를 HTML에서 먼저 가져온 후 병렬 불가 → 순차 처리
                name, color, price, group_key = await self._fetch_page_info(client, url, style_color)
                if not group_key:
                    raise RuntimeError(
                        f"Nike 상품 페이지에서 groupKey를 찾을 수 없습니다. (styleColor={style_color})"
                    )
                sizes = await self._fetch_availability(client, group_key, style_color)
            else:
                # 병렬 조회
                page_task = asyncio.create_task(self._fetch_page_info(client, url, style_color))
                avail_task = asyncio.create_task(self._fetch_availability(client, group_key, style_color))
                page_result, sizes = await asyncio.gather(page_task, avail_task)
                name, color, price, _ = page_result

        if not sizes:
            raise RuntimeError(
                f"Nike 재고 정보를 가져올 수 없습니다. "
                f"(groupKey={group_key}, styleColor={style_color})"
            )

        options = []
        for sz in sizes:
            avail = sz["availability"]
            soldout = not avail["isAvailable"]
            ship = avail.get("ship", "")   # OOS / LOW / MEDIUM / HIGH
            options.append(ProductOption(
                color=color or style_color,
                size=sz["localizedLabel"],
                stock=0 if soldout else -1,
                price=price,
                soldout=soldout,
                option_id=sz.get("gtin", ""),
                stock_level="" if soldout else ship,
            ))

        return ProductInfo(
            name=name or f"Nike {style_color}",
            url=url,
            site=self.SITE_NAME,
            options=options,
        )

    # ── 상품 페이지 HTML → JSON-LD 파싱 ──────────────────────────────────────

    async def _fetch_page_info(
        self, client: httpx.AsyncClient, url: str, style_color: str
    ) -> tuple[str, str, int, str]:
        """(상품명, 색상명, 가격, groupKey) 반환"""
        try:
            resp = await client.get(
                url,
                headers={
                    **self._headers(),
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                },
            )
            if resp.status_code != 200:
                return "", "", 0, ""

            html = resp.text

            # groupKey를 HTML에서 추출 (URL에 없는 경우 대비)
            gk_match = re.search(r'"groupKey"\s*:\s*"([A-Za-z0-9]{6,12})"', html)
            found_group_key = gk_match.group(1) if gk_match else ""

            scripts = re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html,
                re.DOTALL,
            )
            for s in scripts:
                try:
                    ld = json.loads(s)
                    items = ld if isinstance(ld, list) else [ld]
                    for item in items:
                        if item.get("@type") == "ProductGroup":
                            name = item.get("name", "")
                            # 현재 styleColor의 variant에서 색상/가격 추출
                            for variant in item.get("hasVariant", []):
                                if variant.get("mpn") == style_color:
                                    color = variant.get("color", "")
                                    price = int(
                                        variant.get("offers", {}).get("price", 0)
                                    )
                                    return name, color, price, found_group_key
                            # variant가 없으면 (전 사이즈 품절) 이름만 반환
                            return name, "", 0, found_group_key
                except Exception:
                    continue
        except Exception:
            pass
        return "", "", 0, ""

    # ── 재고 API ─────────────────────────────────────────────────────────────

    async def _fetch_availability(
        self, client: httpx.AsyncClient, group_key: str, style_color: str
    ) -> list[dict]:
        """해당 styleColor의 모든 사이즈 + 재고 상태 반환"""
        url = (
            f"https://api.nike.com/discover/product_details_availability/v1"
            f"/marketplace/KR/language/ko"
            f"/consumerChannelId/{self._CHANNEL_ID}"
            f"/groupKey/{group_key}"
        )
        try:
            resp = await client.get(
                url,
                headers={
                    **self._headers(),
                    "Accept": "application/json",
                    "nike-api-caller-id": "com.nike.commerce.nikedotcom.web",
                    "Origin": "https://www.nike.com",
                    "Referer": "https://www.nike.com/",
                },
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [
                s for s in data.get("sizes", [])
                if s.get("productCode") == style_color
            ]
        except Exception:
            return []
