"""
29cm Cloudflare 우회 진단 스크립트.

목적:
  cm29.py의 쿠키 워밍 추가 후에도 BFF가 403인 경우,
  httpx 자체의 TLS 지문이 Cloudflare WAF에 차단되는 것인지 확인.

실행:
  python test_cookie.py

결과 해석:
  bff: status=200  → httpx 쿠키 워밍 동작. cm29.py 또는 uvicorn 점검.
  bff: status=403  → httpx TLS 지문 차단. curl_cffi 도입 필요.
"""

import asyncio
import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def main():
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://www.29cm.co.kr/",
        "Origin": "https://www.29cm.co.kr",
    }
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=15.0, headers=headers
    ) as c:
        r1 = await c.get("https://www.29cm.co.kr/")
        print(f"homepage: status={r1.status_code}, cookies={dict(c.cookies)}")
        r2 = await c.get(
            "https://bff-api.29cm.co.kr/api/v5/product-detail/3673782"
        )
        print(f"bff:      status={r2.status_code}")


if __name__ == "__main__":
    asyncio.run(main())
