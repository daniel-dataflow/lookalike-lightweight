"""
인프라 리소스 메트릭 API — Neon PostgreSQL 링 버퍼 기반 (초경량)
- 기존 Elasticsearch/Docker SDK 의존성 완전 제거
- psutil 로 Render 서버 CPU/Memory 실시간 수집 → infra_metrics 테이블에 저장
- /stream : 최근 1시간 시계열 반환 (차트용)
- /stats  : 현재 평균/최대 요약 반환 (요약 카드용)
"""
import logging
import asyncio
import psutil
from datetime import datetime

from fastapi import APIRouter

from ..database import get_dw_cursor

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/metrics",
    tags=["metric"],
)


# ──────────────────────────────────────
# 내부 헬퍼: psutil 스냅샷 → DB INSERT + 오래된 행 자동 청소
# ──────────────────────────────────────
def _collect_and_store() -> dict:
    """
    현재 시점의 CPU/Memory 를 psutil 로 읽어 infra_metrics 에 기록.
    동시에 1시간 초과 구형 데이터를 삭제하여 링 버퍼처럼 동작.
    반환값: {'cpu': float, 'mem': float}
    """
    cpu = psutil.cpu_percent(interval=None)   # 논블로킹 (이전 측정 기준 %)
    mem = psutil.virtual_memory().percent

    try:
        with get_dw_cursor() as cur:
            cur.execute(
                "INSERT INTO infra_metrics (cpu_usage, memory_usage) VALUES (%s, %s);",
                (cpu, mem),
            )
            cur.execute(
                "DELETE FROM infra_metrics WHERE timestamp < CURRENT_TIMESTAMP - INTERVAL '1 hour';",
            )
    except Exception as e:
        logger.warning(f"infra_metrics 저장 실패: {e}")

    return {"cpu": cpu, "mem": mem}


# ──────────────────────────────────────
# 5분 주기 백그라운드 수집 태스크
# (main.py lifespan 에서 asyncio.create_task 로 기동)
# ──────────────────────────────────────
async def start_metric_collector():
    """앱 수명 동안 5분마다 메트릭 수집. Render 무료 플랜 RAM 부담 최소화."""
    # 첫 측정 시 cpu_percent 기준값 초기화 (interval=0.1 사용 전 1회 예열)
    psutil.cpu_percent(interval=0.1)
    logger.info("📊 infra 메트릭 수집기 시작 (5분 주기)")

    while True:
        await asyncio.sleep(300)   # 5분 대기 → 그 다음 수집
        try:
            snap = await asyncio.to_thread(_collect_and_store)
            logger.info(f"📊 메트릭 수집 완료 cpu={snap['cpu']}% mem={snap['mem']}%")
        except Exception as e:
            logger.warning(f"메트릭 수집기 예외 (계속 실행): {e}")


# ──────────────────────────────────────
# GET /api/metrics/stream  — 차트용 시계열
# ──────────────────────────────────────
@router.get("/stream")
async def get_metrics_stream():
    """
    Neon DB 에서 최근 1시간 치 CPU/Memory 스냅샷을 시간순으로 반환.
    프론트엔드 Chart.js 꺾은선 그래프의 데이터 소스로 사용.

    Response:
        {
            "total": int,
            "metrics": [
                {"time": "HH:MM", "cpu_percent": float,
                 "memory_percent": float,
                 "service": "Render-API", "timestamp": "HH:MM"}
            ]
        }
    """
    try:
        from ..routers.admin import KST

        def _query():
            with get_dw_cursor() as cur:
                # 최신 12개 메트릭을 조회한 후 차트 시간순 렌더링을 위해 다시 오름차순 정렬
                cur.execute("""
                    SELECT * FROM (
                        SELECT
                            cpu_usage   AS cpu_percent,
                            memory_usage AS memory_percent,
                            timestamp
                        FROM infra_metrics
                        ORDER BY timestamp DESC
                        LIMIT 12
                    ) sub
                    ORDER BY timestamp ASC;
                """)
                return cur.fetchall()

        rows = await asyncio.to_thread(_query)
        metrics = []
        for r in rows:
            ts = r["timestamp"]
            # datetime 객체를 타임존 보정 및 ISO 규격 문자열로 변환
            if ts:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=KST)
                ts_iso = ts.isoformat()
                time_str = ts.strftime("%H:%M")
            else:
                ts_iso = ""
                time_str = ""

            metrics.append({
                "time":           time_str,
                "timestamp":      ts_iso,        # JS new Date() 가 안전하게 파싱 가능한 ISO 문자열 제공
                "cpu_percent":    round(r["cpu_percent"] or 0, 2),
                "memory_percent": round(r["memory_percent"] or 0, 2),
                "service":        "FastAPI",
                "container":      "fastapi",
                "memory_usage":   int(psutil.virtual_memory().total * ((r["memory_percent"] or 0) / 100.0)),
            })
            
        return {"total": len(metrics), "metrics": metrics}
    except Exception as e:
        logger.error(f"메트릭 스트림 조회 실패: {e}")
        return {"total": 0, "metrics": []}


# ──────────────────────────────────────
# GET /api/metrics/stats  — 요약 카드용
# ──────────────────────────────────────
@router.get("/stats")
async def get_metric_stats():
    """
    최근 1시간 데이터의 평균 CPU/Memory 를 집계하여 반환.
    관리자 인프라 대시보드 상단 요약 카드(avgCpu / avgMem)에 사용.

    Response:
        {
            "FastAPI": {
                "avg_cpu": float,
                "avg_mem": float,
                "max_mem_mb": float
            }
        }
    """
    # 현재 순간 스냅샷을 psutil 로 즉시 측정 (DB 없어도 동작)
    try:
        cur_cpu = psutil.cpu_percent(interval=None)
        cur_mem = psutil.virtual_memory().percent
    except Exception:
        cur_cpu, cur_mem = 0.0, 0.0

    # cgroups 메모리 한도를 감지하여 가상 컨테이너 실제 리미트 기반 램 연산 보장 (Render 0.5GB 지원)
    import os
    memory_limit = None
    if os.path.exists("/sys/fs/cgroup/memory.max"):
        try:
            with open("/sys/fs/cgroup/memory.max", "r") as f:
                val = f.read().strip()
                if val != "max":
                    memory_limit = int(val)
        except Exception:
            pass
    elif os.path.exists("/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
                val = int(f.read().strip())
                if val < 9223372036854771712:
                    memory_limit = val
        except Exception:
            pass

    total_mem = memory_limit if (memory_limit is not None and memory_limit > 0) else psutil.virtual_memory().total

    # DB 에 쌓인 최근 1시간 평균
    try:
        def _agg():
            with get_dw_cursor() as cur:
                cur.execute("""
                    SELECT
                        AVG(cpu_usage)    AS avg_cpu,
                        AVG(memory_usage) AS avg_mem,
                        MAX(memory_usage) AS max_mem
                    FROM infra_metrics
                    WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '1 hour';
                """)
                return cur.fetchone()

        row = await asyncio.to_thread(_agg)
        avg_cpu = round(row["avg_cpu"] or cur_cpu, 2)
        avg_mem = round(row["avg_mem"] or cur_mem, 2)
        max_mem_pct = row["max_mem"] or cur_mem
        max_mem_mb = round((total_mem * (max_mem_pct / 100.0)) / 1024 / 1024, 2)
    except Exception:
        avg_cpu, avg_mem = cur_cpu, cur_mem
        max_mem_mb = round((total_mem * (cur_mem / 100.0)) / 1024 / 1024, 2)

    return {
        "FastAPI": {
            "avg_cpu":    avg_cpu,
            "avg_mem":    avg_mem,
            "max_mem_mb": max_mem_mb,
            "max_cpu":    cur_cpu,
        }
    }
