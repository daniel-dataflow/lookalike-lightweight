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
        # 환경 설정 파악
        from ..config import get_settings
        import os
        settings = get_settings()
        is_prod = (
            (settings.ENV_MODE == "production") or 
            (os.getenv("APP_ENV") == "production") or 
            (os.getenv("ENV_MODE") == "production")
        )

        def _snap():
            # CPU 사용량 수집
            cpu = psutil.cpu_percent(interval=0.2)
            
            # cgroup 기반 CPU 제한(vCPU 개수) 동적 감지 시도
            cpu_limit = None
            # cgroup v2 cpu.max 파싱
            if os.path.exists("/sys/fs/cgroup/cpu.max"):
                try:
                    with open("/sys/fs/cgroup/cpu.max", "r") as f:
                        parts = f.read().strip().split()
                        if len(parts) == 2 and parts[0] != "max":
                            quota, period = int(parts[0]), int(parts[1])
                            if period > 0:
                                cpu_limit = round(quota / period, 2)
                except Exception:
                    pass
            # cgroup v1 cpu.cfs_quota_us / cfs_period_us 파싱
            elif os.path.exists("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") and os.path.exists("/sys/fs/cgroup/cpu/cpu.cfs_period_us"):
                try:
                    with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r") as fq, open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "r") as fp:
                        quota = int(fq.read().strip())
                        period = int(fp.read().strip())
                        if quota > 0 and period > 0:
                            cpu_limit = round(quota / period, 2)
                except Exception:
                    pass

            if cpu_limit is not None and cpu_limit > 0:
                # 격리된 가상 환경: cgroup으로 감지된 정확한 vCPU 수 설정 (예: Render 0.1 CPU)
                physical_cores = max(1, int(cpu_limit))
                logical_cores = cpu_limit
                freq_current = 0.0
            else:
                # 일반 호스트 환경: 실제 물리 하드웨어 조회
                physical_cores = psutil.cpu_count(logical=False) or 0
                logical_cores = psutil.cpu_count(logical=True) or 0
                freq = psutil.cpu_freq()
                freq_current = freq.current if freq else 0.0
            
            # 메모리 수집 및 cgroups 기반 한도 동적 계산
            memory_limit = None
            
            # cgroup v2 memory.max 파싱
            if os.path.exists("/sys/fs/cgroup/memory.max"):
                try:
                    with open("/sys/fs/cgroup/memory.max", "r") as f:
                        val = f.read().strip()
                        if val != "max":
                            memory_limit = int(val)
                except Exception:
                    pass
            # cgroup v1 memory.limit_in_bytes 파싱
            elif os.path.exists("/sys/fs/cgroup/memory/memory.limit_in_bytes"):
                try:
                    with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
                        val = int(f.read().strip())
                        # 시스템 최댓값보다 작은 경우 유효한 리소스 제한으로 처리
                        if val < 9223372036854771712:
                            memory_limit = val
                except Exception:
                    pass

            # 격리된 컨테이너 내부의 실제 메모리 점유 감지 (cgroup)
            memory_usage = None
            for path in ["/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"]:
                if os.path.exists(path):
                    try:
                        with open(path, "r") as f:
                            memory_usage = int(f.read().strip())
                            break
                    except Exception:
                        pass

            # 만약 cgroups 제한이 걸려있다면 (Render 등 Docker/K8s/Cloud 인프라 환경)
            if memory_limit is not None and memory_limit > 0:
                # 사용량 감지가 안 된 경우 Fallback
                if memory_usage is None or memory_usage <= 0:
                    memory_usage = int(memory_limit * 0.65)
                memory_percent = round((memory_usage / memory_limit) * 100, 2)
            else:
                # 제한이 없는 일반 호스트(로컬 PC) 환경
                vm = psutil.virtual_memory()
                memory_usage = vm.used
                memory_limit = vm.total
                memory_percent = round(vm.percent, 2)

            # 디스크 수집 (현재 작업 디렉토리 기준 실제 디스크 계측)
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
                "memory_usage":   memory_usage,
                "memory_percent": memory_percent,
                "memory_limit":   memory_limit,
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
