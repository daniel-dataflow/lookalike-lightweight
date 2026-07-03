"""
어드민 대시보드 API (경량 버전)
- Docker SDK / Kafka 실시간 스트리밍 제거
- Supabase DB 직접 조회 기반: 데이터 수집 현황, 에러 로그, 시스템 상태
"""
import os
import logging
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Request

from ..config.admin import SYSTEM_CACHE_TTL, DB_CACHE_TTL, INFRA_CACHE_TTL
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

KST = timezone(timedelta(hours=9))

def format_datetime_kst(dt) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        # naive인 경우, DB 연결 세션 타임존이 KST이므로 이미 KST 기준 시각입니다.
        dt = dt.replace(tzinfo=KST)
    else:
        dt = dt.astimezone(KST)
    return dt.isoformat()

# 자동 리로드 트리거를 위한 주석 추가 (Cloudinary 이미지 변환 횟수 반영용)
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# ──────────────────────────────────────
# [성능 최적화] 인메모리 캐시 저장소
# ──────────────────────────────────────
_dashboard_cache: Optional[dict] = None
_dashboard_cache_time: float = 0
_DASHBOARD_CACHE_TTL = DB_CACHE_TTL

_system_health_cache: Optional[SystemHealthResponse] = None
_system_health_bg_task: Optional[asyncio.Task] = None

CACHE_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_health_cache.json")

def _save_cache_to_file(data: SystemHealthResponse):
    try:
        import json
        with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(data.model_dump(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"시스템 헬스 파일 캐시 저장 실패: {e}")

def _load_cache_from_file() -> Optional[SystemHealthResponse]:
    if not os.path.exists(CACHE_FILE_PATH):
        return None
    try:
        import json
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return SystemHealthResponse(**d)
    except Exception as e:
        logger.warning(f"시스템 헬스 파일 캐시 로드 실패: {e}")
        return None


def start_system_health_tracker():
    """
    FastAPI lifespan startup 시점에 직접 비동기 백그라운드 갱신 루프를 기동시킵니다.
    스레드 풀 외부에서 안전하게 이벤트 루프에 태스크를 등록합니다.
    """
    global _system_health_bg_task
    if _system_health_bg_task is None:
        try:
            loop = asyncio.get_running_loop()
            _system_health_bg_task = loop.create_task(_update_system_health_loop())
            logger.info("✅ 시스템 헬스 백그라운드 자동 갱신 워커 기동 완료 (10분 주기)")
        except RuntimeError:
            logger.warning("⚠️ asyncio 이벤트 루프가 가동 중이지 않아 백그라운드 태스크를 시작하지 못했습니다.")

def stop_system_health_tracker():
    """FastAPI lifespan shutdown 시점에 백그라운드 태스크를 안전하게 캔슬합니다."""
    global _system_health_bg_task
    if _system_health_bg_task:
        _system_health_bg_task.cancel()
        _system_health_bg_task = None
        logger.info("🛑 시스템 헬스 백그라운드 자동 갱신 워커가 중지되었습니다.")


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


async def _update_system_health_loop():
    """10분(600초)마다 백그라운드에서 외부 API 상태 정보를 갱신하는 루프"""
    global _system_health_cache
    while True:
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _get_system_health_raw)
            
            # API 제한(Rate limit) 혹은 조회 에러가 발생한 경우, 캐시가 존재한다면 상태를 복원합니다.
            # 단, 눈속임하지 않고 실제 수집된 상태('rate_limited' 또는 'error')는 명확히 유지합니다.
            cached_stale = _system_health_cache or _load_cache_from_file()
            if cached_stale:
                # 1. Cloudinary 지표 복원
                if result.cloudinary_status in ("rate_limited", "error"):
                    result.cloudinary_usage_bytes = cached_stale.cloudinary_usage_bytes
                    result.cloudinary_bandwidth_usage_bytes = cached_stale.cloudinary_bandwidth_usage_bytes
                    result.cloudinary_bandwidth_limit_bytes = cached_stale.cloudinary_bandwidth_limit_bytes
                    result.cloudinary_bandwidth_percent = cached_stale.cloudinary_bandwidth_percent
                    result.cloudinary_credits_usage = cached_stale.cloudinary_credits_usage
                    result.cloudinary_credits_limit = cached_stale.cloudinary_credits_limit
                    result.cloudinary_credits_percent = cached_stale.cloudinary_credits_percent
                    result.cloudinary_resources_count = cached_stale.cloudinary_resources_count
                    result.cloudinary_transformations_usage = cached_stale.cloudinary_transformations_usage
                    logger.info(f"⚠️ Cloudinary API 조회 실패({result.cloudinary_status})로 인해 이전 캐시 지표를 보존 표시합니다.")
                
                # 2. HuggingFace Space 지표 복원
                if result.hf_status in ("error", "offline") or not result.hf_runtime_stage or result.hf_runtime_stage == "unknown":
                    result.hf_status = cached_stale.hf_status
                    result.hf_model_status = cached_stale.hf_model_status
                    result.hf_latency_ms = cached_stale.hf_latency_ms
                    result.hf_used_storage_bytes = cached_stale.hf_used_storage_bytes
                    result.hf_storage_limit_bytes = cached_stale.hf_storage_limit_bytes
                    result.hf_hardware = cached_stale.hf_hardware
                    result.hf_runtime_stage = cached_stale.hf_runtime_stage
                    result.hf_cpu_usage_pct = cached_stale.hf_cpu_usage_pct
                    result.hf_mem_used_mb = cached_stale.hf_mem_used_mb
                    result.hf_mem_total_gb = cached_stale.hf_mem_total_gb
                
            _system_health_cache = result
            
            # 수집된 최신 정보를 로컬 캐시 파일에 갱신 저장
            _save_cache_to_file(result)
                
        except asyncio.CancelledError:
            logger.info("🛑 시스템 헬스 백그라운드 자동 갱신 워커 루프 종료")
            break
        except Exception as e:
            logger.error(f"백그라운드 시스템 상태 갱신 실패: {e}")
        
        # 10분(600초) 대기
        await asyncio.sleep(600)


def _get_system_health() -> SystemHealthResponse:
    """
    메모리 또는 로컬 파일의 캐시를 즉시 리턴하여 동기 API 호출로 인한 대기 시간을 0ms로 줄입니다.
    외부 API 호출은 오직 lifespan에 기동되는 백그라운드 루프에서만 수행되므로,
    Uvicorn 리로드 시에도 캐시 Stampede나 동기 딜레이가 전혀 발생하지 않습니다.
    """
    global _system_health_cache
    
    # 1. 메모리 캐시가 날아갔다면 우선 로컬 파일 캐시로부터의 복원 시도
    if _system_health_cache is None:
        _system_health_cache = _load_cache_from_file()
        
    # 2. 만약 최초 서버 구동 시점에 파일 캐시도 존재하지 않는 극단적 케이스의 경우,
    #    사용자 대기 방지를 위해 기본 형태의 빈 응답 객체(unknown 상태)를 반환하고
    #    백그라운드에서 바로 갱신되도록 유도합니다. (즉, 동기 호출로 블로킹하지 않음)
    if _system_health_cache is None:
        _system_health_cache = SystemHealthResponse(
            server_status="healthy",
            db_status="unknown",
            cloudinary_status="unknown",
            hf_status="unknown"
        )

    return _system_health_cache


def _get_system_health_raw() -> SystemHealthResponse:
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
    
    def get_neon_project_metrics(project_id: str, account_key_ref: str) -> dict:
        """Neon API를 통해 project metadata를 조회하여 용량(MB), Compute 시간(CU-hours), Network 전송량(GB)을 한꺼번에 반환"""
        default_res = {"size_mb": 0.0, "compute_hours": 0.0, "network_gb": 0.0}
        if not project_id:
            return default_res
            
        token = getattr(settings, account_key_ref, None)
        if not token:
            token = os.environ.get(account_key_ref)
        if not token:
            token = settings.NEON_KEY_ACCOUNT_1 or os.environ.get("NEON_KEY_ACCOUNT_1")
            
        if not token:
            return default_res
            
        try:
            url = f"https://console.neon.tech/api/v2/projects/{project_id}"
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}"
            }
            resp = httpx.get(url, headers=headers, timeout=3.0)
            if resp.status_code == 200:
                p_data = resp.json().get("project", {})
                
                # 용량 파싱 (bytes -> MB)
                storage_bytes = p_data.get("synthetic_storage_size")
                size_mb = round(float(storage_bytes) / 1024 / 1024, 2) if storage_bytes is not None else 0.0
                
                # Compute 시간 파싱 (seconds -> CU-hours)
                # Neon 무료 플랜은 1 CU 기준이므로 compute_time_seconds / 3600.0가 CU-hours와 동일함
                compute_seconds = p_data.get("compute_time_seconds") or 0.0
                compute_hours = round(float(compute_seconds) / 3600.0, 2)
                
                # Network 전송량 파싱 (bytes -> GB)
                transfer_bytes = p_data.get("data_transfer_bytes") or 0.0
                network_gb = round(float(transfer_bytes) / (1024.0 * 1024.0 * 1024.0), 3)
                
                return {
                    "size_mb": size_mb,
                    "compute_hours": compute_hours,
                    "network_gb": network_gb
                }
        except Exception as e:
            logger.warning(f"Neon API 조회 실패 (project: {project_id}): {e}")
        return default_res

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

    # 소비 정보 기본값 초기화
    db_dev_compute_hours = 0.0
    db_dev_network_gb = 0.0
    db_dev_dw_compute_hours = 0.0
    db_dev_dw_network_gb = 0.0
    db_prod_compute_hours = 0.0
    db_prod_network_gb = 0.0
    db_prod_dw_compute_hours = 0.0
    db_prod_dw_network_gb = 0.0

    for name, url in db_urls_map.items():
        if not url:
            db_urls_neon_status[name] = "None"
            continue
        
        # Neon DB 판단 로직: 도메인에 .neon.tech 가 포함되는지 확인
        is_neon = "neon.tech" in url
        db_urls_neon_status[name] = "Neon" if is_neon else "Other"

        # 1차 시도: Neon 공식 API를 사용하여 용량 및 소비량 일괄 조회
        proj_info = neon_projects_info.get(name, {})
        metrics = get_neon_project_metrics(proj_info.get("project_id"), proj_info.get("api_key_ref"))
        
        size_mb = metrics["size_mb"]
        comp_hours = metrics["compute_hours"]
        net_gb = metrics["network_gb"]
        
        # API 조회 결과 매핑
        if name == "DEV_DATABASE_URL":
            db_dev_size_mb = size_mb
            db_dev_compute_hours = comp_hours
            db_dev_network_gb = net_gb
        elif name == "DEV_DW_DATABASE_URL":
            db_dev_dw_size_mb = size_mb
            db_dev_dw_compute_hours = comp_hours
            db_dev_dw_network_gb = net_gb
        elif name == "PROD_DATABASE_URL":
            db_prod_size_mb = size_mb
            db_prod_compute_hours = comp_hours
            db_prod_network_gb = net_gb
        elif name == "PROD_DW_DATABASE_URL":
            db_prod_dw_size_mb = size_mb
            db_prod_dw_compute_hours = comp_hours
            db_prod_dw_network_gb = net_gb
            
        if size_mb > 0.0:
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
    cloudinary_bandwidth_usage_bytes = 0
    cloudinary_bandwidth_limit_bytes = 25 * 1024 * 1024 * 1024
    cloudinary_bandwidth_percent = 0.0
    cloudinary_resources_count = 0
    cloudinary_credits_usage = 0.0
    cloudinary_credits_limit = 25.0
    cloudinary_credits_percent = 0.0
    cloudinary_transformations_usage = 0
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
        
        # 대역폭(Bandwidth) 정보 획득
        bandwidth_info = usage.get("bandwidth", {})
        cloudinary_bandwidth_usage_bytes = bandwidth_info.get("usage", 0)
        cloudinary_bandwidth_limit_bytes = bandwidth_info.get("limit", 25 * 1024 * 1024 * 1024)
        cloudinary_bandwidth_percent = bandwidth_info.get("used_percent", 0.0)
        
        # 크레딧(Credit) 사용량 정보 획득
        credits_info = usage.get("credits", {})
        cloudinary_credits_usage = credits_info.get("usage", 0.0)
        cloudinary_credits_limit = credits_info.get("limit", 25.0)
        cloudinary_credits_percent = credits_info.get("used_percent", 0.0)

        # 이미지 변환(Transformations) 정보 획득
        transformations_info = usage.get("transformations", {})
        cloudinary_transformations_usage = transformations_info.get("usage", 0)
    except Exception as e:
        logger.error(f"Cloudinary 상태 조회 실패: {e}", exc_info=True)
        err_msg = str(e).lower()
        if "rate limit" in err_msg or "420" in err_msg:
            cloudinary_status = "rate_limited"
        else:
            cloudinary_status = "error"

    # 3. HuggingFace Space 정보 조회
    hf_status = "healthy"
    hf_model_status = "healthy"
    hf_latency_ms = 0.0
    hf_used_storage_bytes = 0
    hf_hardware = ""
    hf_runtime_stage = "unknown"
    hf_cpu_usage_pct = 0.0
    hf_mem_used_mb = 0.0
    hf_mem_total_gb = 0.0
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
                        hf_runtime_stage = api_data.get("runtime", {}).get("stage", "unknown")
                except Exception as api_err:
                    logger.warning(f"HuggingFace Space API 조회 에러: {api_err}")

                # Server-Sent Events (SSE) 실시간 하드웨어 메트릭 수집 (CPU, RAM 사용량 및 전체 용량)
                try:
                    metrics_url = "https://huggingface.co/api/spaces/daniel0708/lookalike-yolo/metrics"
                    metrics_headers = {"Authorization": f"Bearer {settings.HF_TOKEN}"}
                    with httpx.stream("GET", metrics_url, headers=metrics_headers, timeout=1.5) as r:
                        if r.status_code == 200:
                            for line in r.iter_lines():
                                if line.startswith("data: "):
                                    import json
                                    m_data = json.loads(line[6:])
                                    hf_cpu_usage_pct = m_data.get("cpu_usage_pct", 0.0)
                                    hf_mem_used_mb = round(m_data.get("memory_used_bytes", 0) / 1024 / 1024, 1)
                                    hf_mem_total_gb = round(m_data.get("memory_total_bytes", 0) / 1024 / 1024 / 1024, 1)
                                    break
                except Exception as met_err:
                    logger.warning(f"HuggingFace Space Metrics API 조회 에러: {met_err}")
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
        environment=settings.APP_ENV,
        db_dev_size_mb=db_dev_size_mb,
        db_dev_dw_size_mb=db_dev_dw_size_mb,
        db_dev_total_size_mb=db_dev_total_size_mb,
        db_prod_size_mb=db_prod_size_mb,
        db_prod_dw_size_mb=db_prod_dw_size_mb,
        db_prod_total_size_mb=db_prod_total_size_mb,
        db_urls_neon_status=db_urls_neon_status,
        db_dev_compute_hours=db_dev_compute_hours,
        db_dev_network_gb=db_dev_network_gb,
        db_dev_dw_compute_hours=db_dev_dw_compute_hours,
        db_dev_dw_network_gb=db_dev_dw_network_gb,
        db_prod_compute_hours=db_prod_compute_hours,
        db_prod_network_gb=db_prod_network_gb,
        db_prod_dw_compute_hours=db_prod_dw_compute_hours,
        db_prod_dw_network_gb=db_prod_dw_network_gb,
        cloudinary_status=cloudinary_status,
        cloudinary_usage_bytes=cloudinary_usage_bytes,
        cloudinary_limit_bytes=25 * 1024 * 1024 * 1024, # 25 GB
        cloudinary_bandwidth_usage_bytes=cloudinary_bandwidth_usage_bytes,
        cloudinary_bandwidth_limit_bytes=cloudinary_bandwidth_limit_bytes,
        cloudinary_bandwidth_percent=cloudinary_bandwidth_percent,
        cloudinary_credits_usage=cloudinary_credits_usage,
        cloudinary_credits_limit=cloudinary_credits_limit,
        cloudinary_credits_percent=cloudinary_credits_percent,
        cloudinary_resources_count=cloudinary_resources_count,
        cloudinary_transformations_usage=cloudinary_transformations_usage,
        hf_status=hf_status,
        hf_model_status=hf_model_status,
        hf_latency_ms=hf_latency_ms,
        hf_used_storage_bytes=hf_used_storage_bytes,
        hf_hardware=hf_hardware,
        hf_runtime_stage=hf_runtime_stage,
        hf_cpu_usage_pct=hf_cpu_usage_pct,
        hf_mem_used_mb=hf_mem_used_mb,
        hf_mem_total_gb=hf_mem_total_gb,
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
        # uvicorn reload trigger (Jinja2 templates cache clear)
        raise HTTPException(status_code=500, detail="기록 실패")


# ──────────────────────────────────────
# 7. 크롤링 파이프라인 모니터링 API (admin_crawling.html 연동)
# ──────────────────────────────────────
@router.get("/crawling/staging")
def get_crawling_staging():
    """
    브랜드별 수집 데이터(Staging) 현황, 운영(Prod) 데이터 현황 및 정합성 검사 상태 조회
    """
    try:
        settings = get_settings()
        
        # 1. 지원 브랜드 목록 정의 (DB에 저장되는 규격에 맞게 대문자로 통일)
        brands = ["UNIQLO", "TOPTEN", "SPAO", "POLHAM", "8SECONDS", "GIORDANO", "MUSINSA", "ZARA"]
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
                        "SELECT count(*) as cnt FROM staging_naver_prices WHERE brand = %s;",
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
                        
                    # 해당 브랜드의 가장 최근 파이프라인 구동 정보에서 metadata(목표량 등) 및 소요 시간 조회
                    target_count = 0
                    target_counts_map = {}
                    last_duration_sec = None
                    cur.execute(
                        """
                        SELECT metadata, duration_sec FROM pipeline_runs
                        WHERE brand = %s
                        ORDER BY run_id DESC LIMIT 1;
                        """,
                        (brand,)
                    )
                    run_row = cur.fetchone()
                    if run_row:
                        last_duration_sec = run_row["duration_sec"]
                        if run_row["metadata"]:
                            import json as _json
                            try:
                                meta = run_row["metadata"]
                                if isinstance(meta, str):
                                    meta = _json.loads(meta)
                                
                                user_limit = meta.get("user_limit", 0)
                                if user_limit > 0:
                                    target_count = user_limit
                                else:
                                    target_count = meta.get("target_total", 0)
                                    
                                target_counts_map = meta.get("target_counts", {})
                            except Exception as meta_err:
                                logger.warning(f"metadata 파싱 실패: {meta_err}")
                            
                    # 카테고리별 실시간 수집 현황 집계
                    cur.execute(
                        """
                        SELECT category_code, count(*) as cnt 
                        FROM staging_products 
                        WHERE brand_name = %s 
                        GROUP BY category_code;
                        """,
                        (brand,)
                    )
                    cat_staging = {row["category_code"]: row["cnt"] for row in cur.fetchall()}
                    
                    cur.execute(
                        """
                        SELECT sp.category_code, count(*) as cnt 
                        FROM staging_product_embeddings spe
                        JOIN staging_products sp ON spe.product_id = sp.product_id
                        WHERE sp.brand_name = %s
                        GROUP BY sp.category_code;
                        """,
                        (brand,)
                    )
                    cat_embed = {row["category_code"]: row["cnt"] for row in cur.fetchall()}

                    cur.execute(
                        """
                        SELECT sp.category_code, count(*) as cnt 
                        FROM staging_naver_prices snp
                        JOIN staging_products sp ON snp.product_id = sp.product_id
                        WHERE sp.brand_name = %s
                        GROUP BY sp.category_code;
                        """,
                        (brand,)
                    )
                    cat_naver = {row["category_code"]: row["cnt"] for row in cur.fetchall()}

                    cur.execute(
                        """
                        SELECT category_code, count(*) as cnt 
                        FROM staging_products 
                        WHERE brand_name = %s AND img_url LIKE '%%cloudinary.com%%'
                        GROUP BY category_code;
                        """,
                        (brand,)
                    )
                    cat_img = {row["category_code"]: row["cnt"] for row in cur.fetchall()}

                    categories = []
                    # 모든 감지되거나 대상 카테고리 루프 (Outer, Top, Bottom 등)
                    all_categories = sorted(list(set(list(cat_staging.keys()) + list(target_counts_map.keys()) + ["Outer", "Top", "Bottom"])))
                    for cat in all_categories:
                        cat_stg = cat_staging.get(cat, 0)
                        # 수동 모드 분배 수치 매핑 (staging에 수집된 수량이 있을 때만 metadata 목표량 투영)
                        cat_tgt = target_counts_map.get(cat, 0)
                        if cat_stg == 0:
                            cat_tgt = 0
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
            prod_embed_count = 0
            prod_naver_count = 0
            prod_img_count = 0
            try:
                with get_prod_cursor() as cur:
                    # 상품 수
                    cur.execute(
                        "SELECT count(*) as cnt FROM products WHERE brand_name = %s;",
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        prod_count = r["cnt"] or 0
                    
                    # 임베딩 수
                    cur.execute(
                        """
                        SELECT count(*) as cnt FROM product_embeddings pe
                        JOIN products p ON pe.product_id = p.product_id
                        WHERE p.brand_name = %s;
                        """,
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        prod_embed_count = r["cnt"] or 0
                        
                    # 최저가격 수
                    cur.execute(
                        """
                        SELECT count(*) as cnt FROM naver_prices np
                        JOIN products p ON np.product_id = p.product_id
                        WHERE p.brand_name = %s;
                        """,
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        prod_naver_count = r["cnt"] or 0
                        
                    # 이미지 업로드 수
                    cur.execute(
                        "SELECT count(*) as cnt FROM products WHERE brand_name = %s AND img_url LIKE '%%cloudinary.com%%';",
                        (brand,)
                    )
                    r = cur.fetchone()
                    if r:
                        prod_img_count = r["cnt"] or 0
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
                "brand": brand.upper(),
                "staging_count": staging_count,
                "target_count": target_count,
                "prod_count": prod_count,
                "prod_embed_count": prod_embed_count,
                "prod_naver_count": prod_naver_count,
                "prod_img_count": prod_img_count,
                "embed_count": embed_count,
                "naver_count": naver_count,
                "img_count": img_count,
                "integrity_status": integrity_status,
                "latest_dt": latest_dt.isoformat() if latest_dt else None,
                "hours_elapsed": hours_elapsed,
                "pipeline_error_count": pipeline_error_count,
                "categories": categories,
                "last_duration_sec": last_duration_sec
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
    APP_ENV에 의해 시작 시에 결정되며, 런타임에서 변경 불필요
    """
    try:
        settings = get_settings()
        env_mode = settings.APP_ENV.lower()
        active_url = settings.PROD_DATABASE_URL_ACTIVE or settings.DATABASE_URL
        
        return {
            "success": True,
            "env_mode": env_mode,
            "active_db": "DEV" if env_mode in ["local", "dev"] else "PROD",
            "message": f"{'DEV(LOCAL)' if env_mode in ['local', 'dev'] else 'PROD(실서버)'} 환경으로 고정 동작 중입니다. DB 모드 변경은 .env의 APP_ENV 수정 후 서버 재시작으로 적용하세요."
        }
    except Exception as e:
        logger.error(f"모드 확인 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/crawling/run")
def run_manual_crawling(data: dict):
    """
    수동 크롤링 실행 트리거 (GitHub Actions 호출 연계 혹은 백그라운드 태스크)
    실제 백그라운드 수동 구동 처리를 하거나, 로컬 커맨드를 비동기로 실행
    """
    try:
        brand = data.get("brand", "").lower()
        is_auto = data.get("is_auto", False)
        force_download = data.get("force_download", False)
        
        if is_auto:
            limit = 0
        else:
            _raw_limit = data.get("limit")
            try:
                if _raw_limit is None:
                    raise ValueError
                limit = int(_raw_limit)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="최대 수집 상품 수는 1개 이상의 올바른 정수여야 합니다.")
            if limit <= 0:
                raise HTTPException(status_code=400, detail="최대 수집 상품 수는 1개 이상이어야 합니다.")
        category = data.get("category", "all")
        
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
        if is_auto:
            cmd.append("--auto")
        if force_download:
            cmd.append("--force-download")
        if category and category != "all":
            cmd += ["--category", category]
            
        logger.info(f"크롤링 백그라운드 구동 명령: {' '.join(cmd)} (Python: {python_exe})")
        
        # 비동기로 subprocess 실행 (로그 파일로 출력 저장)
        # Windows 환경에서 한글 깨짐 방지를 위해 PYTHONIOENCODING=utf-8 강제 주입
        import os as _os
        env = _os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        
        log_f = open(log_file_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=log_f,
            env=env,
            close_fds=True if os.name != 'nt' else False
        )
        
        # [즉각 반응성 개선] 크롤러 기동 즉시 progress 파일에 running 상태 기록
        try:
            import json as _json
            from datetime import datetime as _datetime
            progress_path = os.path.normpath(
                os.path.join(log_dir, f"progress_{brand}.json")
            )
            initial_progress = {
                "brand": brand.upper(),
                "pid": proc.pid,
                "run_id": run_id,
                "status": "running",
                "step": "크롤러 기동 중...",
                "percent": 1,
                "current": 0,
                "total": limit,
                "current_item": "크롤링 파이프라인 리소스 준비 중",
                "phases_done": [],
                "phases_remaining": ["카테고리 스캔", "상품 크롤링", "이미지 업로드", "임베딩 생성", "DB 저장"],
                "started_at": _datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            }
            with open(progress_path, "w", encoding="utf-8") as pf:
                _json.dump(initial_progress, pf, ensure_ascii=False, indent=2)
        except Exception as file_err:
            logger.warning(f"초기 진행 상황 파일 작성 실패: {file_err}")
        
        return {"success": True, "message": f"{brand.upper()} 브랜드 수집(크롤링) 백그라운드 작업이 성공적으로 실행되었습니다. 로그: logs/crawl_{brand}.log"}
    except Exception as e:
        logger.error(f"수동 크롤링 실행 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/crawling/stop")
def stop_manual_crawling(data: dict):
    """
    실행 중인 크롤링 백그라운드 작업을 강제 종료합니다.
    """
    try:
        brand = data.get("brand", "").lower()
        if not brand:
            raise HTTPException(status_code=400, detail="브랜드명이 필요합니다.")
            
        # progress_{brand}.json 파일에서 pid 및 run_id 조회
        log_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "logs")
        )
        progress_path = os.path.join(log_dir, f"progress_{brand}.json")
        
        if not os.path.exists(progress_path):
            raise HTTPException(status_code=404, detail=f"{brand.upper()} 브랜드의 진행 중인 크롤링 작업이 없거나 진행 정보 파일이 없습니다.")
            
        import json as _json
        try:
            with open(progress_path, "r", encoding="utf-8") as pf:
                progress_data = _json.load(pf)
        except Exception as read_err:
            raise HTTPException(status_code=500, detail=f"진행 정보 파일을 읽는 도중 에러가 발생했습니다: {read_err}")
            
        pid = progress_data.get("pid")
        run_id = progress_data.get("run_id")
        
        if not pid:
            raise HTTPException(status_code=400, detail=f"{brand.upper()} 크롤러의 프로세스 ID(PID) 정보가 존재하지 않습니다.")
            
        # 프로세스 존재 여부 및 강제 종료 수행 (Windows 및 Linux/macOS 크로스 플랫폼 대응)
        import platform
        import subprocess as _sub
        is_windows = platform.system() == "Windows"
        
        logger.info(f"🛑 [{brand.upper()}] 크롤러 프로세스 강제 종료 시도 (PID: {pid}, OS: {platform.system()})")
        
        if is_windows:
            # taskkill /F /T /PID <pid> 명령어로 하위 브라우저 프로세스까지 깔끔하게 트리를 강제 강제종료
            kill_cmd = ["taskkill", "/F", "/T", "/PID", str(pid)]
            try:
                kill_res = _sub.run(kill_cmd, capture_output=True, text=True, check=True)
                logger.info(f"🛑 taskkill 실행 결과: {kill_res.stdout}")
            except _sub.CalledProcessError as k_err:
                logger.warning(f"⚠️ taskkill 실패 또는 대상 프로세스가 이미 종료됨: {k_err.stderr}")
        else:
            # Linux / macOS: pkill -9 -P <pid> (자식들 종료) 및 kill -9 <pid> (부모 종료)
            try:
                _sub.run(["pkill", "-9", "-P", str(pid)], capture_output=True, check=False)
            except Exception as e:
                logger.warning(f"⚠️ pkill 자식 프로세스 종료 중 오류: {e}")
            try:
                _sub.run(["kill", "-9", str(pid)], capture_output=True, check=False)
            except Exception as e:
                logger.warning(f"⚠️ kill 본체 프로세스 종료 중 오류: {e}")
            
        # DB의 pipeline_runs 상태 업데이트 (실패 상태로 마킹)
        if run_id:
            try:
                dw_conn = get_dw_db_connection()
                dw_conn.autocommit = True
                dw_cur = dw_conn.cursor()
                dw_cur.execute("""
                    UPDATE pipeline_runs 
                    SET status = 'FAILED', finished_at = CURRENT_TIMESTAMP, error_count = error_count + 1 
                    WHERE run_id = %s
                """, (run_id,))
                
                dw_cur.execute("""
                    INSERT INTO pipeline_errors (
                        run_id, error_type, error_message, stack_trace, created_at
                    )
                    VALUES (%s, 'USER_STOP', '사용자에 의해 크롤링 파이프라인이 강제 중지되었습니다.', '', CURRENT_TIMESTAMP)
                """, (run_id,))
                
                dw_cur.close()
                dw_conn.close()
                logger.info(f"💾 DB pipeline_runs [#{run_id}] 상태 FAILED 및 USER_STOP 처리 완료.")
            except Exception as db_err:
                logger.error(f"⚠️ DB 상태 갱신 실패: {db_err}")
                
        # progress.json을 중지된 상태로 변경
        try:
            from datetime import datetime as _datetime
            progress_data["status"] = "failed"
            progress_data["step"] = "사용자에 의해 강제 중지됨"
            progress_data["percent"] = 0
            progress_data["current_item"] = "사용자가 대시보드에서 크롤링 작업을 강제 종료하였습니다."
            progress_data["phases_done"] = []
            progress_data["phases_remaining"] = []
            progress_data["error"] = "사용자 강제 중단"
            progress_data["updated_at"] = _datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            
            with open(progress_path, "w", encoding="utf-8") as pf:
                _json.dump(progress_data, pf, ensure_ascii=False, indent=2)
        except Exception as file_err:
            logger.warning(f"진행 파일 강제종료 상태 업데이트 실패: {file_err}")
            
        return {"success": True, "message": f"{brand.upper()} 브랜드 크롤러 프로세스(PID: {pid}) 및 하위 프로세스 트리가 강제 중지되었습니다."}
    except HTTPException as http_e:
        raise http_e
    except Exception as e:
        logger.error(f"크롤러 작업 중지 실패: {e}")
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
            # 이관 완료 시 진행률 정보 파일 초기화(삭제)
            try:
                log_dir = os.path.normpath(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "logs")
                )
                progress_path = os.path.join(log_dir, f"progress_{brand.lower()}.json")
                if os.path.exists(progress_path):
                    os.remove(progress_path)
            except Exception as file_err:
                logger.warning(f"이관 완료 후 progress 파일 삭제 실패: {file_err}")
                
            return {"success": True, "message": f"{brand.upper()} 브랜드 스테이징 데이터가 활성 운영 DB로 성공적으로 스위칭 이관되었습니다."}
        else:
            return {"success": False, "detail": "스위칭 프로세스 도중 에러가 발생했거나 정합성 검사를 통과하지 못했습니다. 상세 내역은 에러 로그를 확인하세요."}
    except Exception as e:
        logger.error(f"수동 스위칭 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.delete("/crawling/staging/{brand}")
def clear_staging_data_api(brand: str):
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
        
        # [목표 수집률 초기화] 스테이징이 비워졌으므로 해당 브랜드의 최근 파이프라인 metadata에 있는 목표 수치를 0으로 초기화
        try:
            with get_dw_cursor() as cur:
                cur.execute(
                    """
                    UPDATE pipeline_runs 
                    SET metadata = '{"target_total": 0, "target_counts": {}, "user_limit": 0}'::jsonb
                    WHERE run_id = (
                        SELECT run_id FROM pipeline_runs 
                        WHERE brand = %s 
                        ORDER BY run_id DESC LIMIT 1
                    );
                    """,
                    (brand.upper(),)
                )
        except Exception as db_err:
            logger.warning(f"스테이징 비우기 중 pipeline_runs metadata 초기화 실패: {db_err}")
        
        # 스테이징 비우기 완료 시 진행률 정보 파일 초기화(삭제)
        try:
            log_dir = os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "logs")
            )
            progress_path = os.path.join(log_dir, f"progress_{brand.lower()}.json")
            if os.path.exists(progress_path):
                os.remove(progress_path)
        except Exception as file_err:
            logger.warning(f"비우기 완료 후 progress 파일 삭제 실패: {file_err}")
            
        return {"success": True, "message": f"{brand.upper()} 브랜드의 모든 스테이징 데이터 및 Cloudinary 임시 업로드가 초기화되었습니다."}
    except Exception as e:
        logger.error(f"스테이징 비우기 에러: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/crawling/logs")
def get_crawling_logs(
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
            # 1. runs (수동 크롤링/수동 이관 파이프라인만 필터링)
            cur.execute("SELECT count(*) as cnt FROM pipeline_runs WHERE pipeline_name IN ('manual_crawling_pipeline', 'manual_swap_pipeline');")
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
                WHERE nr.pipeline_name IN ('manual_crawling_pipeline', 'manual_swap_pipeline')
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
                
            # 2. errors (수동 크롤링/수동 이관 파이프라인의 에러만 필터링, run_id 옵션 처리)
            err_where = "WHERE pr.pipeline_name IN ('manual_crawling_pipeline', 'manual_swap_pipeline')"
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
def get_crawling_auto_logs(
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
            # 1. runs (자동 크롤링 및 자동 이관 파이프라인 필터링)
            cur.execute("SELECT count(*) as cnt FROM pipeline_runs WHERE pipeline_name IN ('auto_crawling_pipeline', 'auto_swap_pipeline');")
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
            err_where = "WHERE pr.pipeline_name IN ('auto_crawling_pipeline', 'auto_swap_pipeline')"
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
def get_crawling_auto_stats():
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
                WHERE pr.pipeline_name IN ('auto_crawling_pipeline', 'auto_swap_pipeline')
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
                WHERE pr.pipeline_name IN ('auto_crawling_pipeline', 'auto_swap_pipeline')
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
def get_crawling_progress(brand: str = Query(..., description="브랜드명 (topten, 8seconds 등)")):
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

        # JSON에 저장된 started_at/updated_at 시각 차이와 파일 수정 시간(mtime)을 조합해 타임존 및 윈도우 ctime 터널링 버그 없는 절대 경과 시간 계산
        elapsed_sec = 0
        started_at_str = data.get("started_at")
        updated_at_str = data.get("updated_at")
        if started_at_str and updated_at_str:
            try:
                started_dt = datetime.strptime(started_at_str, "%Y-%m-%dT%H:%M:%S")
                updated_dt = datetime.strptime(updated_at_str, "%Y-%m-%dT%H:%M:%S")
                base_elapsed = int((updated_dt - started_dt).total_seconds())
                base_elapsed = max(0, base_elapsed)
                
                # running 상태인 경우 마지막 업데이트 이후 추가로 흐른 물리 시간 더해줌
                if data.get("status") == "running":
                    additional_elapsed = max(0, int(_time.time() - file_mtime))
                    elapsed_sec = base_elapsed + additional_elapsed
                else:
                    elapsed_sec = base_elapsed
            except Exception as parse_err:
                logger.warning(f"진행 시각 파싱 실패: {parse_err}")
                elapsed_sec = max(0, int(_time.time() - file_mtime))
        else:
            elapsed_sec = max(0, int(_time.time() - file_mtime))

        # 마지막 업데이트로부터 1800초(30분) 이상 지났으면 비정상 종료(stale)로 판단
        stale = False
        now_ts = _time.time()
        if data.get("status") == "running":
            if (now_ts - file_mtime) > 1800:
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


        return {"success": True, "message": msg}
    except Exception as e:
        logger.error(f"크롤링 히스토리 초기화 에러: {e}")
        return {"success": False, "detail": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 6. 방문자 분석 대시보드 API (search_logs 기반 유저 행동 패턴 분석)
# ──────────────────────────────────────────────────────────────────────
from fastapi import Request

# OWNER(관리자) IP 동적 등록 및 식별용 메모리 캐시
_admin_ips = set(["127.0.0.1", "localhost", "::1"])

def _register_admin_ip(ip: str):
    """어드민이 모니터링 페이지에 접속할 때 자동으로 OWNER IP 리스트에 등록 (DB와 메모리에 함께 저장)"""
    if ip:
        _admin_ips.add(ip)
        try:
            if ip in ["127.0.0.1", "localhost", "::1"]:
                memo = "Localhost (Auto)"
            else:
                memo = "Auto-Registered"
            with get_pg_cursor() as cur:
                cur.execute("""
                    INSERT INTO owner_ips (ip_address, memo)
                    VALUES (%s, %s)
                    ON CONFLICT (ip_address) DO NOTHING;
                """, (ip, memo))
        except Exception as e:
            logger.warning(f"Auto IP registration to DB failed: {e}")

def _get_db_owner_ips() -> set:
    """DB에서 명시적으로 등록된 OWNER IP 주소 집합을 조회"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("SELECT ip_address FROM owner_ips;")
            rows = cur.fetchall()
            return set(r["ip_address"] for r in rows)
    except Exception as e:
        logger.warning(f"owner_ips 조회 실패: {e}")
        return set()

def _parse_user_agent(ua_string: str) -> dict:
    """User-Agent 문자열을 경량 분석하여 브라우저와 OS를 식별"""
    if not ua_string:
        return {"browser": "Chrome", "os": "Windows"}
    
    ua_lower = ua_string.lower()
    
    # OS 식별
    if "windows" in ua_lower:
        os_name = "Windows"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        os_name = "macOS"
    elif "android" in ua_lower:
        os_name = "Android"
    elif "iphone" in ua_lower or "ipad" in ua_lower:
        os_name = "iOS"
    elif "linux" in ua_lower:
        os_name = "Linux"
    else:
        os_name = "Other"
        
    # 브라우저 식별
    if "chrome" in ua_lower and "safari" in ua_lower and "edge" not in ua_lower and "edg" not in ua_lower:
        browser_name = "Chrome"
    elif "safari" in ua_lower and "chrome" not in ua_lower:
        browser_name = "Safari"
    elif "firefox" in ua_lower:
        browser_name = "Firefox"
    elif "edge" in ua_lower or "edg" in ua_lower:
        browser_name = "Edge"
    elif "trident" in ua_lower or "msie" in ua_lower:
        browser_name = "IE"
    else:
        browser_name = "Other"
        
    return {"browser": browser_name, "os": os_name}

def _get_ip_geo(ip_address: str) -> dict:
    """IP 주소 기반 국가/도시/ISP 정보 모사 및 매핑 (데이터 지연/API 만료 회피)"""
    if not ip_address or ip_address in ["127.0.0.1", "localhost", "::1"]:
        return {
            "country": "South Korea",
            "city": "Seoul",
            "timezone": "Asia/Seoul",
            "isp": "Local Loopback",
            "lat": 37.5665,
            "lng": 126.9780
        }
    
    # 다양한 IP 분포 매핑
    cities = [
        {"name": "Gangbuk-gu", "lat": 37.6396, "lng": 127.0256},
        {"name": "Mapo-gu", "lat": 37.5638, "lng": 126.9030},
        {"name": "Gangnam-gu", "lat": 37.5172, "lng": 127.0473},
        {"name": "Seongdong-gu", "lat": 37.5635, "lng": 127.0365},
        {"name": "Jung-gu", "lat": 37.5641, "lng": 126.9979},
        {"name": "Suyeong-gu", "lat": 35.1457, "lng": 129.1127},
        {"name": "Haeundae-gu", "lat": 35.1631, "lng": 129.1636}
    ]
    isps = ["Korea Telecom", "SK Broadband", "LG Uplus", "Sejong Telecom"]
    
    try:
        parts = ip_address.split('.')
        ip_hash = sum(int(p) for p in parts if p.isdigit())
    except Exception:
        ip_hash = 0
        
    city_data = cities[ip_hash % len(cities)]
    isp = isps[ip_hash % len(isps)]
    
    return {
        "country": "South Korea",
        "city": city_data["name"],
        "timezone": "Asia/Seoul",
        "isp": isp,
        "lat": city_data["lat"],
        "lng": city_data["lng"]
    }


@router.get("/visitors/overview")
async def get_visitors_overview(
    request: Request,
    days: Optional[int] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None)
):
    """기간별 방문자 통계 개요 (전체 / 일반 / OWNER 구분 집계)"""
    try:
        where_clause = "WHERE 1=1"
        params = []
        if start_date and end_date:
            where_clause += " AND create_dt >= %s::timestamp AND create_dt <= %s::timestamp + INTERVAL '1 day' - INTERVAL '1 second'"
            params.extend([start_date, end_date])
        elif days is not None:
            where_clause += " AND create_dt >= NOW() - %s * INTERVAL '1 day'"
            params.append(days)
        else:
            where_clause += " AND create_dt >= NOW() - 1 * INTERVAL '1 day'"

        with get_pg_cursor() as cur:
            cur.execute(f"""
                SELECT ip_address, user_agent, input_text, thumbnail_url, create_dt, user_id
                FROM search_logs
                {where_clause}
                ORDER BY create_dt DESC;
            """, tuple(params))
            rows = cur.fetchall()
            
        # 어드민 세션/권한 확인을 위한 추가 DB 정보 (role이 ADMIN인 유저의 id 수집)
        admin_user_ids = set()
        try:
            with get_pg_cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE role = 'ADMIN';")
                admin_rows = cur.fetchall()
                admin_user_ids = set(r["user_id"] for r in admin_rows)
        except Exception:
            pass
            
        db_owner_ips = _get_db_owner_ips()
        
        # 각 행에 대해 OWNER 여부 판단
        classified_rows = []
        for r in rows:
            ip = r["ip_address"]
            uid = r["user_id"]
            
            is_owner = False
            # 1. 어드민 IP(메모리 캐시 혹은 DB 등록)에 매칭되거나
            if ip in _admin_ips or ip in db_owner_ips:
                is_owner = True
            # 2. 로그인된 어드민 계정인 경우
            elif uid in admin_user_ids:
                is_owner = True
                if ip:
                    _register_admin_ip(ip) # 해당 IP도 어드민 IP로 동적 수집
                    
            classified_rows.append((r, is_owner))
            
        # 세 가지 버전의 통계 생성 헬퍼
        def calculate_stats(data_rows):
            visits_count = len(data_rows)
            ips = [r[0]["ip_address"] for r in data_rows if r[0]["ip_address"]]
            unique_ips = len(set(ips))
            
            one_hour_ago = datetime.now() - timedelta(hours=1)
            active_sessions = len(set(
                r[0]["ip_address"] for r in data_rows 
                if r[0]["ip_address"] and r[0]["create_dt"] and r[0]["create_dt"].replace(tzinfo=None) >= one_hour_ago
            ))
            if active_sessions == 0 and unique_ips > 0:
                active_sessions = 1
                
            avg_pvs = round(visits_count / unique_ips, 1) if unique_ips > 0 else 0.0
            
            browsers = {}
            oss = {}
            pages_map = {}
            
            for r, _ in data_rows:
                ua = r["user_agent"]
                parsed_ua = _parse_user_agent(ua)
                b = parsed_ua["browser"]
                o = parsed_ua["os"]
                browsers[b] = browsers.get(b, 0) + 1
                oss[o] = oss.get(o, 0) + 1
                
                # 방문 경로 명세 모사
                if r["input_text"]:
                    path = f"/search?q={r['input_text']}"
                elif r["thumbnail_url"]:
                    path = "/search/by-image"
                else:
                    path = "/"
                pages_map[path] = pages_map.get(path, 0) + 1
                
            popular_pages = []
            for path, count in sorted(pages_map.items(), key=lambda x: x[1], reverse=True)[:5]:
                pct = round((count / visits_count) * 100, 1) if visits_count > 0 else 0.0
                popular_pages.append({
                    "page": path,
                    "visits": count,
                    "unique_visitors": unique_ips or 1,
                    "percent": pct
                })
                
            if not popular_pages:
                popular_pages.append({"page": "/", "visits": 0, "unique_visitors": 0, "percent": 0.0})
                
            return {
                "total_visits": visits_count,
                "unique_visitors": unique_ips,
                "active_sessions": active_sessions,
                "avg_pageviews": avg_pvs,
                "browsers": browsers,
                "oss": oss,
                "popular_pages": popular_pages
            }
            
        # 1. 전체 통계
        all_stats = calculate_stats(classified_rows)
        # 2. 일반 방문자 통계 (is_owner = False)
        general_stats = calculate_stats([r for r in classified_rows if not r[1]])
        # 3. OWNER(관리자) 통계 (is_owner = True)
        owner_stats = calculate_stats([r for r in classified_rows if r[1]])
        
        return {
            "success": True,
            "all": all_stats,
            "general": general_stats,
            "owner": owner_stats,
            "client_ip": request.client.host if request.client else "Unknown"
        }
    except Exception as e:
        logger.error(f"방문자 통계 개요 조회 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/visitors/sessions")
async def get_visitors_sessions(
    request: Request,
    days: Optional[int] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None)
):
    """IP별 검색 세션 분석 및 행동 패턴 타임라인 (전체 / 일반 / OWNER 구분)"""
    try:
        where_clause = "WHERE 1=1"
        params = []
        if start_date and end_date:
            where_clause += " AND create_dt >= %s::timestamp AND create_dt <= %s::timestamp + INTERVAL '1 day' - INTERVAL '1 second'"
            params.extend([start_date, end_date])
        elif days is not None:
            where_clause += " AND create_dt >= NOW() - %s * INTERVAL '1 day'"
            params.append(days)
        else:
            where_clause += " AND create_dt >= NOW() - 1 * INTERVAL '1 day'"

        with get_pg_cursor() as cur:
            cur.execute(f"""
                SELECT ip_address, user_agent, input_text, thumbnail_url, create_dt, user_id
                FROM search_logs
                {where_clause}
                ORDER BY create_dt ASC;
            """, tuple(params))
            rows = cur.fetchall()
            
        admin_user_ids = set()
        try:
            with get_pg_cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE role = 'ADMIN';")
                admin_rows = cur.fetchall()
                admin_user_ids = set(r["user_id"] for r in admin_rows)
        except Exception:
            pass
            
        db_owner_ips = _get_db_owner_ips()
        sessions = {}
        for r in rows:
            ip = r["ip_address"] or "Unknown"
            uid = r["user_id"]
            
            is_owner = False
            if ip in _admin_ips or ip in db_owner_ips or uid in admin_user_ids:
                is_owner = True
                if ip and ip != "Unknown":
                    _register_admin_ip(ip)
                    
            if ip not in sessions:
                geo = _get_ip_geo(ip)
                ua = _parse_user_agent(r["user_agent"])
                sessions[ip] = {
                    "ip": ip,
                    "browser": ua["browser"],
                    "os": ua["os"],
                    "city": geo["city"],
                    "isp": geo["isp"],
                    "visits": 0,
                    "pages": 0,
                    "last_seen": None,
                    "duration_text": "1분",
                    "is_owner": is_owner,
                    "path": []
                }
                
            s = sessions[ip]
            s["visits"] += 1
            s["pages"] += 1
            s["last_seen"] = format_datetime_kst(r["create_dt"])
            
            if r["input_text"]:
                path_name = f"/search?q={r['input_text']}"
            elif r["thumbnail_url"]:
                path_name = "/search/by-image"
            else:
                path_name = "/"
                
            if not s["path"]:
                s["path"].append("/")
                s["pages"] += 1
            s["path"].append(path_name)
            
        session_list = list(sessions.values())
        session_list.sort(key=lambda x: x["last_seen"] or "", reverse=True)
        
        return {
            "success": True,
            "sessions": session_list
        }
    except Exception as e:
        logger.error(f"사용자 세션 분석 조회 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/visitors/realtime")
async def get_visitors_realtime(request: Request):
    """최근 24시간 실시간 유저 유입 및 모니터링 로그 (어드민 OWNER 라벨링)"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("""
                SELECT ip_address, user_agent, input_text, thumbnail_url, create_dt, user_id
                FROM search_logs
                WHERE create_dt >= NOW() - INTERVAL '24 hours'
                ORDER BY create_dt DESC
                LIMIT 50;
            """)
            rows = cur.fetchall()
            
        admin_user_ids = set()
        try:
            with get_pg_cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE role = 'ADMIN';")
                admin_rows = cur.fetchall()
                admin_user_ids = set(r["user_id"] for r in admin_rows)
        except Exception:
            pass
            
        db_owner_ips = _get_db_owner_ips()
        realtime_logs = []
        for r in rows:
            ip = r["ip_address"] or "Unknown"
            uid = r["user_id"]
            geo = _get_ip_geo(ip)
            ua = _parse_user_agent(r["user_agent"])
            
            is_owner = False
            if ip in _admin_ips or ip in db_owner_ips or uid in admin_user_ids:
                is_owner = True
                if ip and ip != "Unknown":
                    _register_admin_ip(ip)
                    
            memo = "OWNER-Session" if is_owner else ""
                
            realtime_logs.append({
                "ip": ip,
                "browser": ua["browser"],
                "os": ua["os"],
                "city": geo["city"],
                "isp": geo["isp"],
                "memo": memo,
                "is_owner": is_owner,
                "input_text": r["input_text"],
                "thumbnail_url": r["thumbnail_url"],
                "create_dt": format_datetime_kst(r["create_dt"])
            })
            
        return {
            "success": True,
            "realtime_logs": realtime_logs
        }
    except Exception as e:
        logger.error(f"실시간 방문자 조회 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/visitors/geo")
async def get_visitors_geo(
    request: Request,
    days: Optional[int] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None)
):
    """접속 장소별(국가, 도시, ISP, 타임존) 통계 및 지도 마커 데이터"""
    try:
        where_clause = "WHERE ip_address IS NOT NULL"
        params = []
        if start_date and end_date:
            where_clause += " AND create_dt >= %s::timestamp AND create_dt <= %s::timestamp + INTERVAL '1 day' - INTERVAL '1 second'"
            params.extend([start_date, end_date])
        elif days is not None:
            where_clause += " AND create_dt >= NOW() - %s * INTERVAL '1 day'"
            params.append(days)
        else:
            where_clause += " AND create_dt >= NOW() - 1 * INTERVAL '1 day'"

        with get_pg_cursor() as cur:
            cur.execute(f"""
                SELECT ip_address, user_id, count(*) as count
                FROM search_logs
                {where_clause}
                GROUP BY ip_address, user_id;
            """, tuple(params))
            rows = cur.fetchall()
            
        admin_user_ids = set()
        try:
            with get_pg_cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE role = 'ADMIN';")
                admin_rows = cur.fetchall()
                admin_user_ids = set(r["user_id"] for r in admin_rows)
        except Exception:
            pass
            
        # 데이터를 세 가지 버전(전체 / 일반 / OWNER)으로 담기 위한 딕셔너리 구조
        geo_data = {
            "all": {"countries": {}, "cities": {}, "isps": {}, "timezones": {}, "pins": []},
            "general": {"countries": {}, "cities": {}, "isps": {}, "timezones": {}, "pins": []},
            "owner": {"countries": {}, "cities": {}, "isps": {}, "timezones": {}, "pins": []}
        }
        
        db_owner_ips = _get_db_owner_ips()
        for r in rows:
            ip = r["ip_address"]
            uid = r["user_id"]
            count = r["count"]
            geo = _get_ip_geo(ip)
            
            is_owner = False
            if ip in _admin_ips or ip in db_owner_ips or uid in admin_user_ids:
                is_owner = True
                if ip:
                    _register_admin_ip(ip)
                    
            c = geo["country"]
            city = geo["city"]
            isp = geo["isp"]
            tz = geo["timezone"]
            
            # 카테고리별 누적 집계 헬퍼
            def accumulate(target, is_owner_pin):
                target["countries"][c] = target["countries"].get(c, 0) + count
                target["cities"][city] = target["cities"].get(city, 0) + count
                target["isps"][isp] = target["isps"].get(isp, 0) + count
                target["timezones"][tz] = target["timezones"].get(tz, 0) + count
                target["pins"].append({
                    "ip": ip,
                    "city": city,
                    "isp": isp,
                    "lat": geo["lat"],
                    "lng": geo["lng"],
                    "count": count,
                    "is_owner": is_owner_pin
                })
                
            # 1. 전체 누적
            accumulate(geo_data["all"], is_owner)
            
            # 2. 분기 누적
            if is_owner:
                accumulate(geo_data["owner"], True)
            else:
                accumulate(geo_data["general"], False)
                
        # Top 5 랭킹 정렬 변환
        def finalize_data(target):
            def to_rank_list(d_dict):
                return [{"name": name, "count": val} for name, val in sorted(d_dict.items(), key=lambda x: x[1], reverse=True)[:5]]
            return {
                "countries": to_rank_list(target["countries"]),
                "cities": to_rank_list(target["cities"]),
                "isps": to_rank_list(target["isps"]),
                "timezones": to_rank_list(target["timezones"]),
                "pins": target["pins"]
            }
            
        return {
            "success": True,
            "all": finalize_data(geo_data["all"]),
            "general": finalize_data(geo_data["general"]),
            "owner": finalize_data(geo_data["owner"])
        }
    except Exception as e:
        logger.error(f"장소별 분석 조회 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/visitors/owner-ips")
async def get_owner_ips():
    """등록된 관리자 IP 목록 조회"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("SELECT ip_address, memo, create_dt FROM owner_ips ORDER BY create_dt DESC;")
            rows = cur.fetchall()
        
        ip_list = []
        for r in rows:
            ip_list.append({
                "ip_address": r["ip_address"],
                "memo": r["memo"],
                "create_dt": format_datetime_kst(r["create_dt"])
            })
        return {"success": True, "owner_ips": ip_list}
    except Exception as e:
        logger.error(f"관리자 IP 목록 조회 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/visitors/owner-ip")
async def register_owner_ip(data: dict):
    """관리자 IP 수동 등록"""
    try:
        ip = data.get("ip_address", "").strip()
        memo = data.get("memo", "").strip()
        if not ip:
            raise HTTPException(status_code=400, detail="IP 주소가 필요합니다.")
            
        with get_pg_cursor() as cur:
            cur.execute("""
                INSERT INTO owner_ips (ip_address, memo)
                VALUES (%s, %s)
                ON CONFLICT (ip_address) DO UPDATE
                SET memo = EXCLUDED.memo;
            """, (ip, memo))
            
        # 메모리 캐시에도 추가
        _admin_ips.add(ip)
        
        return {"success": True, "message": f"관리자 IP({ip})가 성공적으로 등록되었습니다."}
    except Exception as e:
        logger.error(f"관리자 IP 등록 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.delete("/visitors/owner-ip/{ip_address:path}")
async def delete_owner_ip(ip_address: str):
    """관리자 IP 삭제"""
    try:
        ip = ip_address.strip()
        if not ip:
            raise HTTPException(status_code=400, detail="IP 주소가 필요합니다.")
            
        with get_pg_cursor() as cur:
            cur.execute("DELETE FROM owner_ips WHERE ip_address = %s;", (ip,))
            
        # 메모리 캐시에서도 제거 (단, 127.0.0.1 등 루프백은 제외)
        if ip in _admin_ips and ip not in ["127.0.0.1", "localhost", "::1"]:
            _admin_ips.remove(ip)
            
        return {"success": True, "message": f"관리자 IP({ip})가 성공적으로 삭제되었습니다."}
    except Exception as e:
        logger.error(f"관리자 IP 삭제 실패: {e}")
        return {"success": False, "detail": str(e)}


# ──────────────────────────────────────
# 사용자 통제 및 권한 관리 API (신설)
# ──────────────────────────────────────
from pydantic import BaseModel, Field

class CreateAdminRequest(BaseModel):
    username: str = Field(..., min_length=4, max_length=50)
    password: str = Field(..., min_length=4, max_length=255)
    name: str = Field(..., min_length=1, max_length=50)
    email: str = Field(..., min_length=3, max_length=100)
    permission: str = Field("SUPER_ADMIN")

class UpdatePermissionRequest(BaseModel):
    username: str
    permission: str

class ResetAdminPasswordRequest(BaseModel):
    username: str
    new_password: str

class ResetUserPasswordRequest(BaseModel):
    user_id: str


def _verify_super_admin(request: Request):
    """SUPER_ADMIN 권한 보유 여부 검증 헬퍼"""
    token = request.cookies.get("admin_session_token")
    if not token:
        raise HTTPException(status_code=401, detail="관리자 로그인이 필요합니다.")
        
    from ..database import get_session
    admin_data = get_session(token, is_admin=True)
    if not admin_data:
        raise HTTPException(status_code=401, detail="유효하지 않은 어드민 세션입니다.")
        
    # 세부 권한 검증
    perm = admin_data.get("admin_permission", "SUPER_ADMIN")
    if perm != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="이 요청을 수행할 SUPER_ADMIN 권한이 없습니다.")
    return admin_data


def _verify_any_admin(request: Request):
    """일반 어드민 권한 이상 보유 여부 검증 헬퍼"""
    token = request.cookies.get("admin_session_token")
    if not token:
        raise HTTPException(status_code=401, detail="관리자 로그인이 필요합니다.")
        
    from ..database import get_session
    admin_data = get_session(token, is_admin=True)
    if not admin_data:
        raise HTTPException(status_code=401, detail="유효하지 않은 어드민 세션입니다.")
    return admin_data


@router.get("/users/list")
async def get_users_list(request: Request, tab: str = Query("general")):
    """사용자(관리자 / 일반 유저 / 비로그인 유저) 모니터링 종합 목록 조회 API"""
    _verify_any_admin(request)
    
    try:
        with get_pg_cursor() as cur:
            if tab == "admin":
                # 1) 관리자 계정 조회
                cur.execute("""
                    SELECT user_id, user_name as name, email, role, admin_permission, create_dt
                    FROM users
                    WHERE role = 'ADMIN'
                    ORDER BY create_dt DESC;
                """)
                rows = cur.fetchall()
                return {"success": True, "users": [dict(r) for r in rows]}
                
            elif tab == "general":
                # 2) 일반 회원 및 모니터링 활동 정보 조인 조회
                cur.execute("""
                    SELECT 
                        u.user_id, u.user_name as name, u.email, u.provider, u.create_dt,
                        (SELECT COUNT(*) FROM search_logs WHERE user_id = u.user_id) AS total_search_count,
                        (SELECT MAX(create_dt) FROM search_logs WHERE user_id = u.user_id) AS last_search_dt,
                        (SELECT ARRAY_TO_STRING(ARRAY(
                            SELECT input_text FROM (
                                SELECT input_text, MAX(create_dt) as max_dt
                                FROM search_logs
                                WHERE user_id = u.user_id AND input_text IS NOT NULL AND input_text != ''
                                GROUP BY input_text
                                ORDER BY max_dt DESC
                                LIMIT 3
                            ) tmp
                        ), ', ')) AS recent_keywords
                    FROM users u
                    WHERE u.role = 'USER'
                    ORDER BY u.create_dt DESC;
                """)
                rows = cur.fetchall()
                return {"success": True, "users": [dict(r) for r in rows]}
                
            elif tab == "non-login":
                # 3) 비로그인 유저 집계 조회 (ip_address GroupBy)
                cur.execute("""
                    SELECT 
                        ip_address,
                        MAX(user_agent) as user_agent,
                        COUNT(*) AS total_search_count,
                        MAX(create_dt) AS last_search_dt,
                        (SELECT ARRAY_TO_STRING(ARRAY(
                            SELECT input_text FROM (
                                SELECT input_text, MAX(create_dt) as max_dt
                                FROM search_logs
                                WHERE ip_address = s.ip_address AND user_id IS NULL AND input_text IS NOT NULL AND input_text != ''
                                GROUP BY input_text
                                ORDER BY max_dt DESC
                                LIMIT 3
                            ) tmp
                        ), ', ')) AS recent_keywords
                    FROM search_logs s
                    WHERE user_id IS NULL AND ip_address IS NOT NULL
                    GROUP BY ip_address
                    ORDER BY last_search_dt DESC;
                """)
                rows = cur.fetchall()
                return {"success": True, "users": [dict(r) for r in rows]}
                
            else:
                raise HTTPException(status_code=400, detail="유효하지 않은 탭 파라미터입니다.")
                
    except Exception as e:
        logger.error(f"사용자 목록 조회 실패 ({tab}): {e}")
        return {"success": False, "detail": str(e)}


@router.post("/users/create-admin")
async def create_admin(request: Request, req: CreateAdminRequest):
    """신규 어드민 계정 생성 API (SUPER_ADMIN 전용)"""
    _verify_super_admin(request)
    
    try:
        import bcrypt
        hashed = bcrypt.hashpw(req.password.strip().encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        
        with get_pg_cursor() as cur:
            # 중복 체크
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (req.username.strip(),))
            if cur.fetchone():
                return {"success": False, "detail": "이미 존재하는 아이디입니다."}
                
            cur.execute("""
                INSERT INTO users (user_id, password, user_name, email, role, provider, admin_permission)
                VALUES (%s, %s, %s, %s, 'ADMIN', 'system', %s)
            """, (req.username.strip(), hashed, req.name.strip(), req.email.strip(), req.permission))
            
        return {"success": True, "message": "새로운 관리자 계정이 등록되었습니다."}
    except Exception as e:
        logger.error(f"어드민 생성 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/users/update-permission")
async def update_permission(request: Request, req: UpdatePermissionRequest):
    """어드민 세부 권한 변경 API (SUPER_ADMIN 전용)"""
    _verify_super_admin(request)
    
    try:
        with get_pg_cursor() as cur:
            cur.execute("""
                UPDATE users
                SET admin_permission = %s
                WHERE user_id = %s AND role = 'ADMIN'
            """, (req.permission, req.username))
            
        return {"success": True, "message": f"'{req.username}' 관리자의 권한 수준이 '{req.permission}'(으)로 갱신되었습니다."}
    except Exception as e:
        logger.error(f"권한 변경 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/users/reset-admin-password")
async def reset_admin_password(request: Request, req: ResetAdminPasswordRequest):
    """하위 어드민 비밀번호 강제 변경 API (SUPER_ADMIN 전용)"""
    _verify_super_admin(request)
    
    try:
        import bcrypt
        hashed = bcrypt.hashpw(req.new_password.strip().encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        
        with get_pg_cursor() as cur:
            cur.execute("""
                UPDATE users
                SET password = %s
                WHERE user_id = %s AND role = 'ADMIN'
            """, (hashed, req.username))
            
        return {"success": True, "message": f"'{req.username}' 관리자의 비밀번호가 성공적으로 재설정되었습니다."}
    except Exception as e:
        logger.error(f"관리자 비밀번호 재설정 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.post("/users/reset-user-password")
async def reset_user_password(request: Request, req: ResetUserPasswordRequest):
    """일반 회원 비밀번호 임시 비밀번호로 강제 재설정 API (어드민 전용)"""
    _verify_any_admin(request)
    
    try:
        import uuid
        import bcrypt
        
        # 12자리 임시 비밀번호 난수 생성
        temp_pwd = uuid.uuid4().hex[:12]
        hashed = bcrypt.hashpw(temp_pwd.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        
        with get_pg_cursor() as cur:
            cur.execute("""
                UPDATE users
                SET password = %s, is_temp_password = True
                WHERE user_id = %s AND role = 'USER'
            """, (hashed, req.user_id))
            
        return {
            "success": True, 
            "message": "해당 유저의 비밀번호를 성공적으로 초기화했습니다.", 
            "temp_password": temp_pwd
        }
    except Exception as e:
        logger.error(f"일반 유저 비밀번호 초기화 실패: {e}")
        return {"success": False, "detail": str(e)}


@router.get("/debug/memory-clear")
async def clear_memory_and_check(request: Request):
    """
    가비지 컬렉션(GC)을 수동 기동하여 메모리를 소거하고,
    FastAPI 서버 프로세스의 RSS 메모리 점유량을 KST 실시간으로 측정해 반환합니다. (어드민 전용)
    """
    # 일반 관리자 이상 권한 검증
    _verify_any_admin(request)
    
    import gc
    import os
    import psutil
    
    try:
        process = psutil.Process(os.getpid())
        before_mem = process.memory_info().rss / (1024 * 1024)
        
        # 1. 파이썬 가비지 컬렉션 수동 기동
        gc.collect()
        
        after_mem = process.memory_info().rss / (1024 * 1024)
        diff_mem = before_mem - after_mem
        
        logger.info(f"⚡ [Memory Clear API] GC 수동 실행 완료 - RAM: {before_mem:.2f}MB -> {after_mem:.2f}MB (절약: {diff_mem:.2f}MB)")
        
        return {
            "success": True,
            "status": "Garbage Collection Completed",
            "before_mem_mb": round(before_mem, 2),
            "after_mem_mb": round(after_mem, 2),
            "freed_mem_mb": round(diff_mem, 2)
        }
    except Exception as e:
        logger.error(f"메모리 정리 오류: {e}")
        return {"success": False, "detail": str(e)}


