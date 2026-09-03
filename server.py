"""
서버 진입점.
실행: python server.py     (콘솔 로그 표시)
    또는 pythonw server.py (백그라운드 실행, 로그는 logs/app.log에 자동 리다이렉트)
"""
import os
import sys
import uvicorn

# pythonw.exe로 실행 시 stdout/stderr을 파일로 리다이렉트 (콘솔 창 없어서 로그 사라짐 방지)
if sys.stdout is None or sys.stderr is None:
    _log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = open(
        os.path.join(_log_dir, 'app.log'),
        'a', encoding='utf-8', buffering=1,
    )
    sys.stdout = _log_file
    sys.stderr = _log_file


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )
