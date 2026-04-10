# 재고 조회기 (Inventory Checker) — Claude Code 인수인계 문서

## 프로젝트 개요
한국 이커머스 사이트(무신사, 29cm, Nike KR, Arc'teryx KR)의 상품 재고를 색상·사이즈별로 정확히 조회하는 웹앱.

- **백엔드**: FastAPI + Uvicorn (Python 3.12)
- **프론트엔드**: 정적 HTML + TailwindCSS CDN (`static/index.html`)
- **배포**: Render.com 무료 플랜 (자동 배포: GitHub push → Render auto-deploy)
- **소스**: `https://github.com/JeongHanJun/inventory_checker`

---

## 디렉토리 구조

```
inventory_checker/
├── main.py                  # FastAPI 앱, 라우팅, keep-alive
├── requirements.txt         # Python 의존성
├── runtime.txt              # python-3.12.3 (Render 버전 고정)
├── render.yaml              # Render 배포 설정
├── static/
│   └── index.html           # SPA 프론트엔드
└── scrapers/
    ├── base.py              # BaseScraper, ProductInfo, ProductOption 데이터클래스
    ├── musinsa.py           # 무신사 스크래퍼
    ├── cm29.py              # 29cm 스크래퍼
    ├── nike.py              # Nike Korea 스크래퍼
    ├── arcteryx.py          # Arc'teryx Korea 스크래퍼
    └── generic.py           # 범용 스크래퍼 (fallback)
```

---

## 데이터 구조

```python
# scrapers/base.py

@dataclass
class ProductOption:
    color: str
    size: str
    stock: int          # 재고 수량 (-1 = 알 수 없음)
    price: int          # 가격 (원)
    soldout: bool = False
    option_id: str = ""
    stock_level: str = ""   # Nike 전용: LOW / MEDIUM / HIGH

@dataclass
class ProductInfo:
    name: str
    url: str
    site: str
    options: list[ProductOption]
```

---

## 사이트별 스크래퍼 상세

### 무신사 (`scrapers/musinsa.py`)

**API 엔드포인트:**
- 상품 정보: `GET goods-detail.musinsa.com/api2/goods/{pid}`
- 옵션 목록: `GET goods-detail.musinsa.com/api2/goods/{pid}/options?goodsSaleType=SALE&optKindCd={CLOTHES|SHOES|BAG|ACC|}`
- 재고 확인: `POST goods-detail.musinsa.com/api2/goods/{pid}/options/check-available-stock`

**핵심 로직:**
- `optKindCd`를 `["CLOTHES", "SHOES", "BAG", "ACC", ""]` 순서로 시도, 옵션이 있으면 사용
- 재고 수량: **바이너리 서치** (`_binary_search_stock`) — `check-available-stock` POST API 반복 호출
- `fulfillmentCenterId`: `[2, 1, 3]` 순서로 프로브해서 재고 있는 센터 ID 자동 선택 (`_probe_fulfillment_id`)
- `_STOCK_MAX_QTY = 999` 초과 시 `-1` (수량 미표시)

**URL 패턴:**
- `https://www.musinsa.com/products/{pid}`
- `https://www.musinsa.com/app/goods/{pid}`

**엣지 케이스:**
- 사이즈 없는 단일 옵션 상품: `size = size or "단일"`

---

### 29cm (`scrapers/cm29.py`)

**API 엔드포인트:**
- `GET https://bff-api.29cm.co.kr/api/v5/product-detail/{pid}`

**옵션 구조:**
```
data.optionItems.layout  = ["COLOR", "SIZE"]   ← 2단계
data.optionItems.list    = [
  { title: "WHITE",                            ← 색상(1단계)
    list: [
      { title: "S", limitedQty: 986,           ← 사이즈(2단계) + 재고
        frontOptionStockStatus: "ON_STOCK" | "SOLD_OUT",
        sellPrice: 0.0 }
    ]
  }, ...
]
data.sellPrice = 26100                          ← 기본 판매가
```

**엣지 케이스:**
- `result == "FAIL"` 또는 `data == null` → 상품 없음 에러
- `useOption == False` (옵션 없는 단일 상품) → `_make_single_option()` 으로 상품 레벨 `limitedQty` + `frontItemStockStatus` 사용
- layout 1단계 (SIZE만 or COLOR만) 처리

**URL 패턴:**
- `https://www.29cm.co.kr/products/{pid}`
- `https://www.29cm.co.kr/catalog/{pid}`
- `https://www.29cm.co.kr/product/detail?itemNo={pid}`

---

### Nike Korea (`scrapers/nike.py`)

**API 엔드포인트:**
- 상품 HTML: `GET https://www.nike.com/kr/t/{slug}-{groupKey}/{styleColor}`
  - HTML 내 `<script type="application/ld+json">` ProductGroup에서 상품명/색상/가격 추출
  - `"groupKey":"..."` 패턴으로 8자 영숫자 groupKey 추출 (URL에 없는 경우)
- 재고 API: `GET https://api.nike.com/discover/product_details_availability/v1/marketplace/KR/language/ko/consumerChannelId/{CHANNEL_ID}/groupKey/{groupKey}`
  - `sizes[].localizedLabel` (사이즈), `sizes[].productCode` (styleColor), `sizes[].availability.isAvailable`, `sizes[].availability.ship` (OOS/LOW/MEDIUM/HIGH)

**Channel ID:** `d9a5bc42-4b9c-4976-858a-f159cf99c647`

**핵심 로직:**
- URL에서 `groupKey` 추출 (8자 영숫자 + `/{styleColor}` 패턴)
- groupKey 없으면 HTML fetch 후 정규식으로 추출: `"groupKey":"([A-Za-z0-9]{6,12})"`
- 재고 수량은 LOW/MEDIUM/HIGH만 제공 (정확한 수치 없음) → `stock_level` 필드 사용
- `stock = 0 if soldout else -1`, `stock_level = "" if soldout else ship`

**URL 패턴 예시 (실제 작동 확인):**
```
https://www.nike.com/kr/t/hzTwdMlw/IB1873-702          ← 페가수스 42 런닝화
https://www.nike.com/kr/t/giaG3vkT/IH8039-001          ← 줌 보메로 5 SE
https://www.nike.com/kr/t/qdjlTENZ/IR0951-001          ← 에어포스 1 '07
https://www.nike.com/kr/t/IF0756-323/IF0756-323         ← 스포츠웨어 탑 (리다이렉트)
https://www.nike.com/kr/t/{slug}-{8charKey}/{styleColor}
```

**주의:** Nike는 재고 수량을 정확히 제공하지 않음. LOW/MEDIUM/HIGH 카테고리만 API에서 제공됨.

---

### Arc'teryx Korea (`scrapers/arcteryx.py`)

**데이터 소스:**
- Next.js App Router (RSC 페이로드) — 별도 REST API 없음
- HTML 내 `self.__next_f.push([1, "...JSON..."])` 블록에서 dehydrated React Query state 파싱

**데이터 추출 방법:**
```python
# RSC 블록에서 dehydrated state 위치 탐색
idx = content_str.find('{"state":{"mutations"')
obj, _ = decoder.raw_decode(content_str, idx)
product = obj['state']['queries'][0]['state']['data']['product']
```

**옵션 구조:**
```
product.options[0]  ← level=1, label="Colour"
  .values[].id          색상 ID
  .values[].value       색상명 (예: "BLACK", "FORAGE")
  .values[].sale_state  "ON" | "SOLDOUT"

product.options[1]  ← level=2, label="Size"
  .values[].value       사이즈명 (예: "XS", "One Size")
  .values[].parent_ids  [-1, 색상ID] → 상위 색상과 매핑
  .values[].stock       정확한 재고 수량
  .values[].is_orderable  True=주문가능, False=품절
  .values[].sell_price  가격
```

**URL 패턴:**
- `https://arcteryx.co.kr/products/view/{pid}`
- `https://arcteryx.co.kr/products/view/{pid}?sc=0`

**실제 작동 확인된 상품 ID:**
- `686182` — Heliad 15 Backpack (배낭, 3색상 × One Size)
- `686155` — Belfry Pant (팬츠, 2색상 × 5사이즈)
- `686300` — Cerium Hoody (후디, 다색상 × 다사이즈)

---

## FastAPI 서버 (`main.py`)

**주요 엔드포인트:**
- `POST /api/scrape` — 메인 재고 조회 (`{"url": "..."}`)
- `GET /api/health` — 헬스체크 (keep-alive용)
- `GET /api/debug-json?pid=...` — 무신사 API 원본 뷰어
- `GET /api/debug-json-29cm?pid=...` — 29cm API 원본 뷰어

**Render 슬립 방지:**
```python
# RENDER_EXTERNAL_URL 환경변수 있으면 14분마다 자체 헬스체크 핑
async def _keep_alive(url: str):
    await asyncio.sleep(60)
    while True:
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.get(url)
        await asyncio.sleep(14 * 60)
```

**사이트 라우팅:**
```python
def get_scraper(url):
    u = url.lower()
    if "musinsa.com" in u:    return MusinsaScraper()
    if "29cm.co.kr" in u:     return CM29Scraper()
    if "nike.com/kr" in u:    return NikeScraper()
    if "arcteryx.co.kr" in u: return ArcterycScraper()
    return GenericScraper()
```

---

## 프론트엔드 (`static/index.html`)

**주요 기능:**
- 클립보드 자동 감지: 창 포커스 시 클립보드에 지원 URL이 있으면 자동 팝업
- 검색 히스토리: localStorage (`inv_history`, 최대 20개)
- KREAM 버튼: `kream.co.kr/search?keyword={상품명}` 새 탭 열기
- 컬럼 정렬: 색상/사이즈/재고/가격 클릭 정렬

**재고 뱃지 표시 규칙:**
| 조건 | 표시 |
|------|------|
| soldout=true | 빨간 "품절" |
| stock≥0, stock≤3 | 노란 "N개" |
| stock>3 | 초록 "N개" |
| stock=-1, level=LOW | 노란 "소량 (Nike 비공개)" |
| stock=-1, level=MEDIUM | 초록 "보통 (Nike 비공개)" |
| stock=-1, level=HIGH | 초록 "충분 (Nike 비공개)" |
| stock=-1, level="" | 회색 "재고있음" |

**지원 사이트 클립보드 감지 패턴:**
```js
const SUPPORTED = [
  "musinsa.com/products/",
  "29cm.co.kr",
  "nike.com/kr/",
  "arcteryx.co.kr",
];
```

---

## 배포 (Render.com)

**설정값:**
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Python 버전: `python-3.12.3` (`runtime.txt`)
- 환경변수: `RENDER_EXTERNAL_URL` (자동 설정됨, keep-alive용)

**GitHub → Render 자동 배포:**
- `main` 브랜치 push → Render 자동 감지 → 재빌드

---

## 검증된 테스트 URL 목록 (18/18 통과)

```
# 무신사
https://www.musinsa.com/products/4047243   ← ACS+ OG 신발 (다색상×다사이즈)
https://www.musinsa.com/products/3435636   ← 코튼 니트 의류 (사이즈만)
https://www.musinsa.com/products/3999001   ← BIG COLLAR BLOUSE (색상+사이즈)
https://www.musinsa.com/products/4100000   ← 앵클부츠 (색상+사이즈)
https://www.musinsa.com/products/3800000   ← 훈민정음 맨투맨 (색상+사이즈)

# 29cm
https://www.29cm.co.kr/products/3751730   ← 티셔츠 (2단계: 색상×사이즈)
https://www.29cm.co.kr/products/3453535   ← 단일 옵션 (Free 사이즈)
https://www.29cm.co.kr/products/3765004   ← 가방 (useOption=False, 단일)
https://www.29cm.co.kr/catalog/3832386    ← 한정상품 (useOption=False, 단일)
https://www.29cm.co.kr/catalog/2634598    ← 신발 (사이즈별 재고)
https://www.29cm.co.kr/catalog/3894236    ← 가방 (색상만)

# Nike Korea
https://www.nike.com/kr/t/hzTwdMlw/IB1873-702   ← 페가수스 42 런닝화
https://www.nike.com/kr/t/giaG3vkT/IH8039-001   ← 줌 보메로 5 SE
https://www.nike.com/kr/t/qdjlTENZ/IR0951-001   ← 에어포스 1 '07
https://www.nike.com/kr/t/IF0756-323/IF0756-323  ← 스포츠웨어 탑

# Arc'teryx Korea
https://arcteryx.co.kr/products/view/686182   ← Heliad 15 Backpack
https://arcteryx.co.kr/products/view/686155   ← Belfry Pant
https://arcteryx.co.kr/products/view/686300   ← Cerium Hoody
```

---

## 알려진 제약사항

| 사이트 | 제약 | 이유 |
|--------|------|------|
| Nike | 재고 수량 불명 (LOW/MED/HIGH만) | Nike API 정책 |
| Arc'teryx | HTML 전체 파싱 필요 (~2-4초) | 별도 API 없음, RSC 페이로드 |
| 무신사 | binary search로 재고 파악 (~3-5초) | 직접 재고 API 없음 |
| Adidas | 지원 제외 | 클라우드 IP 차단 (Akamai), 장바구니만 수량 확인 가능 |

---

## 로컬 개발 환경 설정

```bash
git clone https://github.com/JeongHanJun/inventory_checker.git
cd inventory_checker
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8002 --reload
# 브라우저: http://localhost:8002
```

**Python 버전:** 3.12 권장 (3.11도 동작, 3.13+ 미검증)

---

## 향후 개발 시 참고사항

- 새 사이트 추가 시: `scrapers/` 에 `BaseScraper` 상속 클래스 추가 → `main.py`의 `get_scraper()` 에 라우팅 추가 → `index.html`의 `SUPPORTED` 배열에 도메인 추가
- 무신사 `fulfillmentCenterId`가 추가되면 `_FULFILLMENT_IDS = [2, 1, 3]` 리스트에 추가
- Arc'teryx BuildId(`LVStuGsI6DVZ6-0IYkeXi`)는 배포 시 바뀔 수 있으나 현재 코드는 BuildId 무관하게 RSC 블록 내용으로 파싱하므로 영향 없음
