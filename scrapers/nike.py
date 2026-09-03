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
    # styleColor만 URL 끝에서 추출 (fallback용).
    # Nike styleColor 형식은 영문 2자 + 숫자 → 하이픈 → 숫자 (예: IB1873-702).
    # 느슨하게 잡으면 "pegasus-hzTwdMlw" 같은 슬러그도 색상코드로 오인한다.
    _STYLE_RE = re.compile(
        r"/([A-Z]{2}[0-9]{3,5}-[0-9]{2,4})(?:\?.*)?$", re.IGNORECASE
    )
    # styleColor 없는 정규 URL: /kr/t/{slug}-{groupKey}
    # (상품 페이지의 canonical 형태. 단종 색상 URL도 여기로 리다이렉트된다.)
    _GROUP_ONLY_RE = re.compile(
        r"nike\.com/kr/t/(?:[^/?#]*-)?([A-Za-z0-9]{8})(?:[/?#]|$)"
    )

    def _extract_ids(self, url: str) -> tuple[str | None, str]:
        """(groupKey|None, styleColor) 추출.

        styleColor는 빈 문자열일 수 있다(= URL에 색상 지정 없음 → 전 색상 조회).
        groupKey가 None이면 상품 페이지 HTML에서 추출한다.
        """
        m = self._URL_RE.search(url)
        if m:
            return m.group(1), m.group(2).upper()
        # styleColor로 끝나는 URL (groupKey는 HTML에서 추출)
        m2 = self._STYLE_RE.search(url)
        if m2:
            return None, m2.group(1).upper()
        # canonical 형태: 색상 코드 없이 groupKey로만 끝나는 URL
        m3 = self._GROUP_ONLY_RE.search(url)
        if m3:
            return m3.group(1), ""
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
                # groupKey를 HTML에서 먼저 가져와야 하므로 병렬 불가 → 순차 처리
                name, variants, group_key = await self._fetch_page_info(client, url)
                if not group_key:
                    raise RuntimeError(
                        f"Nike 상품 페이지를 열 수 없거나 groupKey가 없습니다. "
                        f"단종되었거나 URL이 오래되었을 수 있습니다. (styleColor={style_color})"
                    )
                sizes = await self._fetch_availability(client, group_key)
            else:
                page_task  = asyncio.create_task(self._fetch_page_info(client, url))
                avail_task = asyncio.create_task(self._fetch_availability(client, group_key))
                (name, variants, _), sizes = await asyncio.gather(page_task, avail_task)

        if not sizes:
            raise RuntimeError(
                f"Nike 재고 API가 사이즈 정보를 반환하지 않았습니다. "
                f"(groupKey={group_key})"
            )

        live_codes = sorted({s.get("productCode") for s in sizes if s.get("productCode")})

        if style_color:
            picked = [s for s in sizes if s.get("productCode") == style_color]
            if not picked:
                # 색상 단종 시 Nike는 색상 없는 canonical URL로 리다이렉트하고
                # 재고 API에서도 해당 productCode가 사라진다.
                raise RuntimeError(
                    f"Nike: 색상 코드 {style_color} 는 현재 판매하지 않습니다 (단종/코드 변경).\n"
                    f"현재 판매 중인 색상: {', '.join(live_codes)}\n"
                    f"색상 코드를 뺀 URL(…/kr/t/…-{group_key})로 조회하면 전 색상을 볼 수 있습니다."
                )
        else:
            # 색상 지정이 없으면 전 색상 조회.
            # 재고 API에는 연관 상품 색상까지 섞여 오므로 ProductGroup에 있는 것만 남긴다.
            picked = [s for s in sizes if s.get("productCode") in variants] if variants else sizes
            picked = picked or sizes

        options = []
        for sz in picked:
            code    = sz.get("productCode", "")
            variant = variants.get(code, {})
            avail   = sz.get("availability", {})
            soldout = not avail.get("isAvailable", False)
            ship    = avail.get("ship", "")   # OOS / LOW / MEDIUM / HIGH

            # 같은 사이즈가 일반/와이드로 중복될 수 있어 groupingLabel로 구분
            size_label = sz.get("localizedLabel", "")
            grouping   = sz.get("groupingLabel", "")
            if grouping:
                size_label = f"{size_label} ({grouping})"

            options.append(ProductOption(
                color=variant.get("color") or code or style_color,
                size=size_label,
                stock=0 if soldout else -1,
                price=variant.get("price", 0),
                soldout=soldout,
                option_id=sz.get("gtin", ""),
                stock_level="" if soldout else ship,
            ))

        return ProductInfo(
            name=name or f"Nike {style_color or group_key}",
            url=url,
            site=self.SITE_NAME,
            options=options,
        )

    # ── 상품 페이지 HTML → JSON-LD 파싱 ──────────────────────────────────────

    async def _fetch_page_info(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, dict[str, dict], str]:
        """(상품명, {styleColor: {color, price}}, groupKey) 반환.

        ProductGroup JSON-LD에는 이 상품의 전 색상이 들어있으므로
        색상 코드 → 색상명/가격 매핑을 통째로 만들어 둔다.
        """
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
                return "", {}, ""

            html = resp.content.decode('utf-8', errors='replace')

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
                            variants: dict[str, dict] = {}
                            for variant in item.get("hasVariant", []):
                                mpn = variant.get("mpn")
                                if not mpn:
                                    continue
                                try:
                                    price = int(variant.get("offers", {}).get("price", 0))
                                except (TypeError, ValueError):
                                    price = 0
                                variants[mpn] = {
                                    "color": variant.get("color", ""),
                                    "price": price,
                                }
                            return name, variants, found_group_key
                except Exception:
                    continue
        except Exception:
            pass
        return "", {}, ""

    # ── 재고 API ─────────────────────────────────────────────────────────────

    async def _fetch_availability(
        self, client: httpx.AsyncClient, group_key: str
    ) -> list[dict]:
        """이 groupKey의 전 색상 × 전 사이즈 재고 상태 반환 (필터링은 호출부에서)."""
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
            return resp.json().get("sizes", [])
        except Exception:
            return []
