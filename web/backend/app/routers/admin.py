"""
어드민 대시보드 API (경량 버전)
- Docker SDK / Kafka 실시간 스트리밍 제거
- Supabase DB 직접 조회 기반: 데이터 수집 현황, 에러 로그, 시스템 상태
"""
import logging
import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from ..config.admin import SYSTEM_CACHE_TTL, DB_CACHE_TTL
from ..config import get_settings
from ..database import get_pg_cursor
from ..models.admin import (
    PipelineRunResponse,
    PipelineStatusResponse,
    PipelineErrorResponse,
    ErrorLogListResponse,
    DataSummaryResponse,
    SystemHealthResponse,
    AdminDashboardResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# ──────────────────────────────────────
# [성능 최적화] 인메모리 캐시 저장소
# ──────────────────────────────────────
_dashboard_cache: Optional[dict] = None
_dashboard_cache_time: float = 0
_DASHBOARD_CACHE_TTL = DB_CACHE_TTL


# ──────────────────────────────────────
# 1. 통합 대시보드 API (프론트엔드 초기 로딩용)
# ──────────────────────────────────────
@router.get("/dashboard", response_model=AdminDashboardResponse)
async def get_admin_dashboard():
    """
    시스템 상태 + 데이터 요약 + 파이프라인 현황을 한 번에 반환.
    프론트엔드 HTTP 왕복을 최소화하기 위해 통합 엔드포인트로 제공.
    """
    global _dashboard_cache, _dashboard_cache_time

    now = time.time()
    if _dashboard_cache and now - _dashboard_cache_time < _DASHBOARD_CACHE_TTL:
        return _dashboard_cache

    loop = asyncio.get_event_loop()
    system, data_summary, pipeline = await asyncio.gather(
        loop.run_in_executor(None, _get_system_health),
        loop.run_in_executor(None, _get_data_summary),
        loop.run_in_executor(None, _get_pipeline_status),
    )

    result = AdminDashboardResponse(
        system=system,
        data_summary=data_summary,
        pipeline=pipeline,
    )
    _dashboard_cache = result
    _dashboard_cache_time = now
    return result


# ──────────────────────────────────────
# 2. 시스템 상태 API
# ──────────────────────────────────────
@router.get("/system/health", response_model=SystemHealthResponse)
async def get_system_health_api():
    """Render 서버 + Supabase DB 상태 반환"""
    return _get_system_health()


def _get_system_health() -> SystemHealthResponse:
    settings = get_settings()
    
    # 1. PostgreSQL DB 정보 조회
    active_connections = 0
    db_size = 0
    db_status = "healthy"
    try:
        with get_pg_cursor() as cur:
            cur.execute("SELECT count(*) as cnt FROM pg_stat_activity;")
            row = cur.fetchone()
            active_connections = row["cnt"] if row else 0

            cur.execute("SELECT pg_database_size(current_database()) as size;")
            row = cur.fetchone()
            db_size = row["size"] if row else 0
    except Exception as e:
        logger.error(f"시스템 상태 DB 조회 실패: {e}")
        db_status = "error"

    # 2. Cloudinary 정보 조회
    cloudinary_status = "healthy"
    cloudinary_usage_bytes = 0
    cloudinary_resources_count = 0
    try:
        import cloudinary
        import cloudinary.api
        # Cloudinary 인증 설정
        cloudinary.config(
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            api_key=settings.CLOUDINARY_API_KEY,
            api_secret=settings.CLOUDINARY_API_SECRET,
            secure=True,
        )
        # API 사용량 정보 가져오기
        usage = cloudinary.api.usage()
        # resources의 최신 개수를 세기 위해 list_resources 실행 (최대 1건 정보만 받아와서 속도 최적화)
        resources = cloudinary.api.resources(max_results=1)
        
        cloudinary_resources_count = usage.get("resources", 0)
        # usage["storage"]["usage"]는 바이트 단위 크기이므로 그대로 대입
        cloudinary_usage_bytes = usage.get("storage", {}).get("usage", 0)
    except Exception as e:
        logger.warning(f"Cloudinary 상태 조회 실패: {e}")
        cloudinary_status = "error"

    # 3. HuggingFace Space 정보 조회
    hf_status = "healthy"
    hf_model_status = "healthy"
    hf_latency_ms = 0.0
    try:
        import httpx
        hf_url = settings.HF_SPACE_URL
        if hf_url:
            t0 = time.time()
            # HF Space는 보통 / 로 접근하면 GUI를 주기 때문에 헬스체크 또는 메타데이터 경로 호출
            # 여기서는 /api/predict 또는 루트 경로 호출 성능 측정
            resp = httpx.get(hf_url, timeout=3.0)
            latency = (time.time() - t0) * 1000.0
            hf_latency_ms = round(latency, 2)
            if resp.status_code >= 400:
                hf_model_status = f"HTTP {resp.status_code}"
                if resp.status_code == 503:
                    hf_status = "sleeping"
        else:
            hf_status = "disabled"
            hf_model_status = "disabled"
    except Exception as e:
        logger.warning(f"HuggingFace Space 상태 조회 실패: {e}")
        hf_status = "error"
        hf_model_status = "offline"

    return SystemHealthResponse(
        server_status="healthy",
        db_status=db_status,
        db_active_connections=active_connections,
        db_size_mb=round(db_size / 1024 / 1024, 2),
        app_version=settings.APP_VERSION,
        environment=settings.ENV_MODE,
        cloudinary_status=cloudinary_status,
        cloudinary_usage_bytes=cloudinary_usage_bytes,
        cloudinary_resources_count=cloudinary_resources_count,
        hf_status=hf_status,
        hf_model_status=hf_model_status,
        hf_latency_ms=hf_latency_ms,
    )


# ──────────────────────────────────────
# 3. 데이터 요약 통계 API
# ──────────────────────────────────────
@router.get("/data/summary", response_model=DataSummaryResponse)
async def get_data_summary_api():
    """상품, 임베딩, 사용자, 검색 건수 등 데이터 요약"""
    return _get_data_summary()


def _get_data_summary() -> DataSummaryResponse:
    try:
        with get_pg_cursor() as cur:
            # 각 테이블 집계를 한 번의 쿼리로 처리
            cur.execute("""
                SELECT
                    (SELECT count(*) FROM products) AS total_products,
                    (SELECT count(*) FROM product_embeddings) AS total_embeddings,
                    (SELECT count(*) FROM users) AS total_users,
                    (SELECT count(*) FROM search_logs) AS total_searches,
                    (SELECT pg_database_size(current_database())) AS db_size
            """)
            row = cur.fetchone()

            # 브랜드별 상품 수 집계
            cur.execute("""
                SELECT brand_name, count(*) as count
                FROM products
                WHERE brand_name IS NOT NULL
                GROUP BY brand_name
                ORDER BY count DESC
                LIMIT 20
            """)
            brand_rows = cur.fetchall()

        return DataSummaryResponse(
            total_products=row["total_products"],
            total_embeddings=row["total_embeddings"],
            total_users=row["total_users"],
            total_searches=row["total_searches"],
            db_size_mb=round(row["db_size"] / 1024 / 1024, 2),
            brands=[
                {"brand": r["brand_name"], "count": r["count"]}
                for r in brand_rows
            ],
        )
    except Exception as e:
        logger.error(f"데이터 요약 조회 실패: {e}")
        return DataSummaryResponse()


# ──────────────────────────────────────
# 4. 파이프라인(크롤링) 현황 API
# ──────────────────────────────────────
@router.get("/pipeline/status", response_model=PipelineStatusResponse)
async def get_pipeline_status_api():
    """GitHub Actions 크롤링 파이프라인 실행 현황"""
    return _get_pipeline_status()


def _get_pipeline_status() -> PipelineStatusResponse:
    try:
        today = datetime.utcnow().date()
        with get_pg_cursor() as cur:
            # 최근 실행 10건
            cur.execute("""
                SELECT run_id, pipeline_name, brand, status,
                       total_items, new_items, updated_items, error_count,
                       started_at, finished_at, duration_sec
                FROM pipeline_runs
                ORDER BY started_at DESC
                LIMIT 10
            """)
            runs = [PipelineRunResponse(**r) for r in cur.fetchall()]

            # 오늘 실행 횟수
            cur.execute(
                "SELECT count(*) as cnt FROM pipeline_runs WHERE started_at::date = %s",
                (today,),
            )
            total_today = cur.fetchone()["cnt"]

            # 오늘 에러 수
            cur.execute(
                "SELECT count(*) as cnt FROM pipeline_errors WHERE created_at::date = %s",
                (today,),
            )
            errors_today = cur.fetchone()["cnt"]

            # 마지막 성공 실행
            cur.execute("""
                SELECT run_id, pipeline_name, brand, status,
                       total_items, new_items, updated_items, error_count,
                       started_at, finished_at, duration_sec
                FROM pipeline_runs
                WHERE status = 'completed'
                ORDER BY finished_at DESC
                LIMIT 1
            """)
            last_success_row = cur.fetchone()
            last_success = PipelineRunResponse(**last_success_row) if last_success_row else None

        return PipelineStatusResponse(
            recent_runs=runs,
            total_runs_today=total_today,
            total_errors_today=errors_today,
            last_successful_run=last_success,
        )
    except Exception as e:
        logger.error(f"파이프라인 현황 조회 실패: {e}")
        return PipelineStatusResponse(recent_runs=[])


# ──────────────────────────────────────
# 5. 에러 로그 API (페이지네이션)
# ──────────────────────────────────────
@router.get("/pipeline/errors", response_model=ErrorLogListResponse)
async def get_pipeline_errors(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    error_type: Optional[str] = Query(None, description="에러 타입 필터"),
):
    """파이프라인 에러 로그 조회 (최신순, 페이지네이션)"""
    try:
        offset = (page - 1) * page_size

        with get_pg_cursor() as cur:
            conditions = []
            params: list = []

            if error_type:
                conditions.append("error_type = %s")
                params.append(error_type)

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            cur.execute(
                f"SELECT count(*) as cnt FROM pipeline_errors {where}",
                params,
            )
            total = cur.fetchone()["cnt"]

            cur.execute(
                f"""
                SELECT error_id, run_id, error_type, error_message,
                       product_id, source_url, created_at
                FROM pipeline_errors
                {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [page_size, offset],
            )
            errors = [PipelineErrorResponse(**r) for r in cur.fetchall()]

        return ErrorLogListResponse(errors=errors, total=total, page=page)

    except Exception as e:
        logger.error(f"에러 로그 조회 실패: {e}")
        raise HTTPException(status_code=500, detail="에러 로그 조회 실패")


# ──────────────────────────────────────
# 6. 파이프라인 실행 기록 API (GitHub Actions 콜백용)
# ──────────────────────────────────────
@router.post("/pipeline/report")
async def report_pipeline_run(data: dict):
    """
    GitHub Actions 워크플로우가 실행 결과를 보고하는 콜백 엔드포인트.
    Pipeline이 완료되면 이 API를 호출하여 결과를 기록한다.
    """
    try:
        with get_pg_cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline_runs (
                    pipeline_name, brand, status,
                    total_items, new_items, updated_items, error_count,
                    github_run_id, started_at, finished_at, duration_sec, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING run_id
                """,
                (
                    data.get("pipeline_name"),
                    data.get("brand"),
                    data.get("status", "completed"),
                    data.get("total_items", 0),
                    data.get("new_items", 0),
                    data.get("updated_items", 0),
                    data.get("error_count", 0),
                    data.get("github_run_id"),
                    data.get("started_at"),
                    data.get("finished_at"),
                    data.get("duration_sec"),
                    data.get("metadata", "{}"),
                ),
            )
            row = cur.fetchone()

        return {"success": True, "run_id": row["run_id"]}

    except Exception as e:
        logger.error(f"파이프라인 결과 기록 실패: {e}")
        raise HTTPException(status_code=500, detail="기록 실패")
