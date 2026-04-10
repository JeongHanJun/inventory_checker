from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProductOption:
    color: str
    size: str
    stock: int          # 재고 수량 (-1 = 알 수 없음)
    price: int          # 가격 (원)
    soldout: bool = False
    option_id: str = ""
    stock_level: str = ""   # 수량 불명 시 수준 표시 (LOW / MEDIUM / HIGH)


@dataclass
class ProductInfo:
    name: str
    url: str
    site: str
    options: list[ProductOption] = field(default_factory=list)
    thumbnail: str = ""
    raw_debug: dict = field(default_factory=dict)  # 디버그용 원본 데이터


class BaseScraper(ABC):
    SITE_NAME: str = "Unknown"

    @abstractmethod
    async def scrape(self, url: str) -> ProductInfo:
        pass

    # ── 공통 유틸 ──────────────────────────────────────────────────────────────

    def _find_options_in_json(self, data, depth: int = 0) -> list[ProductOption]:
        """JSON 트리를 재귀 탐색해 옵션 배열처럼 보이는 곳을 찾아 파싱."""
        if depth > 20:
            return []

        if isinstance(data, list) and data and isinstance(data[0], dict):
            parsed = self._try_parse_option_array(data)
            if parsed:
                return parsed

        if isinstance(data, dict):
            for v in data.values():
                result = self._find_options_in_json(v, depth + 1)
                if result:
                    return result
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    result = self._find_options_in_json(item, depth + 1)
                    if result:
                        return result
        return []

    def _try_parse_option_array(self, arr: list) -> list[ProductOption]:
        """배열이 옵션 데이터처럼 보이는지 확인 후 파싱."""
        sample = arr[0]
        keys_lower = {k.lower() for k in sample.keys()}

        STOCK_KEYS  = {'stock', 'qty', 'quantity', 'stockcount', 'remainqty',
                       'stockqty', 'inventorycount', '재고', '수량', 'remain'}
        SIZE_KEYS   = {'size', 'sizename', 'option1', 'option1value', 'optionvalue',
                       'optionname', '사이즈', 'sizeid', 'optionsize'}
        COLOR_KEYS  = {'color', 'colorname', 'option2', 'option2value', 'colorcode',
                       '색상', 'colorid'}
        SOLDOUT_KEYS = {'soldout', 'issoldout', 'sold_out', 'isSoldOut', '품절'}
        PRICE_KEYS  = {'price', 'saleprice', 'discountprice', 'finalprice',
                       'salesprice', '가격', '판매가', 'amount'}

        has_stock_or_soldout = bool(keys_lower & (STOCK_KEYS | SOLDOUT_KEYS))
        has_identifier = bool(keys_lower & (SIZE_KEYS | COLOR_KEYS))

        if not (has_stock_or_soldout and has_identifier):
            return []

        options = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            kl = {k.lower(): k for k in item.keys()}

            color = self._get_first(item, kl, COLOR_KEYS, default="")
            size  = self._get_first(item, kl, SIZE_KEYS,  default="")

            # 재고
            stock = -1
            for sk in STOCK_KEYS:
                if sk in kl:
                    try:
                        stock = int(item[kl[sk]] or 0)
                    except (ValueError, TypeError):
                        stock = 0
                    break

            # 품절 플래그
            soldout = False
            for dk in SOLDOUT_KEYS:
                if dk in kl:
                    val = item[kl[dk]]
                    soldout = bool(val) if isinstance(val, bool) else str(val).lower() in ('true', '1', 'y')
                    if soldout:
                        stock = 0
                    break

            # 가격
            price = 0
            for pk in PRICE_KEYS:
                if pk in kl:
                    try:
                        price = int(str(item[kl[pk]] or 0).replace(',', ''))
                    except (ValueError, TypeError):
                        price = 0
                    if price > 0:
                        break

            if color or size:
                options.append(ProductOption(
                    color=str(color),
                    size=str(size),
                    stock=stock,
                    price=price,
                    soldout=soldout,
                ))

        return options if len(options) >= 1 else []

    @staticmethod
    def _get_first(item: dict, keys_lower: dict, candidates: set, default=""):
        for c in candidates:
            if c in keys_lower:
                v = item[keys_lower[c]]
                return v if v is not None else default
        return default
