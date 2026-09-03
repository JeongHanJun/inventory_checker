"""
Musinsa 스크래퍼 - 직접 API 호출 (~2-5초), curl_cffi 사용 (Cloudflare WAF 우회)

2026-09 기준 goods-detail.musinsa.com 이 Cloudflare 봇 차단을 켜서
httpx(Python OpenSSL TLS 지문)로 보내면 모든 요청이 403 HTML을 받는다.
29cm(cm29.py)와 동일하게 curl_cffi Chrome impersonation으로 우회한다.

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

from curl_cffi.requests import AsyncSession

from .base import BaseScraper, ProductInfo, ProductOption


class _StockUnknown(Exception):
    """재고 확인 API가 응답하지 않아 수량을 판단할 수 없음.

    _fill_stock_counts 가 이 예외를 만나면 해당 옵션을 건드리지 않고
    activated 기반 초기값(재고있음/품절)을 그대로 남긴다.
    """


class MusinsaScraper(BaseScraper):
    SITE_NAME = "Musinsa"

    _URL_PATTERNS = [
        r'musinsa\.com/products/(\d+)',
        r'musinsa\.com/app/goods/(\d+)',
        r'musinsa\.com/goods/(\d+)',
    ]
    _OPT_KIND_CODES = ["CLOTHES", "SHOES", "BAG", "ACC", ""]
    _STOCK_MAX_QTY  = 999   # 이 이상이면 -1(수량 미표시)로 처리
    _IMPERSONATE    = "chrome124"   # cm29.py와 동일. 차단되면 최신 버전으로 올릴 것

    def _extract_id(self, url: str) -> str:
        for pat in self._URL_PATTERNS:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        raise ValueError(f"Musinsa URL에서 상품 ID를 추출할 수 없습니다: {url}")

    def _normalize_url(self, url: str) -> str:
        return f"https://www.musinsa.com/products/{self._extract_id(url)}"

    def _headers(self, pid: str) -> dict:
        # User-Agent는 curl_cffi impersonate가 TLS 지문과 일치하게 자동 설정한다.
        # 직접 지정하면 지문/UA 불일치로 오히려 Cloudflare에 걸린다.
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": f"https://www.musinsa.com/products/{pid}",
            "Origin": "https://www.musinsa.com",
        }

    async def scrape(self, url: str) -> ProductInfo:
        pid = self._extract_id(url)
        normalized = self._normalize_url(url)

        # curl_cffi: Chrome TLS/HTTP2 지문을 흉내내 Cloudflare 봇 차단을 통과한다.
        # max_clients: 재고 바이너리 서치가 옵션당 수십 개 요청을 동시에 날린다.
        async with AsyncSession(
            impersonate=self._IMPERSONATE, timeout=15.0,
            headers=self._headers(pid), max_clients=30,
        ) as client:
            product_name, base_price, sale_type = await self._fetch_goods_info(client, pid)
            options, statuses = await self._fetch_options(
                client, pid, base_price, run_stock_check=(sale_type == "SALE")
            )

        if not options:
            if statuses and all(s in (403, 429) for s in statuses):
                raise RuntimeError(
                    f"무신사 봇 차단(HTTP {statuses[0]}) — 옵션 API 접근이 거부되었습니다.\n"
                    f"curl_cffi impersonate 버전을 최신 Chrome으로 올려야 할 수 있습니다."
                )
            raise RuntimeError(
                f"옵션 파싱 실패 (옵션 API 응답: {statuses or '요청 실패'}).\n"
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
        self, client: AsyncSession, pid: str
    ) -> tuple[str, int, str]:
        """(상품명, 가격, goodsSaleType) 반환"""
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
                sale_type = d.get("goodsSaleType", "SALE")
                return str(name), int(price), sale_type
        except Exception:
            pass
        return f"Musinsa #{pid}", 0, "SALE"

    # ── 옵션 조회 ─────────────────────────────────────────────────────────────

    async def _fetch_options(
        self, client: AsyncSession, pid: str, base_price: int,
        run_stock_check: bool = True,
    ) -> tuple[list[ProductOption], list[int]]:
        """(옵션 목록, 시도한 요청들의 status code 목록) 반환.

        status code는 실패 원인(차단 403 vs 진짜 파싱 실패)을 구분하기 위해 수집한다.
        """
        statuses: list[int] = []
        for kind in self._OPT_KIND_CODES:
            params = "goodsSaleType=SALE"
            if kind:
                params += f"&optKindCd={kind}"
            url = f"https://goods-detail.musinsa.com/api2/goods/{pid}/options?{params}"
            try:
                resp = await client.get(url)
                statuses.append(resp.status_code)
                if resp.status_code != 200:
                    continue
                options = self._parse_option_items(resp.json(), base_price)
                if options:
                    if run_stock_check:
                        # activated=True인 옵션에 대해 정확한 재고 수량 조회
                        await self._fill_stock_counts(client, pid, options)
                    else:
                        # STOP_SALE 등 판매 중지 상태 → 전체 품절
                        for opt in options:
                            opt.stock = 0
                            opt.soldout = True
                    return options, statuses
            except Exception:
                continue
        return [], statuses

    # ── 재고 수량 바이너리 서치 ───────────────────────────────────────────────

    _FULFILLMENT_IDS = [2, 1, 3]   # 시도 순서: 2가 가장 널리 쓰임

    async def _fill_stock_counts(
        self, client: AsyncSession, pid: str, options: list[ProductOption]
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

        # 하나라도 양수 재고가 확인되면 API가 정상 작동 중인 것으로 판단
        api_working = any(isinstance(r, int) and r != 0 for r in results)

        for (i, _), result in zip(active, results):
            if not isinstance(result, int):
                continue
            if api_working:
                # API 정상: 0은 진짜 품절, 양수는 정확한 수량
                options[i].stock   = result
                options[i].soldout = (result == 0)
            elif result != 0:
                # API 비정상이지만 이 옵션은 수량 확인됨
                options[i].stock   = result
                options[i].soldout = False
            # api_working=False이고 result=0이면 업데이트 안 함
            # (모든 옵션이 0 → API 자체가 이 상품 미지원, activated 유지)

    async def _probe_fulfillment_id(
        self, client: AsyncSession, pid: str, option_item_no: int
    ) -> int:
        """재고가 있다고 응답하는 첫 번째 fulfillmentCenterId 반환 (병렬 프로브)."""
        results = await asyncio.gather(*[
            self._check_out_of_stock(client, pid, option_item_no, 1, fid)
            for fid in self._FULFILLMENT_IDS
        ])
        for fid, oos in zip(self._FULFILLMENT_IDS, results):
            if oos is False:      # None(판단 불가)은 채택하지 않음
                return fid
        return self._FULFILLMENT_IDS[0]   # 모두 품절/불가면 기본값

    # 병렬 exponential probe로 초기 재고 범위 확정 (log 스케일)
    _PROBE_QUANTITIES = [2, 5, 10, 20, 50, 100, 200, 500, 1000]

    async def _binary_search_stock(
        self, client: AsyncSession, pid: str, option_item_no: int, fid: int
    ) -> int:
        """
        check-available-stock POST API로 재고 수량 반환.
          0   → 품절 (모든 fid가 OOS 반환)
          N   → 정확한 재고 수량 (1 ≤ N ≤ 999)
          -1  → _STOCK_MAX_QTY 초과 (수량 미표시)
          _StockUnknown 예외 → API가 답을 주지 않아 판단 불가

        전략:
          1) quantity=1로 재고 유무 확인 (fid 폴백 포함)
          2) exponential probe 병렬 호출 → 대략 범위 확정
          3) 좁은 범위에서만 짧은 binary search로 정확한 값 확정
        """
        # ── 1) 최소 재고(1) 확인 및 fid 폴백 ──────────────────────────
        first = await self._check_out_of_stock(client, pid, option_item_no, 1, fid)
        if first is None:
            raise _StockUnknown(option_item_no)
        if first:
            all_candidates = [f for f in range(1, 16) if f != fid] + [None]
            probe_results = await asyncio.gather(
                *[self._check_out_of_stock(client, pid, option_item_no, 1, c)
                  for c in all_candidates]
            )
            if all(r is None for r in probe_results):
                raise _StockUnknown(option_item_no)
            alt = next(
                (c for c, oos in zip(all_candidates, probe_results) if oos is False),
                "NOT_FOUND",
            )
            if alt == "NOT_FOUND":
                return 0
            fid = alt

        # ── 2) 병렬 exponential probe: 여러 quantity를 한 번에 확인 ──
        probe_results = await asyncio.gather(
            *[self._check_out_of_stock(client, pid, option_item_no, q, fid)
              for q in self._PROBE_QUANTITIES]
        )
        if all(r is None for r in probe_results):
            raise _StockUnknown(option_item_no)
        max_ok = 1                       # quantity=1은 위에서 통과됨
        min_oos = self._STOCK_MAX_QTY + 1
        for q, oos in zip(self._PROBE_QUANTITIES, probe_results):
            if oos is None:              # 판단 불가한 프로브는 범위 계산에서 제외
                continue
            if oos:
                if q < min_oos:
                    min_oos = q
            else:
                if q > max_ok:
                    max_ok = q

        # quantity=1000이 OK → 재고 999 이상 → -1
        if max_ok > self._STOCK_MAX_QTY:
            return -1

        # ── 3) 좁은 범위에서 짧은 binary search로 정확한 값 확정 ────
        lo, hi = max_ok, min_oos - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            oos = await self._check_out_of_stock(client, pid, option_item_no, mid, fid)
            if oos is None:
                # 여기서 멈추면 lo까지는 확인된 값이므로 하한값을 반환한다.
                break
            if oos:
                hi = mid - 1
            else:
                lo = mid
        return lo

    async def _check_out_of_stock(
        self, client: AsyncSession, pid: str, option_item_no: int,
        quantity: int, fid: int | None = 2
    ) -> bool | None:
        """quantity 만큼 구매 가능한지 확인.
          True  = 재고 부족
          False = 구매 가능
          None  = 판단 불가 (차단·타임아웃 등 API가 답을 주지 않음)

        None을 False(구매 가능)로 뭉개면 차단당했을 때 품절 상품이
        "재고있음"으로 표시되므로 반드시 구분한다.
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
                return None
            return resp.json().get("data", {}).get("isOutOfStock", False)
        except Exception:
            return None

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
