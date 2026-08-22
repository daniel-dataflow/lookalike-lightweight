let systemRefreshInterval = null;
let dbCloudRefreshInterval = null;
let cpuChart = null;
let memChart = null;
let systemAutoDisableTimer = null;
let dbAutoDisableTimer = null;
const AUTO_DISABLE_MS = 60 * 60 * 1000; // 1 hour
const CHART_COLORS = [
    '#3366CC', '#22B573', '#FF8C1A', '#E63946', '#8E44AD', '#0EA5A0', '#D63384'
];

// 백엔드로부터 정확한 서비스 표시 이름이 넘어오므로 프론트엔드 매핑은 제거합니다.

document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    
    // 초기 로딩 시 전체 한 번 패치
    fetchSystemHealth();
    fetchStats();
    fetchStream();
    fetchDbStatus();

    // 개별 자동 갱신 스위치 요소 획득
    const systemSwitch = document.getElementById('systemRefreshSwitch');
    const dbCloudSwitch = document.getElementById('dbCloudRefreshSwitch');

    // 자동 새로고침 기본값 비활성화
    // 단, 사용자가 활성화한 경우 로컬스토리지에 저장된 활성화 시각이 1시간 이내면 복원
    const SYS_KEY = 'systemRefreshEnabledAt';
    const DB_KEY = 'dbCloudRefreshEnabledAt';
    function isStillEnabled(key) {
        try {
            const ts = localStorage.getItem(key);
            if (!ts) return false;
            const t = parseInt(ts, 10);
            if (isNaN(t)) { localStorage.removeItem(key); return false; }
            if (Date.now() - t < AUTO_DISABLE_MS) return true;
            localStorage.removeItem(key);
            return false;
        } catch (e) {
            return false;
        }
    }

    if (isStillEnabled(SYS_KEY)) {
        systemSwitch.checked = true;
        startSystemRefresh();
    } else {
        systemSwitch.checked = false;
    }

    if (isStillEnabled(DB_KEY)) {
        dbCloudSwitch.checked = true;
        startDbCloudRefresh();
    } else {
        dbCloudSwitch.checked = false;
    }

    updateManualBtnState();

    systemSwitch.addEventListener('change', (e) => {
        if (e.target.checked) {
            startSystemRefresh();
        } else {
            stopSystemRefresh();
        }
        updateManualBtnState();
    });

    dbCloudSwitch.addEventListener('change', (e) => {
        if (e.target.checked) {
            startDbCloudRefresh();
        } else {
            stopDbCloudRefresh();
        }
        updateManualBtnState();
    });
});

function startSystemRefresh() {
    if (systemRefreshInterval) clearInterval(systemRefreshInterval);
    systemRefreshInterval = setInterval(refreshSystemData, 10000); // 10초마다
    try { localStorage.setItem('systemRefreshEnabledAt', Date.now().toString()); } catch (e) {}
    if (systemAutoDisableTimer) clearTimeout(systemAutoDisableTimer);
    systemAutoDisableTimer = setTimeout(() => {
        const sw = document.getElementById('systemRefreshSwitch');
        if (sw) sw.checked = false;
        stopSystemRefresh();
        try { localStorage.removeItem('systemRefreshEnabledAt'); } catch (e) {}
        updateManualBtnState();
    }, AUTO_DISABLE_MS);
}

function stopSystemRefresh() {
    if (systemRefreshInterval) clearInterval(systemRefreshInterval);
    systemRefreshInterval = null;
    if (systemAutoDisableTimer) { clearTimeout(systemAutoDisableTimer); systemAutoDisableTimer = null; }
    try { localStorage.removeItem('systemRefreshEnabledAt'); } catch (e) {}
}

function startDbCloudRefresh() {
    if (dbCloudRefreshInterval) clearInterval(dbCloudRefreshInterval);
    dbCloudRefreshInterval = setInterval(refreshDbCloudData, 600000); // 10분마다 (과도한 쿼리 방지)
    try { localStorage.setItem('dbCloudRefreshEnabledAt', Date.now().toString()); } catch (e) {}
    if (dbAutoDisableTimer) clearTimeout(dbAutoDisableTimer);
    dbAutoDisableTimer = setTimeout(() => {
        const sw = document.getElementById('dbCloudRefreshSwitch');
        if (sw) sw.checked = false;
        stopDbCloudRefresh();
        try { localStorage.removeItem('dbCloudRefreshEnabledAt'); } catch (e) {}
        updateManualBtnState();
    }, AUTO_DISABLE_MS);
}

function stopDbCloudRefresh() {
    if (dbCloudRefreshInterval) clearInterval(dbCloudRefreshInterval);
    dbCloudRefreshInterval = null;
    if (dbAutoDisableTimer) { clearTimeout(dbAutoDisableTimer); dbAutoDisableTimer = null; }
    try { localStorage.removeItem('dbCloudRefreshEnabledAt'); } catch (e) {}
}

function updateManualBtnState() {
    const systemSwitch = document.getElementById('systemRefreshSwitch');
    const dbCloudSwitch = document.getElementById('dbCloudRefreshSwitch');
    const manualBtn = document.getElementById('btnManualRefresh');
    if (!manualBtn) return;
    
    // 둘 다 자동갱신이 켜져있을 때만 수동 새로고침 버튼을 비활성화
    if (systemSwitch && dbCloudSwitch && systemSwitch.checked && dbCloudSwitch.checked) {
        manualBtn.disabled = true;
    } else {
        manualBtn.disabled = false;
    }
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
 * 서버 시스템 지표만 단독 갱신 (10초 주기)
 */
async function refreshSystemData() {
    await fetchSystemHealth();
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('ko-KR');
}

/**
 * 무거운 DB 통계 및 Cloudinary, HuggingFace Space 지표 갱신 (10분 주기)
 */
async function refreshDbCloudData() {
    await Promise.all([
        fetchStats(),
        fetchStream()
    ]);
    await fetchDbStatus();
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('ko-KR');
}

/**
 * 수동 전체 새로고침
 */
async function refreshDataManual() {
    const manualBtn = document.getElementById('btnManualRefresh');
    if (manualBtn) manualBtn.disabled = true;
    
    try {
        await Promise.all([
            fetchSystemHealth(),
            fetchStats(),
            fetchStream()
        ]);
        await fetchDbStatus();
        document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('ko-KR');
    } catch (e) {
        console.error('수동 새로고침 실패:', e);
    } finally {
        updateManualBtnState();
    }
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

        // CPU 코어 및 사양 정보 출력: [실제 스펙 기준 클록] | [실시간 작동클록] | (코어/스레드)
        const cpuInfo = [];
        
        const curGHz = m.cpu_freq_current > 0 ? (m.cpu_freq_current / 1000).toFixed(2) : null;
        const baseGHz = m.cpu_freq_base > 0 ? (m.cpu_freq_base / 1000).toFixed(2) : null;
        const maxGHz = m.cpu_freq_max > 0 ? (m.cpu_freq_max / 1000).toFixed(2) : null;

        // 정식 스펙 클록 (예: 1.60GHz)
        const specGHz = baseGHz || maxGHz;
        if (specGHz) {
            if (curGHz && curGHz !== specGHz) {
                cpuInfo.push(`${curGHz} / ${specGHz}GHz`);
            } else {
                cpuInfo.push(`${specGHz}GHz`);
            }
        } else if (curGHz) {
            cpuInfo.push(`${curGHz}GHz`);
        }

        // CPU 할당 정보 표기 (클라우드/컨테이너 제한이 있는 경우만 '0.15 CPU' 표기, 로컬 100% 할당 시 생략)
        if (m.cpu_limit !== undefined && m.cpu_limit !== null && m.cpu_limit > 0) {
            cpuInfo.push(`${m.cpu_limit} CPU`);
        }
        if (m.cpu_cores_physical > 0 && m.cpu_cores_logical > 0) {
            cpuInfo.push(`(${m.cpu_cores_physical}C/${m.cpu_cores_logical}T)`);
        }
        const cpuText = cpuInfo.join(' | ').replace(' | (', ' (') || 'CPU 정보 없음';
        const cpuDetailEl = document.getElementById('cpuDetail');
        if (cpuDetailEl) {
            cpuDetailEl.textContent = cpuText;
            cpuDetailEl.setAttribute('title', cpuText);
        }

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

        // DW DB 스위칭 상태 UI 바인딩 (DEV_DW_DB_2 또는 PROD_DW_DB_2 전환 인지 표시)
        const isDwSecondary = data.active_dw_target === 'secondary';
        const activeLabel = data.active_dw_label || (isDwSecondary ? 'DW_DB_2' : 'DW_DB');
        
        const devDwLabelEl = document.getElementById('neonDevDwDbLabel');
        if (devDwLabelEl) {
            if (isDwSecondary) {
                devDwLabelEl.innerHTML = `<span class="text-warning fw-bold">${activeLabel}</span> <span class="badge bg-warning text-dark ms-1" style="font-size: 0.65rem;">DW_2 전환됨</span>`;
            } else {
                devDwLabelEl.textContent = 'DEV_DW_DB';
            }
        }

        const prodDwLabelEl = document.getElementById('neonProdDwDbLabel');
        if (prodDwLabelEl) {
            if (isDwSecondary) {
                prodDwLabelEl.innerHTML = `<span class="text-warning fw-bold">${activeLabel}</span> <span class="badge bg-warning text-dark ms-1" style="font-size: 0.65rem;">DW_2 전환됨</span>`;
            } else {
                prodDwLabelEl.textContent = 'PROD_DW_DB';
            }
        }

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
        updateMetricBar('neonDevDbCompute', 'neonDevDbComputeBar', data.db_dev_compute_hours || 0.0, 100, 'CU');
        updateMetricBar('neonDevDbNetwork', 'neonDevDbNetworkBar', data.db_dev_network_gb || 0.0, 5, 'GB');

        updateMetricBar('neonDevDwDbCompute', 'neonDevDwDbComputeBar', data.db_dev_dw_compute_hours || 0.0, 100, 'CU');
        updateMetricBar('neonDevDwDbNetwork', 'neonDevDwDbNetworkBar', data.db_dev_dw_network_gb || 0.0, 5, 'GB');

        // PROD 환경 바인딩
        updateMetricBar('neonProdDbCompute', 'neonProdDbComputeBar', data.db_prod_compute_hours || 0.0, 100, 'CU');
        updateMetricBar('neonProdDbNetwork', 'neonProdDbNetworkBar', data.db_prod_network_gb || 0.0, 5, 'GB');

        updateMetricBar('neonProdDwDbCompute', 'neonProdDwDbComputeBar', data.db_prod_dw_compute_hours || 0.0, 100, 'CU');
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




        // 운영체제 표시 동적 반영 (백엔드 실제 파싱 운영체제 명칭 바인딩 및 툴팁 제공)
        const dynamicOs = (data && data.os_name) || (metrics && metrics.length > 0 && metrics[0].os_name) || null;
        const osEl = document.getElementById('osName');
        if (osEl && dynamicOs) {
            osEl.textContent = dynamicOs;
            osEl.setAttribute('title', dynamicOs);
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

            // 1. Cloudinary 통합 크레딧 정밀 연산 및 바인딩
            const usageBytes = Math.max(0, data.cloudinary_usage_bytes || 0);
            const bwUsageBytes = Math.max(0, data.cloudinary_bandwidth_usage_bytes || 0);
            const transformations = data.cloudinary_transformations_usage || 0;

            const storage_gb = usageBytes / 1024 / 1024 / 1024;
            const bandwidth_gb = bwUsageBytes / 1024 / 1024 / 1024;
            const bandwidth_mb = bwUsageBytes / 1024 / 1024;
            
            // 크레딧 소모량 공식 적용 (소수점 3자리 정밀도 통일하여 연산 정합성 보장)
            const storage_credit = parseFloat(storage_gb.toFixed(3));
            const bandwidth_credit = parseFloat(bandwidth_gb.toFixed(3));
            const trans_credit = parseFloat((transformations / 1000).toFixed(3));
            
            // 3대 자원 크레딧 합산
            const totalCreditUsed = parseFloat((storage_credit + bandwidth_credit + trans_credit).toFixed(3));
            const credLimit = 25.0;
            const credPercent = Math.min(100, (totalCreditUsed / credLimit) * 100);

            let credColorClass = 'text-success';
            let barColorClass = 'progress-bar bg-success';
            if (credPercent >= 80) {
                credColorClass = 'text-danger fw-bold';
                barColorClass = 'progress-bar bg-danger';
            } else if (credPercent >= 50) {
                credColorClass = 'text-warning fw-bold';
                barColorClass = 'progress-bar bg-warning';
            }

            // Total Credit 사용량 텍스트 출력 (소수점 3자리로 상세 매칭)
            document.getElementById('cloudinaryCreditsUsage').innerHTML = 
                `<span>${totalCreditUsed.toFixed(3)}</span><span class="text-muted" style="font-size: 0.72rem;"> / ${credLimit.toFixed(0)} Credit</span> <span class="${credColorClass} ms-1" style="font-size: 0.75rem; font-weight: 600;">(${credPercent.toFixed(1)}%)</span>`;

            // 게이지 바 동적 반영
            const progressEl = document.getElementById('cloudinaryCreditsProgress');
            if (progressEl) {
                progressEl.style.width = `${credPercent}%`;
                progressEl.className = barColorClass;
            }

            // 2. 하위 개별 실사용량 지표 및 크레딧 소모량 바인딩 (-> 기가 단일화 및 텍스트 간소화로 잘림 해결)
            document.getElementById('cloudinaryUsage').innerHTML = 
                `<span>${storage_gb.toFixed(3)} GB</span> <span class="text-muted small ms-1" style="font-size: 0.72rem;">➔ ${storage_credit.toFixed(3)} Credit</span>`;
            
            const bwEl = document.getElementById('cloudinaryBandwidth');
            if (bwEl) {
                bwEl.innerHTML = 
                    `<span>${bandwidth_gb.toFixed(3)} GB</span> <span class="text-muted text-nowrap" style="font-size: 0.72rem;">➔ ${bandwidth_credit.toFixed(3)} Credit</span>`;
            }

            const transEl = document.getElementById('cloudinaryTransformations');
            if (transEl) {
                transEl.innerHTML = 
                    `<span>${transformations.toLocaleString()} 회</span> <span class="text-muted text-nowrap" style="font-size: 0.72rem;">➔ ${trans_credit.toFixed(3)} Credit</span>`;
            }

            // 3. 최종 합산 사용량 출력 (중복 공식 제거하고 값만 심플하게 노출)
            const formulaEl = document.getElementById('cloudinaryCreditsFormula');
            if (formulaEl) {
                formulaEl.textContent = `${totalCreditUsed.toFixed(3)} Credit`;
            }

            // 4. 남은 가용 크레딧 출력
            const freeCredits = parseFloat((credLimit - totalCreditUsed).toFixed(3));
            const freePercent = 100 - credPercent;

            const freeEl = document.getElementById('cloudinaryCreditsFree');
            if (freeEl) {
                freeEl.textContent = `${freeCredits.toFixed(3)} Credit 남음`;
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
            // [단위 변환] 메가(MB) 표시 말고 기가(GB) 표시로 1024 변환 적용
            const maxMemGB = maxMem / 1024;
            document.getElementById('maxMemVal').innerText = maxMemGB.toFixed(1) + ' GB';
            // [영역 압축] '사용 1위: '에서 '사용 '을 제거하여 가로폭을 확보, 카드 클리핑 방지
            document.getElementById('maxMemDetail').innerText = '1위: ' + maxMemService;
            if (document.getElementById('maxCpuVal')) {
                document.getElementById('maxCpuVal').innerText = maxCpu.toFixed(1) + '%';
                document.getElementById('maxCpuDetail').innerText = '1위: ' + maxCpuService;
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
    if (systemRefreshInterval) clearInterval(systemRefreshInterval);
    if (dbCloudRefreshInterval) clearInterval(dbCloudRefreshInterval);
});