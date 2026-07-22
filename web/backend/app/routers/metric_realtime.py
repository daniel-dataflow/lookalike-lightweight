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


def get_os_name() -> str:
    """
    실제 운영체제(OS) 배포판 명칭 및 버전을 정밀 동적 파싱
    (예: Linux (Ubuntu 22.04.4 LTS), Linux (Debian 12), macOS (14.5), Windows (10.0.19045) 등)
    """
    try:
        import platform, os
        if os.path.exists('/etc/os-release'):
            os_data = {}
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    if '=' in line:
                        k, v = line.strip().split('=', 1)
                        os_data[k] = v.strip('"\'')
            pretty_name = os_data.get('PRETTY_NAME') or os_data.get('NAME')
            if pretty_name:
                return f"Linux ({pretty_name})"

        sys_name = platform.system()
        rel_ver = platform.release()

        if sys_name == "Linux":
            return f"Linux ({rel_ver})"
        elif sys_name == "Darwin":
            return f"macOS ({platform.mac_ver()[0] or rel_ver})"
        elif sys_name == "Windows":
            return f"Windows ({platform.version() or rel_ver})"

        return f"{sys_name} {rel_ver}".strip()
    except Exception:
        import platform
        return platform.system() or "Linux"


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
            (settings.APP_ENV.lower() in ("prod", "production")) or 
            (os.getenv("APP_ENV", "").lower() in ("prod", "production"))
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

            # CPU 할당 퍼센트 계산 (1 vCPU = 100%, 0.15 vCPU = 15%)
            cpu_limit_percent = round(cpu_limit * 100, 1) if cpu_limit else 100.0
            # 할당량 대비 현재 점유율 %
            cpu_quota_usage_percent = (
                round((cpu / (cpu_limit * 100)) * 100, 1) if (cpu_limit and cpu_limit > 0) else round(cpu, 1)
            )

            # 호스트 물리 하드웨어 코어 및 주파수 수집
            physical_cores = psutil.cpu_count(logical=False) or 0
            logical_cores = psutil.cpu_count(logical=True) or 0
            freq = psutil.cpu_freq()
            freq_current = freq.current if freq else 0.0
            freq_max = freq.max if freq else 0.0
            freq_base = 0.0

            # 1단계: /proc/cpuinfo 명시적 수치 파싱 (@ 3.60GHz)
            if os.path.exists("/proc/cpuinfo"):
                try:
                    import re
                    with open("/proc/cpuinfo", "r") as f:
                        content = f.read()
                        match = re.search(r"model name.*@\s*([\d\.]+)\s*GHz", content, re.IGNORECASE)
                        if not match:
                            match = re.search(r"model name.*?\b([\d\.]+)\s*GHz", content, re.IGNORECASE)
                        if match:
                            freq_base = float(match.group(1)) * 1000.0
                except Exception:
                    pass

            # 2단계: Linux 커널 sysfs 직접 파싱 (base_frequency, bios_limit, cpuinfo_max_freq, scaling_max_freq)
            if freq_base <= 0:
                for sys_path in [
                    "/sys/devices/system/cpu/cpu0/cpufreq/base_frequency",
                    "/sys/devices/system/cpu/cpu0/cpufreq/bios_limit",
                    "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq",
                    "/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq"
                ]:
                    if os.path.exists(sys_path):
                        try:
                            with open(sys_path, "r") as sf:
                                khz = float(sf.read().strip())
                                if khz > 0:
                                    freq_base = khz / 1000.0
                                    break
                        except Exception:
                            pass

            # 3단계: lscpu CLI 명령어 파싱 (CPU max MHz / CPU base MHz)
            if freq_base <= 0:
                try:
                    import subprocess, re
                    out = subprocess.check_output(["lscpu"], stderr=subprocess.DEVNULL, timeout=1).decode("utf-8", errors="ignore")
                    mhz_match = re.search(r"CPU (?:max|base) MHz\s*:\s*([\d\.]+)", out, re.IGNORECASE)
                    if mhz_match:
                        mhz_val = float(mhz_match.group(1))
                        if mhz_val > 0:
                            freq_base = mhz_val
                except Exception:
                    pass

            # 4단계: psutil.cpu_freq().max 커널 시스템 콜
            if freq_base <= 0 and freq_max > 0:
                freq_base = freq_max

            # 5단계: 클라우드 CPU 모델명 딕셔너리 매핑 DB (known_models) (시스템 파싱 전면 차단 시 차선책)
            if freq_base <= 0 and os.path.exists("/proc/cpuinfo"):
                try:
                    import re
                    with open("/proc/cpuinfo", "r") as f:
                        cpu_content = f.read()
                    
                    model_match = re.search(r"model name\s*:\s*(.+)", cpu_content, re.IGNORECASE)
                    if model_match:
                        model_str = model_match.group(1).strip()
                        ghz_match = re.search(r"([\d\.]+)\s*GHz", model_str, re.IGNORECASE)
                        if ghz_match:
                            freq_base = float(ghz_match.group(1)) * 1000.0
                        else:
                            known_models = {
                                "EPYC 7763": 2450.0,
                                "EPYC 7B12": 2250.0,
                                "EPYC 7571": 2200.0,
                                "EPYC 7R32": 2800.0,
                                "EPYC": 2400.0,
                                "Haswell": 2400.0,
                                "Broadwell": 2400.0,
                                "Skylake": 2500.0,
                                "Cascadelake": 2500.0,
                                "Ice Lake": 2600.0,
                                "Sapphire": 2500.0,
                                "Xeon": 2400.0,
                                "Graviton": 2500.0,
                                "Ampere": 2800.0,
                                "Core": 2500.0,
                                "Ryzen": 3600.0,
                                "Apple": 3200.0,
                            }
                            for key, base_mhz in known_models.items():
                                if key.lower() in model_str.lower():
                                    freq_base = base_mhz
                                    break
                except Exception:
                    pass

            # 6단계: 최종 기본값 Safety Fallback (2.40GHz 보장)
            if freq_base <= 0:
                freq_base = 2400.0
            
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
                "container":               "fastapi",
                "service":                 "FastAPI",
                "cpu_percent":             round(cpu, 2),
                "cpu_limit":               cpu_limit,
                "cpu_limit_percent":       cpu_limit_percent,
                "cpu_quota_usage_percent": cpu_quota_usage_percent,
                "cpu_cores_physical":      physical_cores,
                "cpu_cores_logical":       logical_cores,
                "cpu_freq_current":        round(freq_current, 2),
                "cpu_freq_max":            round(freq_max, 2),
                "cpu_freq_base":           round(freq_base, 2),
                "memory_usage":            memory_usage,
                "memory_percent":          memory_percent,
                "memory_limit":            memory_limit,
                "disk_used":               disk_used,
                "disk_total":              disk_total,
                "disk_percent":            disk_percent,
                "uptime_seconds":          round(uptime, 2),
                "os_name":                 get_os_name(),
                "status":                  "running",
            }

        snap = await asyncio.to_thread(_snap)
        return {"total": 1, "metrics": [snap]}

    except Exception as e:
        logger.error(f"realtime 메트릭 조회 실패: {e}")
        return {"total": 0, "metrics": [], "error": str(e)}
