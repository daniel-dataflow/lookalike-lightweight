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

from ..database import get_pg_cursor

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
    모니터링 대시보드 초기 렌더링에 필요한 주요 통계(Stats, Trend, Top Errors, Health)를 한 번에 조회함.
    """
    try:
        def _fetch_all():
            with get_pg_cursor() as cur:
                # ① stats: 최근 1시간 레벨별 집계
                cur.execute("""
                    SELECT level, COUNT(*) as cnt 
                    FROM app_logs 
                    WHERE timestamp >= NOW() - INTERVAL '1 hour'
                    GROUP BY level;
                """)
                stats_rows = cur.fetchall()
                by_level = {r["level"]: r["cnt"] for r in stats_rows}
                
                # 프론트엔드가 요구하는 기본 키값 패딩
                for lvl in ["INFO", "WARN", "ERROR", "CRITICAL"]:
                    if lvl not in by_level:
                        by_level[lvl] = 0

                # ② trend: 24시간 동안 각 시간대별(시간 단위) 로그 건수
                cur.execute("""
                    SELECT 
                        TO_CHAR(DATE_TRUNC('hour', timestamp), 'YYYY-MM-DD"T"HH24:00:00.000"Z"') AS time,
                        COUNT(CASE WHEN level IN ('ERROR', 'CRITICAL') THEN 1 END) AS error_cnt,
                        COUNT(CASE WHEN level = 'WARN' THEN 1 END) AS warn_cnt,
                        COUNT(CASE WHEN level = 'INFO' THEN 1 END) AS info_cnt
                    FROM app_logs
                    WHERE timestamp >= NOW() - INTERVAL '24 hours'
                    GROUP BY DATE_TRUNC('hour', timestamp)
                    ORDER BY DATE_TRUNC('hour', timestamp) ASC;
                """)
                trend_rows = cur.fetchall()
                trend = [
                    {
                        "time": r["time"],
                        "ERROR": r["error_cnt"],
                        "WARN": r["warn_cnt"],
                        "INFO": r["info_cnt"]
                    }
                    for r in trend_rows
                ]

                # ③ top_errors: 빈번한 에러 Top 5
                cur.execute("""
                    SELECT error_type, COUNT(*) as count, MAX(timestamp) as last_seen, MIN(message) as message
                    FROM app_logs
                    WHERE timestamp >= NOW() - INTERVAL '24 hours'
                      AND level IN ('ERROR', 'CRITICAL')
                    GROUP BY error_type
                    ORDER BY count DESC
                    LIMIT 5;
                """)
                top_rows = cur.fetchall()
                top_errors = [
                    {
                        "message": r["message"] or r["error_type"],
                        "count": r["count"],
                        "services": ["FastAPI"],
                        "last_seen": r["last_seen"].isoformat() if r["last_seen"] else "",
                        "container": "fastapi"
                    }
                    for r in top_rows
                ]

                # ④ service_health: 1시간 서비스별 헬스 (그룹별 집계)
                cur.execute("""
                    SELECT 
                        service,
                        COUNT(*) as total,
                        COUNT(CASE WHEN level IN ('ERROR', 'CRITICAL') THEN 1 END) as errors,
                        COUNT(CASE WHEN level = 'WARN' THEN 1 END) as warns
                    FROM app_logs
                    WHERE timestamp >= NOW() - INTERVAL '1 hour'
                    GROUP BY service;
                """)
                health_rows = cur.fetchall()
                
                service_health = []
                by_service_stats = {}
                
                # 실재하는 서비스 목록
                active_services = ["FastAPI", "PostgreSQL", "Cloudinary", "HuggingFace"]
                db_service_map = {r["service"]: r for r in health_rows}
                
                for svc in active_services:
                    row = db_service_map.get(svc)
                    total = row["total"] if row else 0
                    errors = row["errors"] if row else 0
                    warns = row["warns"] if row else 0
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
                    by_service_stats[svc] = total

                return {
                    "stats": {"by_level": by_level, "by_service": by_service_stats},
                    "trend": trend,
                    "top_errors": top_errors,
                    "service_health": service_health,
                    "generated_at": datetime.utcnow().isoformat(),
                }
        
        result = await asyncio.to_thread(_fetch_all)
        return result
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
            with get_pg_cursor() as cur:
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
# 3. 실시간 로그 스트림 API (Neon DB 조회)
# ──────────────────────────────────────
@router.get("/stream")
async def get_logs_stream(
    service: Optional[str] = None,
    level: Optional[str] = None,
    keyword: Optional[str] = None,
    size: int = Query(100, le=500)
):
    """실시간 로그 스트림 반환"""
    try:
        def _query():
            conditions = []
            params = []
            if service and service != "ALL":
                conditions.append("service = %s")
                params.append(service)
            if level and level != "ALL":
                conditions.append("level = %s")
                params.append(level)
            if keyword:
                conditions.append("message ILIKE %s")
                params.append(f"%{keyword}%")
                
            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
            
            with get_pg_cursor() as cur:
                cur.execute(f"""
                    SELECT level, service, message, error_type, timestamp 
                    FROM app_logs
                    {where_clause}
                    ORDER BY timestamp DESC
                    LIMIT %s;
                """, params + [size])
                rows = cur.fetchall()
                return rows

        rows = await asyncio.to_thread(_query)
        logs = [
            {
                "timestamp": r["timestamp"].isoformat(),
                "level": r["level"],
                "service": r["service"],
                "container": r["service"].lower(),
                "message": r["message"]
            }
            for r in rows
        ]
        return {
            "total": len(logs),
            "logs": logs
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
    """로그 리스트 텍스트 스트리밍 다운로드"""
    try:
        def _fetch():
            conditions = []
            params = []
            if service and service != "ALL":
                conditions.append("service = %s")
                params.append(service)
            if level and level != "ALL":
                conditions.append("level = %s")
                params.append(level)
            if keyword:
                conditions.append("message ILIKE %s")
                params.append(f"%{keyword}%")
                
            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
            
            with get_pg_cursor() as cur:
                cur.execute(f"""
                    SELECT level, service, message, timestamp 
                    FROM app_logs
                    {where_clause}
                    ORDER BY timestamp DESC
                    LIMIT %s;
                """, params + [size])
                return cur.fetchall()

        rows = await asyncio.to_thread(_fetch)
        
        def iter_logs():
            for r in rows:
                ts = r["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
                lvl = r["level"]
                svc = r["service"]
                msg = r["message"].replace("\n", "  ")
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
    """로그 전체 삭제"""
    try:
        def _delete():
            with get_pg_cursor() as cur:
                cur.execute("TRUNCATE TABLE app_logs;")
                return cur.rowcount

        await asyncio.to_thread(_delete)
        return {
            "success": True,
            "deleted_count": 0,
            "message": "모든 에러 로그 데이터가 성공적으로 초기화되었습니다."
        }
    except Exception as e:
        logger.error(f"로그 삭제 실패: {e}")
        raise HTTPException(status_code=500, detail="로그 초기화 중 백엔드 오류가 발생했습니다.")
