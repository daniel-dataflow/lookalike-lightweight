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
# NeonLogHandler 구현 및 로깅 설정
# ──────────────────────────────────────
import traceback

class NeonLogHandler(logging.Handler):
    """
    FastAPI 에러 로그(WARN 이상)를 Neon PostgreSQL의 app_logs 테이블에 실시간 기록하는 핸들러.
    동시에 24시간 초과 로그는 즉각 파기하여 DB 용량을 안전하게 보호함.
    """
    def __init__(self, level=logging.WARN):
        super().__init__(level)

    def emit(self, record):
        # 재귀 로깅 루프 방지 및 WARN 미만 필터링
        if record.levelno < logging.WARN:
            return
        # app/database.py 모듈이나 psycopg2 관련 내부 로깅은 루프 방지를 위해 생략
        if "sqlalchemy" in record.name or "psycopg2" in record.name or "app.database" in record.name:
            return

        try:
            # INFO 레벨 이하는 무시 (WARNING, ERROR, CRITICAL만 수집)
            if record.levelno < logging.WARNING:
                return

            msg = self.format(record)
            err_type = "unknown_error"
            if record.exc_info:
                exc_type, _, _ = record.exc_info
                if exc_type:
                    err_type = exc_type.__name__
            elif record.levelname in ["ERROR", "CRITICAL"]:
                # 메시지 첫 단어 또는 주요 부분을 에러 타입으로 추정
                msg_body = record.message if hasattr(record, "message") else str(record.msg)
                first_part = msg_body.split(":")[0] if ":" in msg_body else msg_body.split(" ")[0]
                err_type = str(first_part)[:80]
            else:
                err_type = record.name

            # 로그 성격에 따른 서비스(컴포넌트) 분류 판별
            svc_name = "FastAPI"
            name_lower = record.name.lower()
            msg_lower = msg.lower()
            
            if "database" in name_lower or "sqlalchemy" in name_lower or "psycopg" in name_lower or "postgres" in msg_lower or "db_status" in msg_lower:
                svc_name = "PostgreSQL"
            elif "cloudinary" in name_lower or "cloudinary" in msg_lower:
                svc_name = "Cloudinary"
            elif "hf" in name_lower or "huggingface" in name_lower or "hf_space" in msg_lower or "gradio" in msg_lower:
                svc_name = "HuggingFace"

            level_str = record.levelname

            # Neon PostgreSQL에 삽입 및 24시간 만료 데이터 삭제
            from .database import get_pg_cursor, engine
            if engine is not None:
                with get_pg_cursor() as cur:
                    cur.execute(
                        "INSERT INTO app_logs (level, service, message, error_type) VALUES (%s, %s, %s, %s);",
                        (level_str, svc_name, msg, err_type)
                    )
                    cur.execute(
                        "DELETE FROM app_logs WHERE timestamp < NOW() - INTERVAL '24 hours';"
                    )
        except Exception as e:
            # 로깅 자체 예외는 재귀 호출 방지를 위해 무시
            pass

# 기본 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# NeonLogHandler 등록
try:
    neon_handler = NeonLogHandler()
    neon_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    
    # 루트 로거와 uvicorn, fastapi 관련 로거들에 핸들러 바인딩
    logging.getLogger().addHandler(neon_handler)
    for log_name in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"]:
        target_logger = logging.getLogger(log_name)
        target_logger.addHandler(neon_handler)
        target_logger.propagate = True
    
    logger.info("✅ NeonLogHandler가 루트 및 Uvicorn/FastAPI 로거에 성공적으로 추가되었습니다.")
except Exception as e:
    logger.warning(f"NeonLogHandler 등록 실패: {e}")

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

# 글로벌 예외 핸들러 등록 (미처리 에러 로깅 강제화)
from fastapi.responses import JSONResponse
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"서버 내부 예외 발생: {exc}", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"}
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

from .routers.log import router as log_router

app.include_router(pages_router)

app.include_router(auth_router)
app.include_router(product_router)
app.include_router(search_router)
app.include_router(inquiry_router)
app.include_router(admin_router)
app.include_router(metric_router)
app.include_router(metric_realtime_router)
app.include_router(log_router)



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
