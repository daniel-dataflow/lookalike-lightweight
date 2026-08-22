"""
인프라 리소스 메트릭 API — 4세대 인메모리 링 버퍼 기반 (Zero-Compute Idle)
- Neon DB 상시 쿼리 완전 제거 (DB Sleep 0 CU 보장)
- psutil 로 Render 서버 CPU/Memory 실시간 수집 → Python deque(maxlen=12) 인메모리 보관
- /stream : 최근 1시간 시계열 반환 (차트용)
- /stats  : 현재 평균/최대 요약 반환 (요약 카드용)
"""
import logging
import asyncio
import psutil
from datetime import datetime, timedelta, timezone
from collections import deque

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/metrics",
    tags=["metric"],
)

KST = timezone(timedelta(hours=9))

# ──────────────────────────────────────
# 인메모리 시계열 링 버퍼 (최대 12개 = 5분 간격 1시간 치)
# ──────────────────────────────────────
_metric_buffer = deque(maxlen=12)


def _get_cgroup_memory_limit() -> int:
    """cgroups 메모리 한도를 감지하여 가상 컨테이너 실제 리미트 반환 (Render 0.5GB 지원)"""
    import os
    if os.path.exists("/sys/fs/cgroup/memory.max"):
        try:
            with open("/sys/fs/cgroup/memory.max", "r") as f:
                val = f.read().strip()
                if val != "max":
                    return int(val)
        except Exception:
            pass
    elif os.path.exists("/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
                val = int(f.read().strip())
                if val < 9223372036854771712:
                    return val
        except Exception:
            pass
    return psutil.virtual_memory().total


def _collect_and_store() -> dict:
    """
    현재 시점의 CPU/Memory 를 psutil 로 읽어 인메모리 _metric_buffer 에 기록.
    (DB INSERT/DELETE 쿼리를 실행하지 않아 Neon DB Sleep 상태를 100% 보존)
    """
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory().percent
    now = datetime.now(KST)

    total_mem = _get_cgroup_memory_limit()
    mem_bytes = int(total_mem * (mem / 100.0))

    item = {
        "time": now.strftime("%H:%M"),
        "timestamp": now.isoformat(),
        "cpu_percent": round(cpu, 2),
        "memory_percent": round(mem, 2),
        "service": "FastAPI",
        "container": "fastapi",
        "memory_usage": mem_bytes,
        "_raw_time": now
    }

    _metric_buffer.append(item)
    return {"cpu": cpu, "mem": mem}


# 초기 기동 시 즉시 1회 측정하여 버퍼 예열
try:
    psutil.cpu_percent(interval=0.1)
    _collect_and_store()
except Exception as _e:
    pass


# ──────────────────────────────────────
# 5분 주기 백그라운드 수집 태스크
# ──────────────────────────────────────
async def start_metric_collector():
    """
    앱 수명 동안 5분마다 메모리 큐에 메트릭 수집.
    Neon DB로의 쿼리가 일절 발생하지 않아 Compute를 0으로 유지합니다.
    """
    psutil.cpu_percent(interval=0.1)
    logger.info("📊 인메모리 infra 메트릭 수집기 시작 (5분 주기, DB 쿼리 0회)")

    while True:
        await asyncio.sleep(300)  # 5분 대기
        try:
            snap = _collect_and_store()
            logger.debug(f"📊 메트릭 수집 완료 cpu={snap['cpu']}% mem={snap['mem']}%")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"메트릭 수집기 예외 (계속 실행): {e}")


# ──────────────────────────────────────
# GET /api/metrics/stream  — 차트용 시계열
# ──────────────────────────────────────
@router.get("/stream")
async def get_metrics_stream():
    """
    인메모리 버퍼에서 최근 1시간 치 CPU/Memory 스냅샷을 시간순으로 반환.
    (Neon DB 호출 없이 메모리에서 즉시 0ms 서빙)
    """
    try:
        # 현재 버퍼가 비어있다면 즉시 1회 측정
        if not _metric_buffer:
            _collect_and_store()

        metrics = [
            {
                "time": m["time"],
                "timestamp": m["timestamp"],
                "cpu_percent": m["cpu_percent"],
                "memory_percent": m["memory_percent"],
                "service": m["service"],
                "container": m["container"],
                "memory_usage": m["memory_usage"],
            }
            for m in list(_metric_buffer)
        ]

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
    최근 1시간 데이터의 평균 CPU/Memory 를 메모리 큐에서 집계하여 반환.
    관리자 인프라 대시보드 상단 요약 카드(avgCpu / avgMem)에 사용.
    """
    try:
        cur_cpu = psutil.cpu_percent(interval=None)
        cur_mem = psutil.virtual_memory().percent
    except Exception:
        cur_cpu, cur_mem = 0.0, 0.0

    total_mem = _get_cgroup_memory_limit()

    if _metric_buffer:
        buf_list = list(_metric_buffer)
        avg_cpu = round(sum(m["cpu_percent"] for m in buf_list) / len(buf_list), 2)
        avg_mem = round(sum(m["memory_percent"] for m in buf_list) / len(buf_list), 2)
        max_mem_pct = max(m["memory_percent"] for m in buf_list)
        max_mem_mb = round((total_mem * (max_mem_pct / 100.0)) / 1024 / 1024, 2)
    else:
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
