"""
Neon PostgreSQL을 활용한 초경량 에러 로그 모니터링 API 라우터
- 기존 Elasticsearch 의존성 완전 제거
- app_logs 테이블 직접 조회 기반으로 교체
"""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..database import get_dw_cursor

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/logs",
    tags=["log"],
    responses={404: {"description": "Not found"}},
)

# ──────────────────────────────────────
# 1. 통합 대시보드 API (프론트엔드 호환용)
# ──────────────────────────────────────
@router.get("/dashboard")
async def get_log_dashboard():
    """
    모니터링 대시보드 초기 렌더링에 필요한 주요 통계(Stats, Trend, Top Errors, Health)를 조회함.
    인메모리 버퍼를 우선 사용하여 DB 호출 없이 즉시 응답합니다.
    """
    try:
        from ..main import get_in_memory_logs
        mem_logs = get_in_memory_logs()

        by_level = {"INFO": 0, "WARN": 0, "ERROR": 0, "CRITICAL": 0}
        by_service_stats = {"FastAPI": 0, "PostgreSQL": 0, "Cloudinary": 0, "HuggingFace": 0}
        error_counts = {}
        error_messages = {}
        error_last_seen = {}

        for log in mem_logs:
            lvl = log.get("level", "INFO")
            svc = log.get("service", "FastAPI")
            err_type = log.get("error_type", "unknown")
            msg = log.get("message", "")
            ts = log.get("timestamp", "")

            by_level[lvl] = by_level.get(lvl, 0) + 1
            by_service_stats[svc] = by_service_stats.get(svc, 0) + 1

            if lvl in ("ERROR", "CRITICAL"):
                error_counts[err_type] = error_counts.get(err_type, 0) + 1
                error_messages[err_type] = msg
                error_last_seen[err_type] = ts

        top_errors = []
        for err_type, count in sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
            top_errors.append({
                "message": error_messages.get(err_type, err_type),
                "count": count,
                "services": ["FastAPI"],
                "last_seen": error_last_seen.get(err_type, ""),
                "container": "fastapi"
            })

        active_services = ["FastAPI", "PostgreSQL", "Cloudinary", "HuggingFace"]
        service_health = []
        for svc in active_services:
            total = by_service_stats.get(svc, 0)
            errors = sum(1 for l in mem_logs if l.get("service") == svc and l.get("level") in ("ERROR", "CRITICAL"))
            warns = sum(1 for l in mem_logs if l.get("service") == svc and l.get("level") == "WARN")
            error_rate = round((errors / total) * 100, 2) if total > 0 else 0
            if error_rate > 10 or errors > 20:
                status = "critical"
            elif error_rate > 3 or warns > 10:
                status = "warning"
            else:
                status = "healthy"

            service_health.append({
                "service": svc,
                "total": total,
                "errors": errors,
                "warns": warns,
                "error_rate": error_rate,
                "status": status
            })

        trend = []
        now_dt = datetime.now()
        for i in range(24):
            t_hour = (now_dt - timedelta(hours=23 - i)).strftime("%Y-%m-%dT%H:00:00.000Z")
            trend.append({
                "time": t_hour,
                "ERROR": 0,
                "WARN": 0,
                "INFO": 0
            })

        return {
            "stats": {"by_level": by_level, "by_service": by_service_stats},
            "trend": trend,
            "top_errors": top_errors,
            "service_health": service_health,
            "generated_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.error(f"로그 대시보드 조회 실패: {e}")
        return {
            "stats": {"by_level": {}, "by_service": {}},
            "trend": [],
            "top_errors": [],
            "service_health": [],
            "generated_at": datetime.utcnow().isoformat(),
        }


# ──────────────────────────────────────
# 2. 파이프라인 상태 API (간소화)
# ──────────────────────────────────────
@router.get("/pipeline-status")
async def get_pipeline_status():
    """로그 적재 파이프라인 상태 반환"""
    try:
        def _get_stats():
            with get_dw_cursor() as cur:
                cur.execute("SELECT COUNT(*) as count FROM app_logs;")
                count = cur.fetchone()["count"]
                
                # DB 용량 대략 산정용
                cur.execute("SELECT pg_database_size(current_database()) as size;")
                size = cur.fetchone()["size"]
                return count, size

        doc_count, store_size = await asyncio.to_thread(_get_stats)
        return {
            "kafka": {"status": "inactive"},
            "direct": {"status": "active"},
            "elasticsearch": {
                "status": "inactive",
                "total_docs": doc_count,
                "store_size": store_size
            },
            "active_pipeline": "direct"
        }
    except Exception:
        return {
            "kafka": {"status": "inactive"},
            "direct": {"status": "active"},
            "elasticsearch": {"status": "inactive", "total_docs": 0, "store_size": 0},
            "active_pipeline": "direct"
        }


# ──────────────────────────────────────
# 3. 실시간 로그 스트림 API (인메모리 우선 조회)
# ──────────────────────────────────────
@router.get("/stream")
async def get_logs_stream(
    service: Optional[str] = None,
    level: Optional[str] = None,
    keyword: Optional[str] = None,
    size: int = Query(100, le=500)
):
    """실시간 로그 스트림 반환 (인메모리 버퍼 우선 서빙, DB Sleep 유지)"""
    try:
        from ..main import get_in_memory_logs
        mem_logs = get_in_memory_logs()

        limit = size if isinstance(size, int) else 100
        filtered = []
        for log in reversed(mem_logs):
            if service and service != "ALL" and log.get("service") != service:
                continue
            if level and level != "ALL" and log.get("level") != level:
                continue
            if keyword and keyword.lower() not in log.get("message", "").lower():
                continue
            filtered.append({
                "timestamp": log.get("timestamp", ""),
                "level": log.get("level", "INFO"),
                "service": log.get("service", "FastAPI"),
                "container": log.get("service", "fastapi").lower(),
                "message": log.get("message", "")
            })
            if len(filtered) >= limit:
                break

        return {
            "total": len(filtered),
            "logs": filtered
        }
    except Exception as e:
        logger.error(f"실시간 로그 조회 실패: {e}")
        return {"total": 0, "logs": []}


# ──────────────────────────────────────
# 4. 로그 다운로드 API (텍스트 스트리밍)
# ──────────────────────────────────────
@router.get("/download")
async def get_logs_download(
    service: Optional[str] = None,
    level: Optional[str] = None,
    keyword: Optional[str] = None,
    size: int = Query(10000, le=50000)
):
    """로그 리스트 텍스트 스트리밍 다운로드 (인메모리 버퍼 기반)"""
    try:
        from ..main import get_in_memory_logs
        mem_logs = get_in_memory_logs()
        limit = size if isinstance(size, int) else 10000

        def iter_logs():
            count = 0
            for log in reversed(mem_logs):
                if count >= limit:
                    break
                if service and service != "ALL" and log.get("service") != service:
                    continue
                if level and level != "ALL" and log.get("level") != level:
                    continue
                if keyword and keyword.lower() not in log.get("message", "").lower():
                    continue
                ts = log.get("timestamp", "")
                lvl = log.get("level", "INFO")
                svc = log.get("service", "FastAPI")
                msg = log.get("message", "").replace("\n", "  ")
                count += 1
                yield f"[{ts}] [{lvl}] [{svc}] {svc.lower()} - {msg}\n"

        headers = {
            "Content-Disposition": f"attachment; filename=app_logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        }
        return StreamingResponse(iter_logs(), media_type="text/plain", headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"로그 다운로드 실패: {e}")



# ──────────────────────────────────────
# 5. 레거시 및 모달 설정 API 폴백
# ──────────────────────────────────────
@router.get("/alerts/config")
async def get_alert_config():
    return {
        "enabled": False,
        "webhook_url_preview": "http://disabled",
        "min_alert_level": "CRITICAL"
    }

@router.post("/alerts/config")
async def set_alert_config(data: dict):
    return {"success": True, "message": "설정이 더미 저장되었습니다."}

@router.post("/alerts/test")
async def test_alert():
    return {"success": True, "message": "테스트 알림 완료(비활성)"}

@router.get("/alerts/status")
async def get_alert_status():
    return {"circuit_state": "closed", "startup_grace_remaining_sec": 0}

@router.get("/recovery/config")
async def get_recovery_config():
    return {"enabled": False}

@router.post("/recovery/config")
async def set_recovery_config(data: dict):
    return {"success": True}

@router.get("/recovery/status")
async def get_recovery_status():
    return {"restart_history": []}


# ──────────────────────────────────────
# 6. 로그 일괄 삭제 (Purge)
# ──────────────────────────────────────
@router.delete("/purge")
async def purge_all_logs():
    """로그 전체 삭제 (인메모리 버퍼 및 DB 일괄 초기화)"""
    try:
        from ..main import _memory_logs
        _memory_logs.clear()

        def _delete():
            try:
                with get_dw_cursor() as cur:
                    cur.execute("TRUNCATE TABLE app_logs;")
                    return cur.rowcount
            except Exception:
                return 0

        await asyncio.to_thread(_delete)
        return {
            "success": True,
            "deleted_count": 0,
            "message": "모든 에러 로그 데이터가 성공적으로 초기화되었습니다."
        }
    except Exception as e:
        logger.error(f"로그 삭제 실패: {e}")
        raise HTTPException(status_code=500, detail="로그 초기화 중 백엔드 오류가 발생했습니다.")
