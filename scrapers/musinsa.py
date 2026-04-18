"""
Musinsa 스크래퍼 - 직접 API 호출 (~2-5초)

확인된 API 구조:
  goods_info:   goods-detail.musinsa.com/api2/goods/{id}
                → data.goodsPrice.salePrice  (기본 판매가)

  options:      goods-detail.musinsa.com/api2/goods/{id}/options?goodsSaleType=SALE&optKindCd=CLOTHES
                → data.optionItems[]
                    .optionValues[].name        (사이즈/색상 값)
                    .optionValues[].optionName  (그룹명: "사이즈", "색상")
                    .price                      (옵션 추가금액, 보통 0)
                    .activated                  (true=재고있음, false=품절)
                    .isDeleted                  (삭제된 옵션)

  재고 수량:    goods-detail.musinsa.com/api2/goods/{id}/options/check-available-stock
                POST body: {fulfillmentCenterId: 1, optionItemNo: N, quantity: Q}
                → data.isOutOfStock (bool)
                → 바이너리 서치로 정확한 재고 수량 산출
"""

import asyncio
import re
import httpx

from .base import BaseScraper, ProductInfo, ProductOption


class MusinsaScraper(BaseScraper):
    SITE_NAME = "Musinsa"

    _URL_PATTERNS = [
        r'musinsa\.com/products/(\d+)',
        r'musinsa\.com/app/goods/(\d+)',
        r'musinsa\.com/goods/(\d+)',
    ]
    _OPT_KIND_CODES = ["CLOTHES", "SHOES", "BAG", "ACC", ""]
    _STOCK_MAX_QTY  = 999   # 이 이상이면 -1(수량 미표시)로 처리

    def _extract_id(self, url: str) -> str:
        for pat in self._URL_PATTERNS:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        raise ValueError(f"Musinsa URL에서 상품 ID를 추출할 수 없습니다: {url}")

    def _normalize_url(self, url: str) -> str:
        return f"https://www.musinsa.com/products/{self._extract_id(url)}"

    def _headers(self, pid: str) -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": f"https://www.musinsa.com/products/{pid}",
            "Origin": "https://www.musinsa.com",
        }

    async def scrape(self, url: str) -> ProductInfo:
        pid = self._extract_id(url)
        normalized = self._normalize_url(url)

        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0, headers=self._headers(pid)
        ) as client:
            product_name, base_price = await self._fetch_goods_info(client, pid)
            options = await self._fetch_options(client, pid, base_price)

        if not options:
            raise RuntimeError(
                f"옵션 파싱 실패.\n"
                f"확인 방법: http://localhost:8002/api/debug-json?pid={pid} 를 브라우저에서 열어주세요."
            )

        return ProductInfo(
            name=product_name,
            url=normalized,
            site=self.SITE_NAME,
            options=options,
        )

    # ── 상품 기본 정보 ────────────────────────────────────────────────────────

    async def _fetch_goods_info(
        self, client: httpx.AsyncClient, pid: str
    ) -> tuple[str, int]:
        try:
            resp = await client.get(
                f"https://goods-detail.musinsa.com/api2/goods/{pid}"
            )
            if resp.status_code == 200:
                d = resp.json().get("data", {})
                name = (
                    d.get("goodsNm")
                    or d.get("goodsName")
                    or d.get("name")
                    or f"Musinsa #{pid}"
                )
                price = (
                    d.get("goodsPrice", {}).get("salePrice")
                    or d.get("goodsPrice", {}).get("normalPrice")
                    or 0
                )
                return str(name), int(price)
        except Exception:
            pass
        return f"Musinsa #{pid}", 0

    # ── 옵션 조회 ─────────────────────────────────────────────────────────────

    async def _fetch_options(
        self, client: httpx.AsyncClient, pid: str, base_price: int
    ) -> list[ProductOption]:
        for kind in self._OPT_KIND_CODES:
            params = "goodsSaleType=SALE"
            if kind:
                params += f"&optKindCd={kind}"
            url = f"https://goods-detail.musinsa.com/api2/goods/{pid}/options?{params}"
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                options = self._parse_option_items(resp.json(), base_price)
                if options:
                    # activated=True인 옵션에 대해 정확한 재고 수량 조회
                    await self._fill_stock_counts(client, pid, options)
                    return options
            except Exception:
                continue
        return []

    # ── 재고 수량 바이너리 서치 ───────────────────────────────────────────────

    _FULFILLMENT_IDS = [2, 1, 3]   # 시도 순서: 2가 가장 널리 쓰임

    async def _fill_stock_counts(
        self, client: httpx.AsyncClient, pid: str, options: list[ProductOption]
    ) -> None:
        """activated=True인 옵션들의 재고 수량을 병렬 바이너리 서치로 채움."""
        active = [(i, opt) for i, opt in enumerate(options)
                  if not opt.soldout and opt.option_id]
        if not active:
            return

        # 첫 번째 활성 옵션으로 fulfillmentCenterId 탐색
        first_option_id = int(active[0][1].option_id)
        fid = await self._probe_fulfillment_id(client, pid, first_option_id)

        tasks   = [self._binary_search_stock(client, pid, int(opt.option_id), fid)
                   for _, opt in active]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (i, _), result in zip(active, results):
            if isinstance(result, int):
                options[i].stock   = result
                options[i].soldout = (result == 0)

    async def _probe_fulfillment_id(
        self, client: httpx.AsyncClient, pid: str, option_item_no: int
    ) -> int:
        """재고가 있다고 응답하는 첫 번째 fulfillmentCenterId 반환."""
        for fid in self._FULFILLMENT_IDS:
            oos = await self._check_out_of_stock(client, pid, option_item_no, 1, fid)
            if not oos:        # 재고 있음 → 이 센터 ID 사용
                return fid
        return self._FULFILLMENT_IDS[0]   # 모두 품절이면 기본값

    async def _binary_search_stock(
        self, client: httpx.AsyncClient, pid: str, option_item_no: int, fid: int
    ) -> int:
        """
        check-available-stock POST API를 이용해 바이너리 서치로 재고 수량 반환.
          0   → 품절 (모든 fid가 OOS 반환)
          N   → 정확한 재고 수량
          -1  → _STOCK_MAX_QTY 초과
        """
        if await self._check_out_of_stock(client, pid, option_item_no, 1, fid):
            # primary fid가 OOS → 이 옵션에 맞는 대안 fid 탐색
            # (옵션마다 물류센터가 다를 수 있으므로 옵션 단위로 재시도)
            alt = None
            for candidate in self._FULFILLMENT_IDS:
                if candidate == fid:
                    continue
                if not await self._check_out_of_stock(client, pid, option_item_no, 1, candidate):
                    alt = candidate
                    break
            if alt is None:
                # 마지막 수단: fid 없이 시도 (MFS 다중재고 상품)
                if not await self._check_out_of_stock(client, pid, option_item_no, 1, None):
                    fid = None
                else:
                    return 0  # 모든 방법이 OOS → 품절
            else:
                fid = alt
        if not await self._check_out_of_stock(client, pid, option_item_no, self._STOCK_MAX_QTY + 1, fid):
            return -1

        lo, hi = 1, self._STOCK_MAX_QTY
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if await self._check_out_of_stock(client, pid, option_item_no, mid, fid):
                hi = mid - 1
            else:
                lo = mid
        return lo

    async def _check_out_of_stock(
        self, client: httpx.AsyncClient, pid: str, option_item_no: int,
        quantity: int, fid: int | None = 2
    ) -> bool:
        """quantity 만큼 구매 가능한지 확인. True=재고 부족.
        fid=None: fulfillmentCenterId 생략 (MFS 다중재고 상품용)."""
        body: dict = {"optionItemNo": option_item_no, "quantity": quantity}
        if fid is not None:
            body["fulfillmentCenterId"] = fid
        try:
            resp = await client.post(
                f"https://goods-detail.musinsa.com/api2/goods/{pid}/options/check-available-stock",
                json=body,
            )
            if resp.status_code != 200:
                return False
            return resp.json().get("data", {}).get("isOutOfStock", False)
        except Exception:
            return False

    # ── 파서 ─────────────────────────────────────────────────────────────────

    def _parse_option_items(self, raw: dict, base_price: int) -> list[ProductOption]:
        d = raw.get("data", raw)
        option_items = d.get("optionItems")

        if not option_items or not isinstance(option_items, list):
            return []

        options = []
        for item in option_items:
            if not isinstance(item, dict):
                continue
            if item.get("isDeleted", False):
                continue

            color = ""
            size  = ""

            for ov in item.get("optionValues", []):
                group_name = (ov.get("optionName") or "").lower()
                value      = ov.get("name", "")

                if "색상" in group_name or "color" in group_name or "컬러" in group_name:
                    color = value
                elif "사이즈" in group_name or "size" in group_name:
                    size = value
                else:
                    if not color:
                        color = value
                    elif not size:
                        size = value

            activated   = item.get("activated", True)
            extra_price = item.get("price") or 0
            price       = base_price + int(extra_price)

            # 초기값: activated 기반 (이후 _fill_stock_counts에서 정확한 수량으로 교체됨)
            stock   = -1 if activated else 0
            soldout = not activated

            if color or size:
                options.append(ProductOption(
                    color=color,
                    size=size or "단일",   # 사이즈 없는 단일 옵션 상품
                    stock=stock,
                    price=price,
                    soldout=soldout,
                    option_id=str(item.get("no", "")),
                ))

        return options
