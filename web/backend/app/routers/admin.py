"""
어드민 대시보드 API (경량 버전)
- Docker SDK / Kafka 실시간 스트리밍 제거
- Supabase DB 직접 조회 기반: 데이터 수집 현황, 에러 로그, 시스템 상태
"""
import os
import logging
import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from ..config.admin import SYSTEM_CACHE_TTL, DB_CACHE_TTL
from ..config import get_settings
from ..database import get_pg_cursor, get_dw_cursor
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
    
    # 1. PostgreSQL DB 정보 조회 (활성 연결)
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

    # 1-1. Neon DB 각 환경별 상세 용량 및 Neon 검증
    # 대상 URL들: DEV_DATABASE_URL, DEV_DW_DATABASE_URL, PROD_DATABASE_URL, PROD_DW_DATABASE_URL
    db_dev_size_mb = 0.0
    db_dev_dw_size_mb = 0.0
    db_dev_total_size_mb = 0.0
    db_prod_size_mb = 0.0
    db_prod_dw_size_mb = 0.0
    db_prod_total_size_mb = 0.0
    db_urls_neon_status = {}

    db_urls_map = {
        "DEV_DATABASE_URL": settings.DEV_DATABASE_URL,
        "DEV_DW_DATABASE_URL": settings.DEV_DW_DATABASE_URL,
        "PROD_DATABASE_URL": settings.PROD_DATABASE_URL,
        "PROD_DW_DATABASE_URL": settings.PROD_DW_DATABASE_URL,
    }

    for name, url in db_urls_map.items():
        if not url:
            db_urls_neon_status[name] = "None"
            continue
        
        # Neon DB 판단 로직: 도메인에 .neon.tech 가 포함되는지 확인
        is_neon = "neon.tech" in url
        db_urls_neon_status[name] = "Neon" if is_neon else "Other"

    # HTTP API 호출을 위한 헬퍼 정의
    # Neon DB API v2: GET https://api.neon.tech/api/v2/projects/{project_id}
    # Response JSON에서 projects.consumption_metrics.storage_bytes 혹은 logical_size_bytes 정보 수집
    import httpx
    
    def get_neon_synthetic_size_mb(project_id: str, account_key_ref: str) -> float | None:
        if not project_id:
            return None
        
        # account_key_ref 는 "NEON_KEY_ACCOUNT_1" 또는 "NEON_KEY_ACCOUNT_2"
        # 실제 계정 토큰값 추출
        token = getattr(settings, account_key_ref, None)
        if not token:
            # 설정에서 조회 안되면 환경변수 직접 탐색
            token = os.environ.get(account_key_ref)
        if not token:
            # Fallback (구조화되지 않은 상태면 settings.NEON_KEY_ACCOUNT_1 기본 사용)
            token = settings.NEON_KEY_ACCOUNT_1 or os.environ.get("NEON_KEY_ACCOUNT_1")
            
        if not token:
            logger.warning(f"Neon API 토큰이 누락되어 project_id({project_id}) 용량 API 조회 불가")
            return None
            
        try:
            url = f"https://console.neon.tech/api/v2/projects/{project_id}"
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}"
            }
            # Neon API 통신 (타임아웃 3초)
            resp = httpx.get(url, headers=headers, timeout=3.0)
            if resp.status_code == 200:
                p_data = resp.json().get("project", {})
                # synthetic_storage_size 필드가 Neon 콘솔에 표시되는 실제 스토리지 용량 바이트 수치입니다.
                storage_bytes = p_data.get("synthetic_storage_size")
                
                if storage_bytes is not None:
                    return round(float(storage_bytes) / 1024 / 1024, 2)
        except Exception as e:
            logger.warning(f"Neon API 조회 실패 (project: {project_id}): {e}")
        return None

    # 각 데이터베이스 프로젝트에 매핑된 Neon DB 용량 API 및 DB 쿼리 병행 처리
    # 맵핑 사전 빌드
    neon_projects_info = {
        "DEV_DATABASE_URL": {
            "project_id": settings.NEON_DEV_PROJECT_ID,
            "api_key_ref": settings.NEON_DEV_API_KEY
        },
        "DEV_DW_DATABASE_URL": {
            "project_id": settings.NEON_DEV_DW_PROJECT_ID,
            "api_key_ref": settings.NEON_DEV_DW_API_KEY
        },
        "PROD_DATABASE_URL": {
            "project_id": settings.NEON_PROD_PROJECT_ID,
            "api_key_ref": settings.NEON_PROD_API_KEY
        },
        "PROD_DW_DATABASE_URL": {
            "project_id": settings.NEON_PROD_DW_PROJECT_ID,
            "api_key_ref": settings.NEON_PROD_DW_API_KEY
        }
    }

    for name, url in db_urls_map.items():
        if not url:
            db_urls_neon_status[name] = "None"
            continue
        
        # Neon DB 판단 로직: 도메인에 .neon.tech 가 포함되는지 확인
        is_neon = "neon.tech" in url
        db_urls_neon_status[name] = "Neon" if is_neon else "Other"

        # 1차 시도: Neon 공식 API를 사용하여 Synthetic Storage 용량 조회
        proj_info = neon_projects_info.get(name, {})
        size_mb = get_neon_synthetic_size_mb(proj_info.get("project_id"), proj_info.get("api_key_ref"))
        
        if size_mb is not None:
            # API 조회 성공
            if name == "DEV_DATABASE_URL":
                db_dev_size_mb = size_mb
            elif name == "DEV_DW_DATABASE_URL":
                db_dev_dw_size_mb = size_mb
            elif name == "PROD_DATABASE_URL":
                db_prod_size_mb = size_mb
            elif name == "PROD_DW_DATABASE_URL":
                db_prod_dw_size_mb = size_mb
            continue

        # 2차 시도 (API 실패 시 fallback): psycopg2 쿼리를 통해 logical pg_database_size 용량 측정
        try:
            # 쿼리 시 sslmode=require 파싱 처리
            conn_url = url
            if conn_url.startswith("postgres://"):
                conn_url = conn_url.replace("postgres://", "postgresql://", 1)
            
            # psycopg2 직접 연결 시도 (timeout 3초 설정)
            # DSN 문자열과 sslmode, connect_timeout을 쿼리스트링 파라미터 형태로 안전하게 결합
            if "connect_timeout=" not in conn_url:
                separator = "&" if "?" in conn_url else "?"
                conn_url = f"{conn_url}{separator}connect_timeout=3"
            if "sslmode=" not in conn_url and not any(h in conn_url for h in ["localhost", "127.0.0.1", "db", "postgres"]):
                separator = "&" if "?" in conn_url else "?"
                conn_url = f"{conn_url}{separator}sslmode=require"
            
            import psycopg2
            from psycopg2.extras import RealDictCursor
            with psycopg2.connect(conn_url) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT pg_database_size(current_database()) as size;")
                    r = cur.fetchone()
                    size_bytes = r["size"] if r else 0
                    fallback_size_mb = round(size_bytes / 1024 / 1024, 2)
                    
                    if name == "DEV_DATABASE_URL":
                        db_dev_size_mb = fallback_size_mb
                    elif name == "DEV_DW_DATABASE_URL":
                        db_dev_dw_size_mb = fallback_size_mb
                    elif name == "PROD_DATABASE_URL":
                        db_prod_size_mb = fallback_size_mb
                    elif name == "PROD_DW_DATABASE_URL":
                        db_prod_dw_size_mb = fallback_size_mb
        except Exception as ex:
            logger.warning(f"Neon DB fallback 용량 조회 실패 ({name}): {ex}")
            db_urls_neon_status[name] = "Error"

    db_dev_total_size_mb = round(db_dev_size_mb + db_dev_dw_size_mb, 2)
    db_prod_total_size_mb = round(db_prod_size_mb + db_prod_dw_size_mb, 2)

    # 2. Cloudinary 정보 조회
    cloudinary_status = "healthy"
    cloudinary_usage_bytes = 0
    cloudinary_resources_count = 0
    cloudinary_credits_usage = 0.0
    cloudinary_credits_limit = 25.0
    cloudinary_credits_percent = 0.0
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
        
        # 크레딧(Credit) 사용량 정보 획득
        credits_info = usage.get("credits", {})
        cloudinary_credits_usage = credits_info.get("usage", 0.0)
        cloudinary_credits_limit = credits_info.get("limit", 25.0)
        cloudinary_credits_percent = credits_info.get("used_percent", 0.0)
    except Exception as e:
        logger.warning(f"Cloudinary 상태 조회 실패: {e}")
        cloudinary_status = "error"

    # 3. HuggingFace Space 정보 조회
    hf_status = "healthy"
    hf_model_status = "healthy"
    hf_latency_ms = 0.0
    hf_used_storage_bytes = 0
    hf_hardware = ""
    try:
        import httpx
        hf_url = settings.HF_SPACE_URL
        if hf_url:
            t0 = time.time()
            resp = httpx.get(hf_url, timeout=3.0)
            latency = (time.time() - t0) * 1000.0
            hf_latency_ms = round(latency, 2)
            if resp.status_code >= 400:
                hf_model_status = f"HTTP {resp.status_code}"
                if resp.status_code == 503:
                    hf_status = "sleeping"
            
            # HuggingFace API를 통해 Space 세부 정보를 조회합니다.
            # URL 예: https://huggingface.co/api/spaces/daniel0708/lookalike-yolo
            # settings.HF_TOKEN이 존재하는 경우 사용합니다.
            if settings.HF_TOKEN and "daniel0708-lookalike-yolo" in hf_url:
                try:
                    hf_api_url = "https://huggingface.co/api/spaces/daniel0708/lookalike-yolo"
                    headers = {"Authorization": f"Bearer {settings.HF_TOKEN}"}
                    api_resp = httpx.get(hf_api_url, headers=headers, timeout=3.0)
                    if api_resp.status_code == 200:
                        api_data = api_resp.json()
                        hf_used_storage_bytes = api_data.get("usedStorage", 0)
                        hf_hardware = api_data.get("runtime", {}).get("hardware", {}).get("current", "")
                except Exception as api_err:
                    logger.warning(f"HuggingFace Space API 조회 에러: {api_err}")
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
        db_dev_size_mb=db_dev_size_mb,
        db_dev_dw_size_mb=db_dev_dw_size_mb,
        db_dev_total_size_mb=db_dev_total_size_mb,
        db_prod_size_mb=db_prod_size_mb,
        db_prod_dw_size_mb=db_prod_dw_size_mb,
        db_prod_total_size_mb=db_prod_total_size_mb,
        db_urls_neon_status=db_urls_neon_status,
        cloudinary_status=cloudinary_status,
        cloudinary_usage_bytes=cloudinary_usage_bytes,
        cloudinary_limit_bytes=25 * 1024 * 1024 * 1024, # 25 GB
        cloudinary_credits_usage=cloudinary_credits_usage,
        cloudinary_credits_limit=cloudinary_credits_limit,
        cloudinary_credits_percent=cloudinary_credits_percent,
        cloudinary_resources_count=cloudinary_resources_count,
        hf_status=hf_status,
        hf_model_status=hf_model_status,
        hf_latency_ms=hf_latency_ms,
        hf_used_storage_bytes=hf_used_storage_bytes,
        hf_hardware=hf_hardware,
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
        with get_dw_cursor() as cur:
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

        with get_dw_cursor() as cur:
            conditions: list = []
            params: list = []

            if error_type:
                conditions.append("error_type = %s")
                params.append(error_type)

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

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

        return ErrorLogListResponse(errors=errors, total=len(errors), page=page)

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
        with get_dw_cursor() as cur:
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


# ──────────────────────────────────────
# 7. 크롤링 파이프라인 모니터링 API (admin_crawling.html 연동)
# ──────────────────────────────────────
@router.get("/crawling/staging")
async def get_crawling_staging():
    """
    브랜드별 수집 데이터(Staging) 현황, 운영(Prod) 데이터 현황 및 정합성 검사 상태 조회
    """
    try:
        settings = get_settings()
        
        # 1. 지원 브랜드 목록 정의
        brands = ["8SECONDS", "UNIQLO", "MUSINSA", "TOPTEN", "ZARA"]
        brand_stats = []
        total_staging = 0
        
        from ..database import get_dw_cursor, get_prod_cursor
        
        for brand in brands:
            staging_count = 0
            embed_count = 0
            naver_count = 0
            prod_count = 0
            img_count = 0
            latest_dt = None
            hours_elapsed = 999
            pipeline_error_count = 0
            
            # (1) DW DB에서 스테이징 데이터 집계
            try:
                with get_dw_cursor() as cur:
                    cur.execute(
                        "SELECT count(*) as cnt, max(create_dt) as max_dt FROM staging_products WHERE brand_name = %s;",
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        staging_count = r["cnt"] or 0
                        latest_dt = r["max_dt"]
                        if latest_dt:
                            # timezone aware를 맞춰서 계산
                            now = datetime.now(latest_dt.tzinfo)
                            delta = now - latest_dt
                            hours_elapsed = int(delta.total_seconds() / 3600)
                    
                    cur.execute(
                        "SELECT count(*) as cnt FROM staging_product_embeddings WHERE brand = %s;",
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        embed_count = r["cnt"] or 0
                        
                    cur.execute(
                        "SELECT count(*) as cnt FROM staging_naver_prices WHERE UPPER(brand) = %s;",
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        naver_count = r["cnt"] or 0
                        
                    # 이미지 업로드 완료 수 (Cloudinary 주소 형태로 들어간 건)
                    cur.execute(
                        "SELECT count(*) as cnt FROM staging_products WHERE brand_name = %s AND img_url LIKE '%%cloudinary.com%%';",
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        img_count = r["cnt"] or 0
                        
                    # 해당 브랜드의 가장 최근 파이프라인 run_id를 찾아서 그 구동 건에 속한 에러만 집계 (과거 에러 누적 방지)
                    cur.execute(
                        """
                        SELECT count(*) as cnt FROM pipeline_errors pe
                        WHERE pe.run_id = (
                            SELECT run_id FROM pipeline_runs 
                            WHERE brand = %s 
                            ORDER BY run_id DESC LIMIT 1
                        );
                        """,
                        (brand,)
                     )
                    r = cur.fetchone()
                    if r:
                        pipeline_error_count = r["cnt"] or 0
                        
                    # 해당 브랜드의 가장 최근 파이프라인 구동 정보에서 metadata(목표량 등) 조회
                    target_count = 0
                    target_counts_map = {}
                    cur.execute(
                        """
                        SELECT metadata FROM pipeline_runs
                        WHERE brand = %s
                        ORDER BY run_id DESC LIMIT 1;
                        """,
                        (brand,)
                    )
                    run_row = cur.fetchone()
                    if run_row and run_row["metadata"]:
                        import json as _json
                        try:
                            meta = run_row["metadata"]
                            if isinstance(meta, str):
                                meta = _json.loads(meta)
                            target_count = meta.get("target_total", 0)
                            target_counts_map = meta.get("target_counts", {})
                        except Exception as meta_err:
                            logger.warning(f"metadata 파싱 실패: {meta_err}")
                            
                    # 카테고리별 실시간 수집 현황 집계
                    cur.execute(
                        """
                        SELECT category, count(*) as cnt 
                        FROM staging_products 
                        WHERE brand_name = %s 
                        GROUP BY category;
                        """,
                        (brand,)
                    )
                    cat_staging = {row["category"]: row["cnt"] for row in cur.fetchall()}
                    
                    cur.execute(
                        """
                        SELECT sp.category, count(*) as cnt 
                        FROM staging_product_embeddings spe
                        JOIN staging_products sp ON spe.product_id = sp.product_id
                        WHERE sp.brand_name = %s
                        GROUP BY sp.category;
                        """,
                        (brand,)
                    )
                    cat_embed = {row["category"]: row["cnt"] for row in cur.fetchall()}

                    cur.execute(
                        """
                        SELECT sp.category, count(*) as cnt 
                        FROM staging_naver_prices snp
                        JOIN staging_products sp ON snp.product_id = sp.product_id
                        WHERE sp.brand_name = %s
                        GROUP BY sp.category;
                        """,
                        (brand,)
                    )
                    cat_naver = {row["category"]: row["cnt"] for row in cur.fetchall()}

                    cur.execute(
                        """
                        SELECT category, count(*) as cnt 
                        FROM staging_products 
                        WHERE brand_name = %s AND img_url LIKE '%%cloudinary.com%%'
                        GROUP BY category;
                        """,
                        (brand,)
                    )
                    cat_img = {row["category"]: row["cnt"] for row in cur.fetchall()}

                    categories = []
                    # 모든 감지되거나 대상 카테고리 루프 (Outer, Top, Bottom 등)
                    all_categories = sorted(list(set(list(cat_staging.keys()) + list(target_counts_map.keys()) + ["Outer", "Top", "Bottom"])))
                    for cat in all_categories:
                        cat_tgt = target_counts_map.get(cat, 0)
                        cat_stg = cat_staging.get(cat, 0)
                        if cat_tgt == 0 and cat_stg == 0:
                            continue # 데이터가 전혀 없는 카테고리는 생략
                        categories.append({
                            "category": cat,
                            "staging_count": cat_stg,
                            "target_count": cat_tgt,
                            "embed_count": cat_embed.get(cat, 0),
                            "img_count": cat_img.get(cat, 0),
                            "naver_count": cat_naver.get(cat, 0)
                        })

            except Exception as dw_err:
                logger.error(f"DW Staging {brand} 조회 실패: {dw_err}")
                categories = []
                target_count = 0
                
            # (2) Active Core DB에서 운영 데이터 집계
            try:
                with get_prod_cursor() as cur:
                    cur.execute(
                        "SELECT count(*) as cnt FROM products WHERE brand_name = %s;",
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        prod_count = r["cnt"] or 0
            except Exception as prod_err:
                logger.error(f"PROD {brand} 조회 실패: {prod_err}")
                
            # (3) 정합성 상태 판별
            # - empty: 스테이징에 데이터 없음
            # - ready: 24시간 유예기간 경과
            # - waiting: 24시간 미만 대기
            # - img_missing: 이미지가 누락된 경우
            if staging_count == 0:
                integrity_status = "empty"
            elif img_count < staging_count:
                integrity_status = "img_missing"
            elif hours_elapsed >= 24:
                integrity_status = "ready"
            else:
                integrity_status = "waiting"
                
            total_staging += staging_count
            
            brand_stats.append({
                "brand": brand,
                "staging_count": staging_count,
                "target_count": target_count,
                "prod_count": prod_count,
                "embed_count": embed_count,
                "naver_count": naver_count,
                "img_count": img_count,
                "integrity_status": integrity_status,
                "latest_dt": latest_dt.isoformat() if latest_dt else None,
                "hours_elapsed": hours_elapsed,
                "pipeline_error_count": pipeline_error_count,
                "categories": categories
            })
            
        return {
            "success": True,
            "total_staging": total_staging,
            "is_test_db": False,
            "brands": brand_stats
        }
    except Exception as e:
        logger.error(f"어드민 크롤링 현황 조회 에러: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/crawling/toggle-mode")
async def toggle_database_mode(data: dict):
    """
    DB 모드 확인 API (DEV/PROD 구조로 단순화 - TEST 단계 제거)
    ENV_MODE에 의해 시작 시에 결정되며, 런타임에서 변경 불필요
    """
    try:
        settings = get_settings()
        env_mode = settings.ENV_MODE.lower()
        active_url = settings.PROD_DATABASE_URL_ACTIVE or settings.DATABASE_URL
        
        return {
            "success": True,
            "env_mode": env_mode,
            "active_db": "DEV" if env_mode in ["local", "dev"] else "PROD",
            "message": f"{'DEV(LOCAL)' if env_mode in ['local', 'dev'] else 'PROD(실서버)'} 환경으로 고정 동작 중입니다. DB 모드 변경은 .env의 ENV_MODE 수정 후 서버 재시작으로 적용하세요."
        }
    except Exception as e:
        logger.error(f"모드 확인 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/crawling/run")
async def run_manual_crawling(data: dict):
    """
    수동 크롤링 실행 트리거 (GitHub Actions 호출 연계 혹은 백그라운드 태스크)
    실제 백그라운드 수동 구동 처리를 하거나, 로컬 커맨드를 비동기로 실행
    """
    try:
        brand = data.get("brand", "").lower()
        limit = data.get("limit", 10)
        
        if not brand:
            raise HTTPException(status_code=400, detail="브랜드명이 필요합니다.")
            
        # CLI 명령어를 로컬 백그라운드 서브프로세스로 기동하여 연동
        import sys
        import subprocess
        
        # ──────────────────────────────────────────────────────────
        # [중요] 크롤러 실행 Python 환경 결정
        # gradio_client, playwright 등 크롤러 의존성이 설치된
        # ml-env를 우선 탐색하고, 없으면 현재 서버의 Python 사용
        # ──────────────────────────────────────────────────────────
        _ml_env_candidates = [
            r"C:\Users\Daniel\miniconda3\envs\ml-env\python.exe",
            os.path.join(os.path.expanduser("~"), "miniconda3", "envs", "ml-env", "python.exe"),
            os.path.join(os.path.expanduser("~"), "anaconda3", "envs", "ml-env", "python.exe"),
        ]
        python_exe = sys.executable  # 기본값: 현재 서버 Python
        for _candidate in _ml_env_candidates:
            if os.path.isfile(_candidate):
                python_exe = _candidate
                break
        
        cli_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "data-pipeline", "crawlers", "web_crawlers", "crawling_pipeline_cli.py")
        )
        
        # 크롤링 로그를 파일로 저장 (디버깅 용이)
        log_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "logs")
        )
        os.makedirs(log_dir, exist_ok=True)
        log_file_path = os.path.join(log_dir, f"crawl_{brand}.log")
        
        cmd = [python_exe, cli_path, "--brand", brand, "--limit", str(limit), "--action", "crawl"]
        logger.info(f"수동 크롤링 백그라운드 구동 명령: {' '.join(cmd)} (Python: {python_exe})")
        
        # 비동기로 subprocess 실행 (로그 파일로 출력 저장)
        # Windows 환경에서 한글 깨짐 방지를 위해 PYTHONIOENCODING=utf-8 강제 주입
        import os as _os
        env = _os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        
        with open(log_file_path, "a", encoding="utf-8") as log_f:
            subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=log_f,
                env=env,
                close_fds=True if os.name != 'nt' else False
            )
        
        return {"success": True, "message": f"{brand.upper()} 브랜드 수집(크롤링) 백그라운드 작업이 성공적으로 실행되었습니다. 로그: logs/crawl_{brand}.log"}
    except Exception as e:
        logger.error(f"수동 크롤링 실행 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/crawling/swap")
async def execute_manual_swap(data: dict):
    """
    수동 스위칭 실행
    """
    try:
        brand = data.get("brand", "")
        force = data.get("force", False)
        
        if not brand:
            raise HTTPException(status_code=400, detail="브랜드명이 필요합니다.")
            
        # sys.path 설정하여 base_utils 가져옴
        import sys
        import importlib
        utils_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "data-pipeline", "crawlers", "web_crawlers")
        )
        if utils_dir not in sys.path:
            sys.path.append(utils_dir)
            
        import base_utils
        importlib.reload(base_utils)
        
        # 비동기 함수 실행을 위해 백그라운드 태스크로 구동 또는 동기 래핑
        # 프론트엔드가 결과를 대기하므로 여기서는 바로 await 실행
        success = await base_utils.swap_staging_to_production(brand, force=force)
        
        if success:
            # 스위칭 성공 시 실시간 진행률 JSON 파일이 존재하면 삭제하여 대기 중(Idle)으로 즉시 리셋 유도
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                progress_path = os.path.normpath(
                    os.path.join(base_dir, "..", "..", "..", "..", "logs", f"progress_{brand.lower()}.json")
                )
                if os.path.exists(progress_path):
                    os.remove(progress_path)
            except Exception as file_err:
                logger.warning(f"스위칭 후 진행률 파일 삭제 실패: {file_err}")
                
            return {"success": True, "message": f"{brand.upper()} 브랜드 스테이징 데이터가 활성 운영 DB로 성공적으로 스위칭 이관되었습니다."}
        else:
            return {"success": False, "detail": "스위칭 프로세스 도중 에러가 발생했거나 정합성 검사를 통과하지 못했습니다. 상세 내역은 에러 로그를 확인하세요."}
    except Exception as e:
        logger.error(f"수동 스위칭 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.delete("/crawling/staging/{brand}")
async def clear_staging_data_api(brand: str):
    """
    특정 브랜드의 스테이징 데이터를 비움
    """
    try:
        import sys
        import importlib
        utils_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "data-pipeline", "crawlers", "web_crawlers")
        )
        if utils_dir not in sys.path:
            sys.path.append(utils_dir)
            
        import base_utils
        importlib.reload(base_utils)
        
        # 동기 호출 수행
        base_utils.clear_staging_data(brand)
        
        # 스테이징 비우기 성공 시 실시간 진행률 JSON 파일 삭제하여 Idle 초기화
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            progress_path = os.path.normpath(
                os.path.join(base_dir, "..", "..", "..", "..", "logs", f"progress_{brand.lower()}.json")
            )
            if os.path.exists(progress_path):
                os.remove(progress_path)
        except Exception as file_err:
            logger.warning(f"스테이징 비우기 후 진행률 파일 삭제 실패: {file_err}")
            
        return {"success": True, "message": f"{brand.upper()} 브랜드의 모든 스테이징 데이터 및 Cloudinary 임시 업로드가 초기화되었습니다."}
    except Exception as e:
        logger.error(f"스테이징 비우기 에러: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/crawling/logs")
async def get_crawling_logs(
    run_page: int = Query(1, ge=1),
    run_limit: int = Query(10, ge=1, le=100),
    err_page: int = Query(1, ge=1),
    err_limit: int = Query(10, ge=1, le=100),
    run_id: Optional[int] = Query(None, description="특정 run_id 필터링")
):
    """
    크롤링 구동 내역 및 에러 로그 목록 제공 (admin_crawling.html 용)
    """
    try:
        from ..database import get_dw_cursor
        
        run_offset = (run_page - 1) * run_limit
        err_offset = (err_page - 1) * err_limit
        
        runs = []
        errors = []
        total_runs = 0
        total_errors = 0
        
        with get_dw_cursor() as cur:
            # 1. runs (수동 크롤링/이관 파이프라인만 필터링)
            cur.execute("SELECT count(*) as cnt FROM pipeline_runs WHERE pipeline_name IN ('manual_crawling_pipeline', 'crawling_pipeline', 'swap_pipeline');")
            total_runs = cur.fetchone()["cnt"]
            
            cur.execute(
                """
                WITH numbered_runs AS (
                    SELECT run_id, pipeline_name, brand, status, total_items, new_items, updated_items, embed_count, 
                           started_at, finished_at, duration_sec,
                           ROW_NUMBER() OVER (PARTITION BY CASE WHEN pipeline_name = 'auto_crawling_pipeline' THEN 'auto' ELSE 'manual' END ORDER BY started_at ASC) as display_run_id
                    FROM pipeline_runs
                )
                SELECT run_id, pipeline_name, brand, status, total_items, new_items, updated_items, embed_count, 
                       (SELECT COALESCE(count(*), 0) FROM pipeline_errors pe WHERE pe.run_id = nr.run_id) as error_count, 
                       started_at, finished_at, duration_sec, display_run_id
                FROM numbered_runs nr
                WHERE nr.pipeline_name IN ('manual_crawling_pipeline', 'crawling_pipeline', 'swap_pipeline')
                ORDER BY nr.started_at DESC
                LIMIT %s OFFSET %s;
                """,
                (run_limit, run_offset)
            )
            for r in cur.fetchall():
                runs.append({
                    "run_id": r["run_id"],
                    "display_run_id": r["display_run_id"],
                    "pipeline_name": r["pipeline_name"],
                    "brand": r["brand"],
                    "status": r["status"],
                    "total_items": r["total_items"] or 0,
                    "new_items": r["new_items"] or 0,
                    "updated_items": r["updated_items"] or 0,
                    "embed_count": r["embed_count"] or 0,
                    "error_count": r["error_count"] or 0,
                    "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                    "duration_sec": r["duration_sec"] or 0
                })
                
            # 2. errors (수동 크롤링/이관 파이프라인의 에러만 필터링, run_id 옵션 처리)
            err_where = "WHERE pr.pipeline_name IN ('manual_crawling_pipeline', 'crawling_pipeline', 'swap_pipeline')"
            err_params = []
            if run_id:
                err_where += " AND pe.run_id = %s"
                err_params.append(run_id)

            cur.execute(
                f"""
                SELECT count(*) as cnt 
                FROM pipeline_errors pe
                JOIN pipeline_runs pr ON pe.run_id = pr.run_id
                {err_where};
                """,
                err_params
            )
            total_errors = cur.fetchone()["cnt"]
            
            cur.execute(
                f"""
                WITH numbered_runs AS (
                    SELECT run_id, 
                           ROW_NUMBER() OVER (PARTITION BY CASE WHEN pipeline_name = 'auto_crawling_pipeline' THEN 'auto' ELSE 'manual' END ORDER BY started_at ASC) as display_run_id
                    FROM pipeline_runs
                )
                SELECT pe.error_id, pe.run_id, pe.error_type, pe.error_message, pe.product_id, pe.source_url, pe.created_at, pr.brand, nr.display_run_id
                FROM pipeline_errors pe
                JOIN pipeline_runs pr ON pe.run_id = pr.run_id
                JOIN numbered_runs nr ON pr.run_id = nr.run_id
                {err_where}
                ORDER BY pe.created_at DESC
                LIMIT %s OFFSET %s;
                """,
                err_params + [err_limit, err_offset]
            )
            for r in cur.fetchall():
                errors.append({
                    "error_id": r["error_id"],
                    "run_id": r["run_id"],
                    "display_run_id": r["display_run_id"],
                    "error_type": r["error_type"],
                    "error_message": r["error_message"],
                    "product_id": r["product_id"],
                    "source_url": r["source_url"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "brand": r["brand"]
                })
                
        return {
            "success": True,
            "total_runs": total_runs,
            "total_errors": total_errors,
            "runs": runs,
            "errors": errors
        }
    except Exception as e:
        logger.error(f"크롤링 로그 조회 에러: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/crawling/auto/logs")
async def get_crawling_auto_logs(
    run_page: int = Query(1, ge=1),
    run_limit: int = Query(10, ge=1, le=100),
    err_page: int = Query(1, ge=1),
    err_limit: int = Query(10, ge=1, le=100),
    run_id: Optional[int] = Query(None, description="특정 run_id 필터링")
):
    """
    자동 크롤링 전용 구동 내역 및 에러 로그 목록 제공 (admin_crawling_auto.html 용)
    """
    try:
        from ..database import get_dw_cursor
        
        run_offset = (run_page - 1) * run_limit
        err_offset = (err_page - 1) * err_limit
        
        runs = []
        errors = []
        total_runs = 0
        total_errors = 0
        
        with get_dw_cursor() as cur:
            # 1. runs
            cur.execute("SELECT count(*) as cnt FROM pipeline_runs WHERE pipeline_name = 'auto_crawling_pipeline';")
            total_runs = cur.fetchone()["cnt"]
            
            cur.execute(
                """
                WITH numbered_runs AS (
                    SELECT run_id, pipeline_name, brand, status, total_items, new_items, updated_items, embed_count, error_count,
                           started_at, finished_at, duration_sec,
                           ROW_NUMBER() OVER (PARTITION BY CASE WHEN pipeline_name = 'auto_crawling_pipeline' THEN 'auto' ELSE 'manual' END ORDER BY started_at ASC) as display_run_id
                    FROM pipeline_runs
                )
                SELECT run_id, pipeline_name, brand, status, total_items, new_items, updated_items, embed_count, error_count, started_at, finished_at, duration_sec, display_run_id
                FROM numbered_runs nr
                WHERE nr.pipeline_name = 'auto_crawling_pipeline'
                ORDER BY nr.started_at DESC
                LIMIT %s OFFSET %s;
                """,
                (run_limit, run_offset)
            )
            for r in cur.fetchall():
                runs.append({
                    "run_id": r["run_id"],
                    "display_run_id": r["display_run_id"],
                    "pipeline_name": r["pipeline_name"],
                    "brand": r["brand"],
                    "status": r["status"],
                    "total_items": r["total_items"] or 0,
                    "new_items": r["new_items"] or 0,
                    "updated_items": r["updated_items"] or 0,
                    "embed_count": r["embed_count"] or 0,
                    "error_count": r["error_count"] or 0,
                    "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                    "duration_sec": r["duration_sec"] or 0
                })
                
            # 2. errors (run_id 옵션 처리)
            err_where = "WHERE pr.pipeline_name = 'auto_crawling_pipeline'"
            err_params = []
            if run_id:
                err_where += " AND pe.run_id = %s"
                err_params.append(run_id)
                
            cur.execute(
                f"""
                SELECT count(*) as cnt 
                FROM pipeline_errors pe
                JOIN pipeline_runs pr ON pe.run_id = pr.run_id
                {err_where};
                """,
                err_params
            )
            total_errors = cur.fetchone()["cnt"]
            
            cur.execute(
                f"""
                WITH numbered_runs AS (
                    SELECT run_id, 
                           ROW_NUMBER() OVER (PARTITION BY CASE WHEN pipeline_name = 'auto_crawling_pipeline' THEN 'auto' ELSE 'manual' END ORDER BY started_at ASC) as display_run_id
                    FROM pipeline_runs
                )
                SELECT pe.error_id, pe.run_id, pe.error_type, pe.error_message, pe.product_id, pe.source_url, pe.created_at, pr.brand, pe.stack_trace, sp.prod_name, nr.display_run_id
                FROM pipeline_errors pe
                JOIN pipeline_runs pr ON pe.run_id = pr.run_id
                JOIN numbered_runs nr ON pr.run_id = nr.run_id
                LEFT JOIN staging_products sp ON pe.product_id = sp.product_id
                {err_where}
                ORDER BY pe.created_at DESC
                LIMIT %s OFFSET %s;
                """,
                err_params + [err_limit, err_offset]
            )
            for r in cur.fetchall():
                errors.append({
                    "error_id": r["error_id"],
                    "run_id": r["run_id"],
                    "display_run_id": r["display_run_id"],
                    "error_type": r["error_type"],
                    "error_message": r["error_message"],
                    "product_id": r["product_id"],
                    "source_url": r["source_url"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "brand": r["brand"],
                    "stack_trace": r["stack_trace"] or "",
                    "prod_name": r["prod_name"] or ""
                })
                
        return {
            "success": True,
            "total_runs": total_runs,
            "total_errors": total_errors,
            "runs": runs,
            "errors": errors
        }
    except Exception as e:
        logger.error(f"자동 크롤링 로그 조회 에러: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/crawling/auto/stats")
async def get_crawling_auto_stats():
    """
    자동 크롤링 에러 분석 통계 (에러 유형 및 다빈도 브랜드 분포)
    """
    try:
        from ..database import get_dw_cursor
        
        top_error_types = []
        error_brands = []
        
        with get_dw_cursor() as cur:
            # 1. 다빈도 에러 유형 Top 5
            cur.execute("""
                SELECT pe.error_type, count(*) as cnt
                FROM pipeline_errors pe
                JOIN pipeline_runs pr ON pe.run_id = pr.run_id
                WHERE pr.pipeline_name = 'auto_crawling_pipeline'
                GROUP BY pe.error_type
                ORDER BY cnt DESC
                LIMIT 5;
            """)
            for r in cur.fetchall():
                top_error_types.append({
                    "error_type": r["error_type"],
                    "count": r["cnt"]
                })
                
            # 2. 에러가 잦은 브랜드 목록
            cur.execute("""
                SELECT pr.brand, count(*) as cnt
                FROM pipeline_errors pe
                JOIN pipeline_runs pr ON pe.run_id = pr.run_id
                WHERE pr.pipeline_name = 'auto_crawling_pipeline'
                GROUP BY pr.brand
                ORDER BY cnt DESC;
            """)
            for r in cur.fetchall():
                error_brands.append({
                    "brand": r["brand"],
                    "count": r["cnt"]
                })
                
        return {
            "success": True,
            "top_error_types": top_error_types,
            "error_brands": error_brands
        }
    except Exception as e:
        logger.error(f"자동 크롤링 통계 조회 에러: {e}")
        return {"success": False, "detail": str(e)}


# ─────────────────────────────────────────────────────
# [실시간 진행률 API] 크롤링 파이프라인 현재 진행 상황 조회
# ─────────────────────────────────────────────────────
@router.get("/crawling/progress")
async def get_crawling_progress(brand: str = Query(..., description="브랜드명 (topten, 8seconds 등)")):
    """
    크롤링 파이프라인의 실시간 진행 상황을 반환합니다.
    CLI가 logs/progress_{brand}.json에 기록한 내용을 읽어 반환합니다.
    프론트엔드에서 3초 간격으로 폴링하여 진행 현황 패널을 업데이트합니다.
    """
    import json as _json
    import time as _time

    # 진행률 파일 경로 (CLI와 동일한 위치)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    progress_path = os.path.normpath(
        os.path.join(base_dir, "..", "..", "..", "..", "logs", f"progress_{brand.lower()}.json")
    )

    if not os.path.exists(progress_path):
        return {
            "success": True,
            "found": False,
            "brand": brand.upper(),
            "status": "idle",
            "step": "대기 중",
            "percent": 0,
            "current": 0,
            "total": 0,
            "current_item": "",
            "phases_done": [],
            "phases_remaining": [],
            "elapsed_sec": 0,
            "run_id": None,
            "error": "",
            "started_at": "",
            "updated_at": "",
        }

    try:
        # progress_path의 실제 파일 수정 시간을 읽어 타임존 차이 없는 절대 물리 시간 계산
        st = os.stat(progress_path)
        file_mtime = st.st_mtime
        file_ctime = st.st_ctime
        
        with open(progress_path, "r", encoding="utf-8") as f:
            data = _json.load(f)

        # JSON에 저장된 started_at 시각(로컬 타임)과 현재 백엔드 로컬 타임의 차이로 정확한 경과 시간 계산
        elapsed_sec = 0
        file_elapsed = max(0, int(_time.time() - file_ctime))
        started_at_str = data.get("started_at")
        if started_at_str:
            try:
                started_dt = datetime.strptime(started_at_str, "%Y-%m-%dT%H:%M:%S")
                diff_sec = int((datetime.now() - started_dt).total_seconds())
                # KST/UTC 타임존 미스매치 시 실제 파일 생성 시간(file_ctime) 경과로 안전하게 대체
                if abs(diff_sec - file_elapsed) > 1800:
                    elapsed_sec = file_elapsed
                else:
                    elapsed_sec = max(0, diff_sec)
            except Exception as parse_err:
                logger.warning(f"started_at 파싱 실패: {parse_err}")
                elapsed_sec = file_elapsed
        else:
            elapsed_sec = file_elapsed

        # 마지막 업데이트로부터 300초 이상 지났으면 비정상 종료(stale)로 판단
        stale = False
        now_ts = _time.time()
        if data.get("status") == "running":
            if (now_ts - file_mtime) > 300:
                stale = True

        return {
            "success": True,
            "found": True,
            "brand": data.get("brand", brand.upper()),
            "run_id": data.get("run_id"),
            "status": "stale" if stale else data.get("status", "unknown"),
            "step": data.get("step", ""),
            "percent": data.get("percent", 0),
            "current": data.get("current", 0),
            "total": data.get("total", 0),
            "current_item": data.get("current_item", ""),
            "phases_done": data.get("phases_done", []),
            "phases_remaining": data.get("phases_remaining", []),
            "elapsed_sec": elapsed_sec,
            "error": data.get("error", ""),
            "started_at": data.get("started_at", ""),
            "updated_at": data.get("updated_at", ""),
            "stale": stale,
        }
    except Exception as e:
        logger.error(f"진행률 파일 읽기 에러: {e}")
        return {"success": False, "detail": str(e)}


@router.delete("/crawling/history")
async def clear_crawling_history(
    pipeline_type: str = Query("all", description="초기화할 파이프라인 타입 (manual, auto, all)")
):
    """
    크롤링 구동 내역(runs) 및 에러 로그(errors) 히스토리를 초기화합니다.
    """
    try:
        from ..database import get_dw_cursor
        
        with get_dw_cursor() as cur:
            if pipeline_type == "manual":
                # 수동 파이프라인 대상
                cur.execute("""
                    DELETE FROM pipeline_errors 
                    WHERE run_id IN (
                        SELECT run_id FROM pipeline_runs 
                        WHERE pipeline_name IN ('manual_crawling_pipeline', 'crawling_pipeline', 'swap_pipeline')
                    );
                """)
                cur.execute("""
                    DELETE FROM pipeline_runs 
                    WHERE pipeline_name IN ('manual_crawling_pipeline', 'crawling_pipeline', 'swap_pipeline');
                """)
                msg = "수동 크롤링 및 이관 구동 내역 히스토리가 성공적으로 초기화되었습니다."
            elif pipeline_type == "auto":
                # 자동 파이프라인 대상
                cur.execute("""
                    DELETE FROM pipeline_errors 
                    WHERE run_id IN (
                        SELECT run_id FROM pipeline_runs 
                        WHERE pipeline_name = 'auto_crawling_pipeline'
                    );
                """)
                cur.execute("""
                    DELETE FROM pipeline_runs 
                    WHERE pipeline_name = 'auto_crawling_pipeline';
                """)
                msg = "자동 크롤링 배치 구동 내역 히스토리가 성공적으로 초기화되었습니다."
            else:
                # 전체 대상
                cur.execute("DELETE FROM pipeline_errors;")
                cur.execute("DELETE FROM pipeline_runs;")
                msg = "모든 크롤링 구동 내역 및 에러 로그 히스토리가 성공적으로 초기화되었습니다."
                
        return {"success": True, "message": msg}
    except Exception as e:
        logger.error(f"크롤링 히스토리 초기화 에러: {e}")
        return {"success": False, "detail": str(e)}
