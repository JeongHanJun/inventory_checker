"""
Arc'teryx Korea 스크래퍼 — httpx 전용 (Playwright 불필요, ~2-4초)

데이터 소스:
  HTML 내 Next.js RSC(React Server Components) 페이로드
  self.__next_f.push([1, "...JSON..."])  블록 중
  dehydrated React Query state에서 상품 데이터 추출

데이터 구조:
  state.queries[0].state.data.product:
    .name_en         (영문 상품명)
    .sell_price      (판매가)
    .options[0]      (level=1, label="Colour") → 색상 목록
      .values[].id            색상 옵션 ID
      .values[].value         색상명 (예: "BLACK", "FORAGE")
      .values[].sale_state    "ON" / "SOLDOUT"
    .options[1]      (level=2, label="Size") → 사이즈 + 재고
      .values[].value         사이즈명 (예: "XS", "S", "M", "One Size")
      .values[].parent_ids    [-1, 색상id] → 상위 색상과 매핑
      .values[].stock         재고 수량
      .values[].is_orderable  True=주문가능, False=품절

URL 패턴:
  https://arcteryx.co.kr/products/view/{pid}
  https://arcteryx.co.kr/products/view/{pid}?sc=0
"""

import json
import re

import httpx

from .base import BaseScraper, ProductInfo, ProductOption


class ArcterycScraper(BaseScraper):
    SITE_NAME = "Arc'teryx"

    _PID_RE = re.compile(r"arcteryx\.co\.kr/products/view/(\d+)")

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }

    def _extract_id(self, url: str) -> str:
        m = self._PID_RE.search(url)
        if not m:
            raise ValueError(f"Arc'teryx URL에서 상품 ID를 추출할 수 없습니다: {url}")
        return m.group(1)

    def _normalize_url(self, url: str) -> str:
        pid = self._extract_id(url)
        return f"https://arcteryx.co.kr/products/view/{pid}"

    async def scrape(self, url: str) -> ProductInfo:
        pid = self._extract_id(url)
        normalized = self._normalize_url(url)

        async with httpx.AsyncClient(
            headers=self._HEADERS, timeout=20.0, follow_redirects=True
        ) as client:
            resp = await client.get(normalized)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Arc'teryx 페이지 로드 실패 (status={resp.status_code}, pid={pid})"
                )
            html = resp.text

        product = self._extract_product_data(html, pid)
        if not product:
            raise RuntimeError(
                f"Arc'teryx 상품 데이터를 찾을 수 없습니다. (pid={pid})"
            )

        name     = product.get("name_en") or product.get("name") or f"Arc'teryx #{pid}"
        price    = int(product.get("sell_price") or product.get("retail_price") or 0)
        options  = self._parse_options(product, price)

        if not options:
            raise RuntimeError(
                f"Arc'teryx 옵션 파싱 실패 (pid={pid})"
            )

        return ProductInfo(
            name=name,
            url=normalized,
            site=self.SITE_NAME,
            options=options,
        )

    # ── RSC 페이로드 파싱 ──────────────────────────────────────────────────────

    def _extract_product_data(self, html: str, pid: str) -> dict | None:
        """
        Next.js RSC self.__next_f.push([1, "..."]) 블록에서
        dehydrated React Query state를 찾아 product 데이터 반환.
        """
        blocks = re.findall(
            r'self\.__next_f\.push\(\[(.*?)\]\)', html, re.DOTALL
        )
        decoder = json.JSONDecoder()

        for block in blocks:
            # 형식: 1,"<JSON-encoded RSC string>"
            m = re.match(r'^1,(.*)$', block, re.DOTALL)
            if not m:
                continue
            try:
                content_str = json.loads(m.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                continue

            # dehydrated React Query state 마커 탐색
            idx = content_str.find('{"state":{"mutations"')
            if idx < 0:
                continue

            try:
                obj, _ = decoder.raw_decode(content_str, idx)
                queries = obj.get("state", {}).get("queries", [])
                for q in queries:
                    data = q.get("state", {}).get("data", {})
                    product = data.get("product")
                    if isinstance(product, dict) and str(product.get("id")) == pid:
                        return product
            except (json.JSONDecodeError, ValueError, KeyError):
                continue

        return None

    # ── 옵션/재고 파서 ────────────────────────────────────────────────────────

    def _parse_options(self, product: dict, base_price: int) -> list[ProductOption]:
        options_raw = product.get("options", [])

        # options[0] = level 1 (색상), options[1] = level 2 (사이즈+재고)
        # 단일 레벨(사이즈만)인 경우도 처리
        color_map: dict[int, str] = {}   # id → 색상명
        size_level: list[dict]    = []
        color_level: list[dict]   = []

        for opt in options_raw:
            label = (opt.get("label") or "").lower()
            level = opt.get("level", 1)
            values = opt.get("values", [])

            if "colour" in label or "color" in label or level == 1:
                color_level = values
                for v in values:
                    color_map[v["id"]] = v.get("value", "")
            if "size" in label or level == 2:
                size_level = values

        result: list[ProductOption] = []

        if size_level:
            for sv in size_level:
                size       = sv.get("value", "")
                stock      = sv.get("stock")
                orderable  = sv.get("is_orderable", True)
                sale_state = sv.get("sale_state", "ON")
                sell_price = int(sv.get("sell_price") or base_price)

                # 색상 매핑: parent_ids 마지막 값이 색상 ID
                parent_ids = sv.get("parent_ids", [])
                color = ""
                if len(parent_ids) >= 2:
                    color_id = parent_ids[-1]
                    color = color_map.get(color_id, "")

                # 품절 판정
                if sale_state == "SOLDOUT" or not orderable:
                    soldout = True
                    stock_val = 0
                elif stock is not None:
                    soldout = stock == 0
                    stock_val = int(stock)
                else:
                    soldout = False
                    stock_val = -1

                result.append(ProductOption(
                    color=color,
                    size=size or "단일",
                    stock=stock_val,
                    price=sell_price,
                    soldout=soldout,
                    option_id=str(sv.get("id", "")),
                ))

        elif color_level:
            # 색상만 있고 사이즈 없는 경우 (드문 케이스)
            for cv in color_level:
                sale_state = cv.get("sale_state", "ON")
                soldout    = sale_state == "SOLDOUT"
                result.append(ProductOption(
                    color=cv.get("value", ""),
                    size="단일",
                    stock=0 if soldout else -1,
                    price=base_price,
                    soldout=soldout,
                    option_id=str(cv.get("id", "")),
                ))

        return result
