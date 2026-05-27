"""
FastAPI 기반 백엔드 애플리케이션 진입점
- 일체형 단일 서버 아키텍처 (Jinja2 SSR 100%)
- ENV_MODE=local  -> 로컬 PostgreSQL(5433) + 로컬 static/uploads
- ENV_MODE=production -> Supabase DB + Cloudflare R2
"""
import os
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import sentry_sdk

from .config import get_settings
from .database import init_all_databases, close_all_databases, cleanup_expired_sessions
from . import database as _db_module
from .routers import auth_router, product_router, search_router, inquiry_router, admin_router
from .routers.pages import router as pages_router
from .routers.metric import router as metric_router, start_metric_collector
from .routers.metric_realtime import router as metric_realtime_router

# ──────────────────────────────────────
# 로깅 설정
# ──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()

# ──────────────────────────────────────
# Sentry 초기화 (설정된 경우에만)
# ──────────────────────────────────────
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=1.0,
        environment=settings.ENV_MODE,
        profiles_sample_rate=1.0,
    )
    logger.info("Sentry SDK 초기화 완료")


# ──────────────────────────────────────
# 앱 생명주기 (startup / shutdown)
# ──────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 구동/종료 시 DB 연결 초기화/해제 (ENV_MODE에 따라 자동 분기)"""
    logger.info(f"앱 시작 [모드: {settings.ENV_MODE}] - DB 연결 초기화")
    try:
        init_all_databases()
    except Exception as e:
        logger.error(f"DB 연결 초기화 실패: {e}")

    try:
        cleanup_expired_sessions()
    except Exception as e:
        logger.warning(f"세션 정리 실패: {e}")

    # 5분 주기 인프라 메트릭 수집 백그라운드 태스크 기동
    collector_task = asyncio.create_task(start_metric_collector())
    logger.info("📊 인프라 메트릭 수집 태스크 시작")

    yield

    logger.info("앱 종료 - 데이터베이스 연결 해제")
    collector_task.cancel()
    close_all_databases()


# ──────────────────────────────────────
# FastAPI 앱 생성
# ──────────────────────────────────────
app = FastAPI(
    title=settings.APP_TITLE,
    description="Lookalike - AI 기반 패션 유사 상품 검색 (단일 서버 Jinja2 SSR)",
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ──────────────────────────────────────
# CORS (일체형이므로 최소 설정)
# ──────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Set-Cookie"],
)

# ──────────────────────────────────────
# 정적 파일 서빙 (절대 경로로 안전하게)
# main.py 위치: web/backend/app/ -> web/frontend/static
# ──────────────────────────────────────
_static_dir = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend", "static")
)
_uploads_dir = os.path.join(_static_dir, "uploads")
_raw_data_dir = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "raw")
)

# 로컬 스토리지 모드를 위해 uploads 디렉토리 미리 생성
os.makedirs(_uploads_dir, exist_ok=True)

if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
    logger.info(f"정적 파일 마운트: {_static_dir}")

# 로컬 이미지 폴더 마운트 (DB의 /raw/... 경로와 일치시킴)
if os.path.isdir(_raw_data_dir):
    app.mount("/raw", StaticFiles(directory=_raw_data_dir), name="raw_data")
    logger.info(f"로컬 이미지 마운트: /raw -> {_raw_data_dir}")
else:
    logger.warning(f"로컬 이미지 폴더를 찾을 수 없습니다: {_raw_data_dir}")

# ──────────────────────────────────────
# 라우터 등록
# pages_router를 최우선으로 -> Jinja2 SSR 페이지 우선 처리
# ──────────────────────────────────────
app.include_router(pages_router)

app.include_router(auth_router)
app.include_router(product_router)
app.include_router(search_router)
app.include_router(inquiry_router)
app.include_router(admin_router)
app.include_router(metric_router)
app.include_router(metric_realtime_router)


# ──────────────────────────────────────
# 헬스체크 & 상태 API & 파비콘
# ──────────────────────────────────────
from fastapi.responses import FileResponse

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = os.path.join(_static_dir, "images", "favicon.ico")
    if os.path.isfile(favicon_path):
        return FileResponse(favicon_path)
    return HTMLResponse(status_code=404)

@app.get("/health", tags=["system"])
async def health_check():
    """헬스체크 엔드포인트"""
    return {
        "status": "healthy",
        "environment": settings.ENV_MODE,
        "version": settings.APP_VERSION,
    }


@app.get("/api/status", tags=["system"])
async def api_status():
    """DB 연결 상태 확인"""
    return {
        "status": "running",
        "environment": settings.ENV_MODE,
        "databases": {
            "postgresql": "connected" if _db_module.engine is not None else "disconnected",
        },
    }


@app.get("/sentry-debug", tags=["system"])
async def trigger_error():
    """Sentry 동작 확인용 (의도적 오류)"""
    1 / 0
