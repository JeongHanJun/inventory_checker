"""
서버 진입점.
실행: python server.py
"""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )
