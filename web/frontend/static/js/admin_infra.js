let autoRefreshInterval = null;
let cpuChart = null;
let memChart = null;
const CHART_COLORS = [
    '#3366CC', '#22B573', '#FF8C1A', '#E63946', '#8E44AD', '#0EA5A0', '#D63384'
];

// 백엔드로부터 정확한 서비스 표시 이름이 넘어오므로 프론트엔드 매핑은 제거합니다.

document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    refreshData();

    // Auto refresh
    const switchEl = document.getElementById('autoRefreshSwitch');
    const manualBtn = document.getElementById('btnManualRefresh');
    if (switchEl.checked) startAutoRefresh();

    switchEl.addEventListener('change', (e) => {
        if (e.target.checked) {
            startAutoRefresh();
            manualBtn.disabled = true;
        } else {
            stopAutoRefresh();
            manualBtn.disabled = false;
        }
    });
});

function startAutoRefresh() {
    if (autoRefreshInterval) clearInterval(autoRefreshInterval);
    autoRefreshInterval = setInterval(refreshData, 10000); // 10초마다
}

function stopAutoRefresh() {
    if (autoRefreshInterval) clearInterval(autoRefreshInterval);
    autoRefreshInterval = null;
}

function getProgressColor(percent) {
    if (percent < 60) return 'bg-success';
    if (percent < 80) return 'bg-warning';
    return 'bg-danger';
}

function initCharts() {
    const cpuCtx = document.getElementById('cpuChart').getContext('2d');
    const memCtx = document.getElementById('memChart').getContext('2d');

    // 시간 포맷 (ISO → '오후 4:13' / 이미 '17:34' 등 포맷팅된 문자열은 그대로 반환)
    function fmtTime(iso) {
        if (!iso) return '';
        // 이미 '17:34' 와 같이 가공된 포맷이면 그대로 반환
        if (iso.length === 5 && iso.includes(':')) {
            return iso;
        }
        try {
            const t = iso.endsWith('Z') ? iso : iso + 'Z';
            const date = new Date(t);
            if (isNaN(date.getTime())) return iso;
            return date.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
        } catch (e) {
            return iso;
        }
    }
    // 차트 외부에서도 사용할 수 있도록 저장
    window._fmtTime = fmtTime;

    // hover 시 해당 라인만 강조, 나머지 fade 처리
    const hoverPlugin = {
        id: 'hoverHighlight',
        beforeDatasetsDraw(chart) {
            const active = chart.getActiveElements();
            if (active.length > 0) {
                const activeIdx = active[0].datasetIndex;
                chart.data.datasets.forEach((ds, i) => {
                    ds.borderColor = i === activeIdx
                        ? ds._originalColor
                        : ds._originalColor + '20';  // 비활성: 12% 투명도 (확실히 fade)
                    ds.borderWidth = i === activeIdx ? 3.5 : 0.8;
                });
            } else {
                chart.data.datasets.forEach(ds => {
                    ds.borderColor = ds._originalColor;
                    ds.borderWidth = 1.5;
                });
            }
        }
    };

    const sharedOptions = (yConfig) => ({
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
            x: {
                display: true,
                ticks: {
                    maxTicksLimit: 6,
                    maxRotation: 0,
                    font: { size: 10 },
                    color: '#aaa',
                    callback: (_, idx) => fmtTime(cpuChart?.data?.labels?.[idx] || memChart?.data?.labels?.[idx] || '')
                },
                grid: { display: false }
            },
            y: yConfig
        },
        elements: {
            point: { radius: 0, hoverRadius: 3, hoverBorderWidth: 2 },
            line: { borderJoinStyle: 'round' }
        },
        plugins: {
            legend: {
                position: 'bottom',
                labels: {
                    boxWidth: 10, boxHeight: 2,
                    padding: 15,
                    font: { size: 11 },
                    usePointStyle: false
                }
            },
            tooltip: {
                backgroundColor: 'rgba(0,0,0,0.8)',
                titleFont: { size: 12 },
                bodyFont: { size: 11 },
                padding: 10,
                cornerRadius: 6,
                mode: 'index',
                intersect: false,
                filter: item => item.parsed.y !== null && item.parsed.y !== undefined
            }
        }
    });

    cpuChart = new Chart(cpuCtx, {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: sharedOptions({
            beginAtZero: true,
            suggestedMax: 50,
            ticks: {
                stepSize: 10,
                callback: v => v + '%',
                font: { size: 10 },
                color: '#aaa'
            },
            grid: { color: 'rgba(0,0,0,0.04)', drawBorder: false }
        }),
        plugins: [hoverPlugin]
    });

    memChart = new Chart(memCtx, {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: sharedOptions({
            beginAtZero: true,
            ticks: {
                callback: v => (v >= 1000 ? (v / 1024).toFixed(1) + ' GB' : v.toFixed(0) + ' MB'),
                font: { size: 10 },
                color: '#aaa'
            },
            grid: { color: 'rgba(0,0,0,0.04)', drawBorder: false }
        }),
        plugins: [hoverPlugin]
    });
}

/**
 * 메인 갱신 함수: 시스템 리소스 + 메트릭 데이터를 병렬 수신하여 UI 업데이트.
 * Neon DB 링 버퍼(스트림) + psutil 요약(stats) + 어드민 시스템 헬스를 병렬 호치.
 */
async function refreshData() {
    await Promise.all([
        fetchSystemHealth(),   // 시스템 상태 (CPU/Mem/Disk/Uptime) — psutil realtime
        fetchStats(),          // 요약 카드 (1시간 평균) — Neon DB stats
        fetchStream()          // 시계열 차트   — Neon DB stream
    ]);
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('ko-KR');

    // 데이터베이스 상태는 어드민 API 사용
    fetchDbStatus();
}

// 시스템 상태 (psutil realtime)
async function fetchSystemHealth() {
    try {
        const resp = await fetch('/api/metrics/realtime');
        const data = await resp.json();
        const m = (data.metrics || [])[0];
        if (!m) return;

        // CPU 카드
        document.getElementById('cpuPercent').textContent = `${m.cpu_percent.toFixed(1)}%`;
        document.getElementById('cpuProgress').style.width = `${m.cpu_percent}%`;
        document.getElementById('cpuProgress').className = `progress-bar ${getProgressColor(m.cpu_percent)}`;

        // CPU 코어 및 사양 정보 출력
        const cpuInfo = [];
        if (m.cpu_freq_current > 0) cpuInfo.push(`${(m.cpu_freq_current / 1000).toFixed(2)}GHz`);
        if (m.cpu_cores_physical > 0 && m.cpu_cores_logical > 0) {
            cpuInfo.push(`${m.cpu_cores_physical}C/${m.cpu_cores_logical}T`);
        }
        document.getElementById('cpuDetail').textContent = cpuInfo.join(' | ') || 'CPU 정보 없음';

        // 메모리 카드
        const memUsedGB = (m.memory_usage / 1024 / 1024 / 1024).toFixed(1);
        const memTotalGB = (m.memory_limit / 1024 / 1024 / 1024).toFixed(1);
        document.getElementById('memoryPercent').textContent = `${m.memory_percent.toFixed(1)}%`;
        document.getElementById('memoryProgress').style.width = `${m.memory_percent}%`;
        document.getElementById('memoryProgress').className = `progress-bar ${getProgressColor(m.memory_percent)}`;
        document.getElementById('memoryDetail').textContent = `${memUsedGB}GB / ${memTotalGB}GB`;

        // 업타임
        if (m.uptime_seconds !== undefined) {
            const uptime = m.uptime_seconds;
            if (uptime < 60) {
                document.getElementById('uptime').textContent = `${uptime.toFixed(0)}초`;
            } else if (uptime < 3600) {
                document.getElementById('uptime').textContent = `${Math.floor(uptime / 60)}분 ${Math.floor(uptime % 60)}초`;
            } else {
                const hrs = Math.floor(uptime / 3600);
                const mins = Math.floor((uptime % 3600) / 60);
                document.getElementById('uptime').textContent = `${hrs}시간 ${mins}분`;
            }
        } else {
            document.getElementById('uptime').textContent = '구동 중';
        }

        // 디스크
        if (m.disk_used !== undefined) {
            const diskUsedGB = (m.disk_used / 1024 / 1024 / 1024).toFixed(1);
            const diskTotalGB = (m.disk_total / 1024 / 1024 / 1024).toFixed(1);
            const diskFreeGB = ((m.disk_total - m.disk_used) / 1024 / 1024 / 1024).toFixed(1);
            document.getElementById('diskPercent').textContent = `${m.disk_percent.toFixed(1)}%`;
            document.getElementById('diskProgress').style.width = `${m.disk_percent}%`;
            document.getElementById('diskProgress').className = `progress-bar ${getProgressColor(m.disk_percent)}`;
            document.getElementById('diskDetail').textContent = `${diskUsedGB}GB / ${diskTotalGB}GB`;
            document.getElementById('diskTotal').textContent = `${diskTotalGB} GB`;
            document.getElementById('diskFree').textContent = `${diskFreeGB} GB`;
        } else {
            document.getElementById('diskPercent').textContent = 'N/A';
            document.getElementById('diskDetail').textContent = '측정 실패';
            document.getElementById('diskTotal').textContent = '-';
            document.getElementById('diskFree').textContent = '-';
        }
    } catch (e) {
        console.error('시스템 실시간 조회 실패:', e);
    }
}

// 데이터베이스 및 이미지 상태 (admin health API)
async function fetchDbStatus() {
    try {
        const resp = await fetch('/api/admin/system/health');
        const data = await resp.json();

        // PostgreSQL
        const pgOk = data.db_status === 'healthy';
        document.getElementById('pgConnections').textContent = data.db_active_connections || '-';

        // Neon DB 세부 정보 바인딩
        const updateNeonBadge = (elemId, status) => {
            const el = document.getElementById(elemId);
            if (!el) return;
            if (status === 'Neon') {
                el.className = 'badge bg-success';
                el.textContent = 'Neon';
            } else if (status === 'Other') {
                el.className = 'badge bg-secondary';
                el.textContent = 'Other';
            } else if (status === 'Error') {
                el.className = 'badge bg-danger';
                el.textContent = 'Error';
            } else {
                el.className = 'badge bg-light text-muted';
                el.textContent = 'None';
            }
        };

        const neonStatus = data.db_urls_neon_status || {};
        updateNeonBadge('neonDevDbBadge', neonStatus.DEV_DATABASE_URL);
        updateNeonBadge('neonDevDwDbBadge', neonStatus.DEV_DW_DATABASE_URL);
        updateNeonBadge('neonProdDbBadge', neonStatus.PROD_DATABASE_URL);
        updateNeonBadge('neonProdDwDbBadge', neonStatus.PROD_DW_DATABASE_URL);

        const setElText = (id, text) => {
            const el = document.getElementById(id);
            if (el) el.textContent = text;
        };

        setElText('neonDevDbSize', data.db_dev_size_mb ? `${data.db_dev_size_mb} MB` : '0.0 MB');
        setElText('neonDevDwDbSize', data.db_dev_dw_size_mb ? `${data.db_dev_dw_size_mb} MB` : '0.0 MB');
        setElText('neonDevTotalSize', data.db_dev_total_size_mb ? `${data.db_dev_total_size_mb} MB` : '0.0 MB');

        setElText('neonProdDbSize', data.db_prod_size_mb ? `${data.db_prod_size_mb} MB` : '0.0 MB');
        setElText('neonProdDwDbSize', data.db_prod_dw_size_mb ? `${data.db_prod_dw_size_mb} MB` : '0.0 MB');
        setElText('neonProdTotalSize', data.db_prod_total_size_mb ? `${data.db_prod_total_size_mb} MB` : '0.0 MB');

        // Neon DB Compute / Network 바인딩 및 게이지 렌더러
        const updateMetricBar = (textId, barId, value, limit, unit) => {
            const textEl = document.getElementById(textId);
            const barEl = document.getElementById(barId);
            if (!textEl || !barEl) return;

            // 퍼센트 계산
            const pct = limit > 0 ? (value / limit) * 100 : 0;
            barEl.style.width = `${Math.min(100, pct)}%`;

            let colorClass = 'text-success';
            if (pct >= 100) {
                barEl.className = 'progress-bar bg-danger';
                colorClass = 'text-danger fw-bold';
            } else if (pct >= 80) {
                barEl.className = 'progress-bar bg-warning';
                colorClass = 'text-warning fw-bold';
            } else {
                barEl.className = 'progress-bar bg-success';
                colorClass = 'text-success';
            }

            // 단순 나열된 볼드를 지우고 비율(%)에 스타일 가중치를 적용하여 가독성을 높입니다.
            textEl.innerHTML = `<span>${value}</span><span class="text-muted" style="font-size: 0.72rem;"> / ${limit} ${unit}</span> <span class="${colorClass} ms-1" style="font-size: 0.75rem; font-weight: 600;">(${pct.toFixed(1)}%)</span>`;
        };

        // DEV 환경 바인딩
        updateMetricBar('neonDevDbCompute', 'neonDevDbComputeBar', data.db_dev_compute_hours || 0.0, 100, 'CU-hrs');
        updateMetricBar('neonDevDbNetwork', 'neonDevDbNetworkBar', data.db_dev_network_gb || 0.0, 5, 'GB');

        updateMetricBar('neonDevDwDbCompute', 'neonDevDwDbComputeBar', data.db_dev_dw_compute_hours || 0.0, 100, 'CU-hrs');
        updateMetricBar('neonDevDwDbNetwork', 'neonDevDwDbNetworkBar', data.db_dev_dw_network_gb || 0.0, 5, 'GB');

        // PROD 환경 바인딩
        updateMetricBar('neonProdDbCompute', 'neonProdDbComputeBar', data.db_prod_compute_hours || 0.0, 100, 'CU-hrs');
        updateMetricBar('neonProdDbNetwork', 'neonProdDbNetworkBar', data.db_prod_network_gb || 0.0, 5, 'GB');

        updateMetricBar('neonProdDwDbCompute', 'neonProdDwDbComputeBar', data.db_prod_dw_compute_hours || 0.0, 100, 'CU-hrs');
        updateMetricBar('neonProdDwDbNetwork', 'neonProdDwDbNetworkBar', data.db_prod_dw_network_gb || 0.0, 5, 'GB');

        // APP_ENV에 따라서 DEV 또는 PROD 행만 노출
        const isLocalEnv = (data.environment && (data.environment.toLowerCase() === 'local' || data.environment.toLowerCase() === 'dev'));

        // 리밋 도달 시 배너 경고 (각 개별 DB 프로젝트가 100% 이상 한도 도달했는지 체크)
        const isLimitReached = isLocalEnv
            ? (
                (data.db_dev_compute_hours || 0.0) >= 100 ||
                (data.db_dev_network_gb || 0.0) >= 5 ||
                (data.db_dev_dw_compute_hours || 0.0) >= 100 ||
                (data.db_dev_dw_network_gb || 0.0) >= 5
            )
            : (
                (data.db_prod_compute_hours || 0.0) >= 100 ||
                (data.db_prod_network_gb || 0.0) >= 5 ||
                (data.db_prod_dw_compute_hours || 0.0) >= 100 ||
                (data.db_prod_dw_network_gb || 0.0) >= 5
            );
        if (isLimitReached) {
            document.getElementById('pgStatus').className = 'badge bg-danger text-white';
            document.getElementById('pgStatus').textContent = '리밋 도달';
        } else {
            document.getElementById('pgStatus').className = `badge bg-${pgOk ? 'success' : 'danger'}`;
            document.getElementById('pgStatus').textContent = pgOk ? '정상' : '오류';
        }

        const devElements = ['neonDevRowGroup1', 'neonDevRowGroup2', 'neonDevRowGroup3'];
        const prodElements = ['neonProdRowGroup1', 'neonProdRowGroup2', 'neonProdRowGroup3'];

        devElements.forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.style.display = isLocalEnv ? 'table-row' : 'none';
            }
        });
        prodElements.forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.style.display = isLocalEnv ? 'none' : 'table-row';
            }
        });




        // 운영체제 표시 동적 반영
        if (data.environment === 'local' || data.environment === 'dev') {
            document.getElementById('osName').textContent = 'Windows';
        } else {
            document.getElementById('osName').textContent = 'Linux (Render)';
        }

        // Cloudinary 상태 및 리소스 반영
        const cloudinaryStatusEl = document.getElementById('cloudinaryStatus');
        if (cloudinaryStatusEl) {
            if (data.cloudinary_status === 'healthy') {
                cloudinaryStatusEl.className = 'badge bg-success';
                cloudinaryStatusEl.textContent = '정상';
            } else if (data.cloudinary_status === 'rate_limited') {
                cloudinaryStatusEl.className = 'badge bg-warning text-dark';
                cloudinaryStatusEl.textContent = 'API 제한';
            } else {
                cloudinaryStatusEl.className = 'badge bg-danger';
                cloudinaryStatusEl.textContent = '오류';
            }

            // 크레딧(Credit) 데이터 바인딩 (음수값 0으로 보정)
            const credUsage = Math.max(0, data.cloudinary_credits_usage || 0.0);
            const credLimit = data.cloudinary_credits_limit || 25.0;
            const credPercent = Math.max(0, data.cloudinary_credits_percent || 0.0);

            let credColorClass = 'text-success';
            if (credPercent >= 80) credColorClass = 'text-danger fw-bold';
            else if (credPercent >= 50) credColorClass = 'text-warning fw-bold';

            document.getElementById('cloudinaryCreditsUsage').innerHTML = 
                `<span>${credUsage.toFixed(2)}</span><span class="text-muted" style="font-size: 0.72rem;"> / ${credLimit.toFixed(0)}</span> <span class="${credColorClass} ms-1" style="font-size: 0.75rem; font-weight: 600;">(${credPercent.toFixed(1)}%)</span>`;

            // GB 단위 변환 (usage_bytes / 1024^3) (음수값 0으로 보정)
            const usageBytes = Math.max(0, data.cloudinary_usage_bytes || 0);
            const usageGB = (usageBytes / 1024 / 1024 / 1024).toFixed(3);
            document.getElementById('cloudinaryUsage').innerHTML = `<span>${usageGB} GB</span>`;

            // 대역폭(Bandwidth) 데이터 바인딩
            const bwUsageBytes = Math.max(0, data.cloudinary_bandwidth_usage_bytes || 0);
            const bwLimitBytes = data.cloudinary_bandwidth_limit_bytes || (25 * 1024 * 1024 * 1024);
            const bwUsageGB = (bwUsageBytes / 1024 / 1024 / 1024).toFixed(3);
            const bwLimitGB = (bwLimitBytes / 1024 / 1024 / 1024).toFixed(0);
            const bwPercent = Math.max(0, data.cloudinary_bandwidth_percent || 0.0);
            
            let bwColorClass = 'text-success';
            if (bwPercent >= 80) bwColorClass = 'text-danger fw-bold';
            else if (bwPercent >= 50) bwColorClass = 'text-warning fw-bold';

            const bwEl = document.getElementById('cloudinaryBandwidth');
            if (bwEl) {
                bwEl.innerHTML = 
                    `<span>${bwUsageGB}</span><span class="text-muted" style="font-size: 0.72rem;"> / ${bwLimitGB} GB</span> <span class="${bwColorClass} ms-1" style="font-size: 0.75rem; font-weight: 600;">(${bwPercent.toFixed(1)}%)</span>`;
            }

            const resourcesCount = Math.max(0, data.cloudinary_resources_count || 0);
            document.getElementById('cloudinaryResources').innerHTML = `<span>${resourcesCount.toLocaleString()}개</span>`;

            // 남은 크레딧 계산 및 클래스 바인딩
            const freeCredits = Math.max(0, credLimit - credUsage);
            const freePercent = 100 - credPercent;

            const freeEl = document.getElementById('cloudinaryCreditsFree');
            if (freeEl) {
                freeEl.textContent = `${freeCredits.toFixed(2)} Credit 남음`;
                if (freePercent < 10) {
                    freeEl.className = 'text-danger fw-bold small';
                } else if (freePercent < 30) {
                    freeEl.className = 'text-warning fw-bold small';
                } else {
                    freeEl.className = 'text-success fw-bold small';
                }
            }
        }

        // HuggingFace Space 상태 반영
        const hfOk = data.hf_status === 'healthy';
        const hfStatusEl = document.getElementById('hfStatus');
        if (hfStatusEl) {
            if (data.hf_status === 'sleeping') {
                hfStatusEl.className = 'badge bg-warning text-dark';
                hfStatusEl.textContent = '대기 모드';
            } else {
                hfStatusEl.className = `badge bg-${hfOk ? 'success' : 'danger'}`;
                hfStatusEl.textContent = hfOk ? '정상' : '오류';
            }

            document.getElementById('hfModelStatus').textContent = data.hf_model_status || '-';
            document.getElementById('hfLatency').textContent = data.hf_latency_ms ? `${data.hf_latency_ms} ms` : '-';

            // Git Storage 사용량 정보 반영 (HuggingFace 콘솔은 10진수 Decimal MB/GB 단위를 사용하므로 10^6 및 10^9 기준으로 계산)
            const hfUsedBytes = data.hf_used_storage_bytes || 0;
            const hfLimitBytes = data.hf_storage_limit_bytes || (1024 * 1024 * 1024);
            const hfUsedMB = (hfUsedBytes / 1000000).toFixed(1);
            const hfLimitGB = (hfLimitBytes / 1000000000).toFixed(0);
            const hfHw = data.hf_hardware ? ` ${data.hf_hardware}` : '';

            const hfModelEl = document.getElementById('hfModel');
            if (hfModelEl) {
                hfModelEl.textContent = data.hf_hardware || '-';
            }

            const hfStageEl = document.getElementById('hfRuntimeStage');
            if (hfStageEl) {
                const stage = data.hf_runtime_stage || 'unknown';
                hfStageEl.textContent = stage;
                if (stage.toUpperCase() === 'RUNNING') {
                    hfStageEl.className = 'text-success fw-bold text-nowrap';
                } else if (stage.toUpperCase() === 'BUILDING') {
                    hfStageEl.className = 'text-warning fw-bold text-nowrap';
                } else {
                    hfStageEl.className = 'text-danger fw-bold text-nowrap';
                }
            }

            const hfResourceEl = document.getElementById('hfResourceUsage');
            if (hfResourceEl) {
                const cpu = data.hf_cpu_usage_pct !== undefined ? `${data.hf_cpu_usage_pct.toFixed(0)}%` : '-';
                const ram_used = data.hf_mem_used_mb !== undefined ? `${(data.hf_mem_used_mb / 1024).toFixed(1)}` : '-';
                const ram_total = data.hf_mem_total_gb !== undefined ? `${data.hf_mem_total_gb}GB` : '-';
                hfResourceEl.innerHTML = 
                    `<span class="text-muted" style="font-size: 0.72rem;">CPU:</span> <span class="text-dark me-2" style="font-size: 0.75rem; font-weight: 600;">${cpu}</span>` +
                    `<span class="text-muted" style="font-size: 0.72rem;">| RAM:</span> <span class="text-dark" style="font-size: 0.75rem; font-weight: 600;">${ram_used}<span class="text-muted" style="font-size: 0.7rem; font-weight: normal;">/${ram_total}</span></span>`;
            }

            const hfStorageEl = document.getElementById('hfStorageUsage');
            if (hfStorageEl) {
                hfStorageEl.textContent = `${hfUsedMB} MB / ${hfLimitGB} GB`;
            }
        }
    } catch (e) {
        console.error('데이터베이스 상태 조회 실패:', e);
    }
}

// 메트릭 통계 (Neon DB 평균)
async function fetchStats() {
    try {
        const resp = await fetch('/api/metrics/stats');
        const data = await resp.json();

        let totalCpu = 0, totalMem = 0, count = 0;
        let maxMem = 0, maxMemService = '-';
        let maxCpu = 0, maxCpuService = '-';

        for (const [svc, stats] of Object.entries(data)) {
            totalCpu += stats.avg_cpu;
            totalMem += stats.avg_mem;
            count++;

            if (stats.max_mem_mb > maxMem) {
                maxMem = stats.max_mem_mb;
                maxMemService = svc;
            }
            let cpuVal = stats.max_cpu || stats.avg_cpu;
            if (cpuVal > maxCpu) {
                maxCpu = cpuVal;
                maxCpuService = svc;
            }
        }

        if (count > 0) {
            document.getElementById('avgCpu').innerText = (totalCpu / count).toFixed(1) + '%';
            document.getElementById('avgMem').innerText = (totalMem / count).toFixed(1) + '%';
            document.getElementById('maxMemVal').innerText = maxMem.toFixed(0) + ' MB';
            document.getElementById('maxMemDetail').innerText = '사용 1위: ' + maxMemService;
            if (document.getElementById('maxCpuVal')) {
                document.getElementById('maxCpuVal').innerText = maxCpu.toFixed(1) + '%';
                document.getElementById('maxCpuDetail').innerText = '사용 1위: ' + maxCpuService;
            }
        }
    } catch (e) {
        console.error('Stats error', e);
    }
}

// 메트릭 스트림 (Neon DB 시계열)
async function fetchStream() {
    try {
        const resp = await fetch('/api/metrics/stream');
        const data = await resp.json();
        const logs = (data.metrics || []);

        if (!logs.length) return;

        updateCharts(logs);
        updateTable(logs);
    } catch (e) {
        console.error('Stream error', e);
    }
}

/**
 * 각각의 도커 컨테이너가 최근 뱉어낸 시계열 메트릭(CPU, Memory)을 Elasticsearch에서 긁어와 차트 위젯에 주입함.
 * 컨테이너 간의 자원 경합이나 특정 서비스의 메모리 누수 버그를 실시간 그래프 교차 분석으로 찾게 도와줌.
 * @param {Array} logs 백엔드 응답을 배열로 변환한 데이터
 */
function updateCharts(logs) {
    const uniqueTimes = [...new Set(logs.map(l => l.timestamp))].sort();
    const services = [...new Set(logs.map(l => l.service))];

    // [성능 최적화] Map 1회 빌드 → O(1) 조회 (기존 Array.find O(N×M) 제거)
    const dataMap = new Map();
    logs.forEach(l => dataMap.set(`${l.service}|${l.timestamp}`, l));

    const datasetsCpu = [];
    const datasetsMem = [];

    services.forEach((svc, idx) => {
        const color = CHART_COLORS[idx % CHART_COLORS.length];
        const dataCpu = [];
        const dataMem = [];

        uniqueTimes.forEach(t => {
            const entry = dataMap.get(`${svc}|${t}`);
            if (entry) {
                dataCpu.push(entry.cpu_percent);
                dataMem.push(entry.memory_usage / 1024 / 1024);
            } else {
                dataCpu.push(null);
                dataMem.push(null);
            }
        });

        datasetsCpu.push({
            label: svc, borderColor: color, backgroundColor: color,
            _originalColor: color,
            data: dataCpu, borderWidth: 1.5, tension: 0.4, fill: false
        });
        datasetsMem.push({
            label: svc, borderColor: color, backgroundColor: color,
            _originalColor: color,
            data: dataMem, borderWidth: 1.5, tension: 0.4, fill: false
        });
    });

    cpuChart.data.labels = uniqueTimes;
    cpuChart.data.datasets = datasetsCpu;
    cpuChart.update('none'); // [성능] 애니메이션 비활성화

    memChart.data.labels = uniqueTimes;
    memChart.data.datasets = datasetsMem;
    memChart.update('none'); // [성능] 애니메이션 비활성화
}

function updateTable(logs) {
    const latestMap = {};
    logs.forEach(log => { latestMap[log.container] = log; });

    const tbody = document.getElementById('metricsTableBody');
    tbody.innerHTML = '';

    Object.values(latestMap).sort((a, b) => a.service.localeCompare(b.service)).forEach(log => {
        const tr = document.createElement('tr');
        const ts = log.timestamp.endsWith('Z') ? log.timestamp : log.timestamp + 'Z';
        const timeStr = new Date(ts).toLocaleString('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const memMB = (log.memory_usage / 1024 / 1024).toFixed(1);
        let limitStr = "";
        if (log.memory_limit && log.memory_limit > 0) {
            const limitMB = (log.memory_limit / 1024 / 1024).toFixed(1);
            limitStr = ` / ${limitMB}`;
        }

        let cpuClass = "";
        if (log.cpu_percent > 80) cpuClass = "text-danger fw-bold";
        else if (log.cpu_percent > 50) cpuClass = "text-warning fw-bold";

        tr.innerHTML = `
                <td><span class="badge bg-light text-dark border">${log.service}</span></td>
                <td class="small">${log.container}</td>
                <td class="${cpuClass} text-end pe-4">${log.cpu_percent.toFixed(2)} %</td>
                <td class="text-end pe-4">${log.memory_percent.toFixed(2)} %</td>
                <td class="text-end pe-4">${memMB}${limitStr} MB</td>
                <td class="text-muted small text-end pe-4">${timeStr}</td>
            `;
        tbody.appendChild(tr);
    });
}

// 페이지 언로드 시 인터벌 정리
window.addEventListener('beforeunload', () => {
    if (autoRefreshInterval) clearInterval(autoRefreshInterval);
});