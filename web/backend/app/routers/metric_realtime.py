"""
실시간 서버 리소스 스냅샷 API — psutil 기반 (초경량)
- 기존 Docker SDK 의존성 완전 제거
- psutil 로 FastAPI 서버의 현재 CPU/Memory/Disk/Uptime 을 즉시 반환
"""
import time
import logging
import asyncio
import psutil

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/metrics",
    tags=["metric"],
)

# 서버 시작 시각 (업타임 계산용)
_START_TIME = time.time()


@router.get("/realtime")
async def get_realtime_metrics():
    """
    psutil 로 현재 서버의 CPU/Memory/Disk/Uptime 등을 즉시 측정하여 반환.
    기존 Docker API 포맷과 호환되도록 metrics 배열 구조 유지.
    """
    try:
        def _snap():
            # CPU 사용량 및 세부 정보
            cpu = psutil.cpu_percent(interval=0.2)
            physical_cores = psutil.cpu_count(logical=False) or 0
            logical_cores = psutil.cpu_count(logical=True) or 0
            
            # CPU 주파수
            freq = psutil.cpu_freq()
            freq_current = freq.current if freq else 0.0

            # 메모리
            vm = psutil.virtual_memory()

            # 디스크 수집 (Render 환경 권한 오류 방어 및 현재 작업 디렉토리 기준 측정)
            try:
                disk = psutil.disk_usage('.')
                disk_used = disk.used
                disk_total = disk.total
                disk_percent = round(disk.percent, 2)
            except Exception as e:
                logger.warning(f"디스크 메트릭 수집 실패 (기본값 대체): {e}")
                disk_used = 0
                disk_total = 0
                disk_percent = 0.0
            
            # 업타임
            uptime = time.time() - _START_TIME

            return {
                "container":      "fastapi",
                "service":        "FastAPI",
                "cpu_percent":    round(cpu, 2),
                "cpu_cores_physical": physical_cores,
                "cpu_cores_logical":  logical_cores,
                "cpu_freq_current":   round(freq_current, 2),
                "memory_usage":   vm.used,
                "memory_percent": round(vm.percent, 2),
                "memory_limit":   vm.total,
                "disk_used":      disk_used,
                "disk_total":     disk_total,
                "disk_percent":   disk_percent,
                "uptime_seconds": round(uptime, 2),
                "status":         "running",
            }

        snap = await asyncio.to_thread(_snap)
        return {"total": 1, "metrics": [snap]}

    except Exception as e:
        logger.error(f"realtime 메트릭 조회 실패: {e}")
        return {"total": 0, "metrics": [], "error": str(e)}
