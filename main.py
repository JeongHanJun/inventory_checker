"""
재고 조회기 - FastAPI 서버
실행: uvicorn main:app --host 0.0.0.0 --port 8002
"""

import os
import re
import asyncio
import httpx
import traceback
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scrapers.musinsa import MusinsaScraper
from scrapers.cm29 import CM29Scraper
from scrapers.nike import NikeScraper
from scrapers.adidas import AdidasScraper
from scrapers.generic import GenericScraper


# ── Render 슬립 방지: 14분마다 자체 핑 ───────────────────────────────────────
async def _keep_alive(url: str) -> None:
    """Render 무료 플랜 슬립(15분) 방지용 자체 핑 루프."""
    await asyncio.sleep(60)          # 서버 완전 기동 후 시작
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                await c.get(url)
        except Exception:
            pass
        await asyncio.sleep(14 * 60)  # 14분 대기


@asynccontextmanager
async def lifespan(app: FastAPI):
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        asyncio.create_task(_keep_alive(f"{render_url}/api/health"))
    yield


app = FastAPI(title="Inventory Checker", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_scraper(url: str):
    u = url.lower()
    if "musinsa.com" in u:
        return MusinsaScraper()
    if "29cm.co.kr" in u:
        return CM29Scraper()
    if "nike.com/kr" in u:
        return NikeScraper()
    if "adidas.co.kr" in u:
        return AdidasScraper()
    return GenericScraper()


class ScrapeRequest(BaseModel):
    url: str


# ── 메인 조회 ─────────────────────────────────────────────────────────────────

@app.post("/api/scrape")
async def scrape(req: ScrapeRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    scraper = get_scraper(url)
    try:
        result = await scraper.scrape(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"오류: {str(e)}")

    return {
        "name":    result.name,
        "url":     result.url,
        "site":    result.site,
        "options": [asdict(o) for o in result.options],
    }


# ── 디버그: 브라우저에서 직접 열 수 있는 JSON 뷰어 ────────────────────────────

@app.get("/api/debug-json")
async def debug_json(pid: str = Query(..., description="Musinsa 상품 ID")):
    """
    브라우저에서 직접 열 수 있는 Musinsa API 원본 응답 뷰어.
    예: http://localhost:8000/api/debug-json?pid=3435636
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": f"https://www.musinsa.com/products/{pid}",
        "Origin": "https://www.musinsa.com",
    }
    result = {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0, headers=headers) as client:
        for label, url in {
            "goods_info": f"https://goods-detail.musinsa.com/api2/goods/{pid}",
            "options_CLOTHES": f"https://goods-detail.musinsa.com/api2/goods/{pid}/options?goodsSaleType=SALE&optKindCd=CLOTHES",
            "options_SHOES":   f"https://goods-detail.musinsa.com/api2/goods/{pid}/options?goodsSaleType=SALE&optKindCd=SHOES",
            "options_plain":   f"https://goods-detail.musinsa.com/api2/goods/{pid}/options?goodsSaleType=SALE",
            "api_musinsa_options": f"https://api.musinsa.com/api2/goods/{pid}/options?goodsSaleType=SALE&optKindCd=CLOTHES",
            "api_musinsa_stock":   f"https://api.musinsa.com/api2/goods/{pid}/stock?goodsSaleType=SALE",
            "api_musinsa_info":    f"https://api.musinsa.com/api2/goods/{pid}",
        }.items():
            try:
                r = await client.get(url)
                result[label] = {"status": r.status_code, "url": url, "data": r.json()}
            except Exception as e:
                result[label] = {"error": str(e), "url": url}

    return JSONResponse(content=result)


@app.get("/api/debug-json-29cm")
async def debug_json_29cm(pid: str = Query(..., description="29cm 상품 ID")):
    """
    29cm 디버그: HTML 원문 일부 + API 응답 확인.
    예: http://localhost:8002/api/debug-json-29cm?pid=3751730
    """
    import json as _json
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    result = {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0, headers=headers) as client:
        for label, url in {
            "html_www":     f"https://www.29cm.co.kr/products/{pid}",
            "html_product": f"https://product.29cm.co.kr/catalog/{pid}",
        }.items():
            try:
                r = await client.get(url, headers={**headers, "Accept": "text/html"})
                html = r.text
                # __NEXT_DATA__ 시도
                m = re.search(r'<script\s+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
                if m:
                    result[label] = {"status": r.status_code, "url": url,
                                     "__NEXT_DATA__": _json.loads(m.group(1))}
                else:
                    # <script> 태그에 window.* 형태 데이터가 있을 수 있음
                    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
                    inline = [s[:300] for s in scripts if len(s) > 50][:5]
                    result[label] = {
                        "status": r.status_code, "url": url,
                        "note": "__NEXT_DATA__ not found",
                        "html_head_500": html[:500],
                        "inline_scripts_preview": inline,
                        "final_url": str(r.url),
                    }
            except Exception as e:
                result[label] = {"error": str(e), "url": url}

        for label, url in {
            "bff_product_detail":  f"https://bff-api.29cm.co.kr/api/v5/product-detail/{pid}",
            "bff_product_additional": f"https://bff-api.29cm.co.kr/api/v5/product-detail/additional/{pid}",
            "api_v5":  f"https://api.29cm.co.kr/v5/products/{pid}",
        }.items():
            try:
                r = await client.get(url, headers={**headers, "Accept": "application/json"})
                try:
                    result[label] = {"status": r.status_code, "url": url, "data": r.json()}
                except Exception:
                    result[label] = {"status": r.status_code, "url": url, "text": r.text[:300]}
            except Exception as e:
                result[label] = {"error": str(e), "url": url}

    return JSONResponse(content=result)


@app.get("/api/debug-capture")
async def debug_capture(url: str = Query(..., description="분석할 상품 URL")):
    """
    Playwright로 페이지를 열고 모든 JSON 응답을 캡처해서 반환.
    네이버/크림 등 신규 사이트 API 구조 분석용.
    예: http://localhost:8002/api/debug-capture?url=https://smartstore.naver.com/wawacorp/products/11583866632
    """
    from playwright.async_api import async_playwright, Response as PwResponse

    captured = []
    next_data = None
    title = ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ko-KR",
            extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"},
        )
        page = await ctx.new_page()

        async def on_response(resp: PwResponse):
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                data = await resp.json()
                captured.append({"url": resp.url, "status": resp.status, "data": data})
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            pass

        next_data_raw = await page.evaluate(
            "() => { const el = document.getElementById('__NEXT_DATA__'); "
            "return el ? el.textContent : null; }"
        )
        if next_data_raw:
            try:
                import json as _json
                next_data = _json.loads(next_data_raw)
            except Exception:
                pass

        try:
            title = await page.title()
        except Exception:
            pass

        await browser.close()

    return JSONResponse(content={
        "page_title": title,
        "url": url,
        "captured_json_count": len(captured),
        "__NEXT_DATA__": next_data,
        "captured_responses": captured,
    })


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── 정적 파일 ─────────────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
