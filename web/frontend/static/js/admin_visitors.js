    // 전역 상태 변수
    let currentSegment = 'all';  // 'all', 'general', 'owner'
    let currentDays = 1;         // 1, 7, 30
    let cachedOverviewData = null;
    let cachedGeoData = null;
    let map = null;
    let markersLayer = null;
    let realtimeTimer = null;
    let sessionList = [];

    // 페이지 로드 시 초기화
    document.addEventListener("DOMContentLoaded", function () {
        loadAllData();
        initMap();
        startRealtimePolling();
    });

    // 지도 인프라 초기화
    function initMap() {
        // 서울 좌표 중심으로 초기 줌 설정
        map = L.map('visitorMap').setView([36.2, 127.8], 7);
        
        // OpenStreetMap 타일 레이어 등록
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 18,
            attribution: '© OpenStreetMap contributors'
        }).addTo(map);

        markersLayer = L.layerGroup().addTo(map);
    }

    // 탭 이동 시 지도 렌더링 깨짐 방지
    function invalidateMapSize() {
        setTimeout(function() {
            if (map) {
                map.invalidateSize();
            }
        }, 300);
    }

    // 세그먼트 전환 핸들러 (전체 / 일반 / OWNER)
    function changeSegment(segment) {
        currentSegment = segment;
        
        // 버튼 활성화 스타일 전환
        document.querySelectorAll('.segment-btn').forEach(btn => btn.classList.remove('active'));
        if (segment === 'all') document.getElementById('segAll').classList.add('active');
        if (segment === 'general') document.getElementById('segGeneral').classList.add('active');
        if (segment === 'owner') document.getElementById('segOwner').classList.add('active');

        // 캐싱된 통계 데이터 리렌더링
        renderStats();
        renderGeoStats();
    }

    // 기간 선택 변경 핸들러
    function handlePeriodChange() {
        const select = document.getElementById("periodSelect");
        const customDiv = document.getElementById("customDateRange");
        
        if (select.value === "custom") {
            customDiv.classList.remove("d-none");
            customDiv.classList.add("d-flex");
            // 오늘 날짜를 기본값으로 세팅
            const todayStr = new Date().toISOString().split('T')[0];
            document.getElementById("startDateSelect").value = todayStr;
            document.getElementById("endDateSelect").value = todayStr;
        } else {
            customDiv.classList.remove("d-flex");
            customDiv.classList.add("d-none");
        }
        loadAllData();
    }

    // 전체 API 데이터 로드
    async function loadAllData() {
        const spinner = document.getElementById("loadingSpinner");
        spinner.classList.remove("d-none");
        
        const periodVal = document.getElementById("periodSelect").value;
        let queryParams = "";
        
        if (periodVal === "custom") {
            const start = document.getElementById("startDateSelect").value;
            const end = document.getElementById("endDateSelect").value;
            if (!start || !end) {
                spinner.classList.add("d-none");
                return; // 날짜가 모두 채워지기 전까지는 조회 대기
            }
            queryParams = `start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}`;
        } else {
            queryParams = `days=${periodVal}`;
        }

        try {
            // 병렬 API 호출
            const [overviewRes, geoRes, sessionsRes] = await Promise.all([
                fetch(`/api/admin/visitors/overview?${queryParams}`).then(r => r.json()),
                fetch(`/api/admin/visitors/geo?${queryParams}`).then(r => r.json()),
                fetch(`/api/admin/visitors/sessions?${queryParams}`).then(r => r.json())
            ]);

            if (overviewRes.success) {
                cachedOverviewData = overviewRes;
                currentClientIp = overviewRes.client_ip || "Unknown";
                renderStats();
            }
            if (geoRes.success) {
                cachedGeoData = geoRes;
                renderGeoStats();
            }
            if (sessionsRes.success) {
                sessionList = sessionsRes.sessions;
                renderSessions();
            }
            
            // 실시간 로그 1회 로딩
            await loadRealtimeLogs();

        } catch (err) {
            console.error("데이터 로딩 실패:", err);
            alert("방문자 데이터 조회 실패: 서버 연결 상태를 확인해주세요.");
        } finally {
            spinner.classList.add("d-none");
        }
    }

    // 1. 개요 통계 화면 출력
    // Chart.js 인스턴스 보관용 전역 변수
    let browserChartInstance = null;
    let osChartInstance = null;

    function renderStats() {
        if (!cachedOverviewData) return;

        // 선택된 세그먼트 데이터 추출 (all, general, owner)
        const stats = cachedOverviewData[currentSegment];
        if (!stats) return;

        // 메트릭 숫자 갱신
        document.getElementById("metricTotalVisits").innerText = stats.total_visits.toLocaleString() + "회";
        document.getElementById("metricUniqueUsers").innerText = stats.unique_visitors.toLocaleString() + "명";
        document.getElementById("metricActiveSessions").innerText = stats.active_sessions.toLocaleString() + "개";
        document.getElementById("metricAvgPageviews").innerText = stats.avg_pageviews + " PV";

        // 인기 검색 유입 경로 갱신
        const pagesTbody = document.getElementById("popularPagesTableBody");
        pagesTbody.innerHTML = "";

        if (!stats.popular_pages || stats.popular_pages.length === 0 || stats.popular_pages[0].visits === 0) {
            pagesTbody.innerHTML = `<tr><td colspan="4" class="text-center py-4 text-muted">지정 기간 내 유입 정보가 없습니다.</td></tr>`;
        } else {
            stats.popular_pages.forEach((p, idx) => {
                let badgeClass = "bg-secondary";
                if (idx === 0) badgeClass = "bg-danger";
                else if (idx === 1) badgeClass = "bg-warning text-dark";
                else if (idx === 2) badgeClass = "bg-info text-dark";

                pagesTbody.innerHTML += `
                    <tr>
                        <td class="text-center"><span class="badge ${badgeClass}">${idx + 1}</span></td>
                        <td class="font-monospace small text-break text-primary">${escapeHtml(p.page)}</td>
                        <td class="text-end fw-semibold">${p.visits.toLocaleString()}회</td>
                        <td>
                            <div class="d-flex align-items-center gap-2">
                                <div class="progress flex-grow-1" style="height: 6px;">
                                    <div class="progress-bar bg-primary" role="progressbar" style="width: ${p.percent}%"></div>
                                </div>
                                <small class="text-muted font-monospace" style="width: 45px; text-align: right;">${p.percent}%</small>
                            </div>
                        </td>
                    </tr>
                `;
            });
        }

        // 차트 그리기
        renderCharts(stats.browsers, stats.oss);
    }

    // Chart.js 렌더링
    function renderCharts(browsersData, osData) {
        // 기존 차트 객체 제거 (파괴 후 재생성 필요)
        if (browserChartInstance) browserChartInstance.destroy();
        if (osChartInstance) osChartInstance.destroy();

        // 1. 브라우저 점유율 차트
        const browserCanvas = document.getElementById("browserChart");
        const browserLabels = Object.keys(browsersData);
        const browserValues = Object.values(browsersData);
        
        browserChartInstance = new Chart(browserCanvas, {
            type: 'doughnut',
            data: {
                labels: browserLabels.length ? browserLabels : ["데이터 없음"],
                datasets: [{
                    data: browserValues.length ? browserValues : [1],
                    backgroundColor: [
                        '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#6b7280'
                    ],
                    borderWidth: 2,
                    borderColor: '#ffffff'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'right',
                        labels: { boxWidth: 12, font: { weight: 600 } }
                    }
                },
                cutout: '65%'
            }
        });

        // 2. OS 점유율 차트
        const osCanvas = document.getElementById("osChart");
        const osLabels = Object.keys(osData);
        const osValues = Object.values(osData);

        osChartInstance = new Chart(osCanvas, {
            type: 'pie',
            data: {
                labels: osLabels.length ? osLabels : ["데이터 없음"],
                datasets: [{
                    data: osValues.length ? osValues : [1],
                    backgroundColor: [
                        '#10b981', '#3b82f6', '#8b5cf6', '#f59e0b', '#ef4444', '#6b7280'
                    ],
                    borderWidth: 2,
                    borderColor: '#ffffff'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'right',
                        labels: { boxWidth: 12, font: { weight: 600 } }
                    }
                }
            }
        });
    }

    // 2. 세션 목록 렌더링
    function renderSessions() {
        const tbody = document.getElementById("sessionsTableBody");
        tbody.innerHTML = "";

        // 현재 선택된 세그먼트에 맞춘 세션 필터링
        // (전체: 모두, 일반: owner가 아님, owner: owner임)
        let filtered = sessionList;
        if (currentSegment === 'general') {
            filtered = sessionList.filter(s => !s.is_owner);
        } else if (currentSegment === 'owner') {
            filtered = sessionList.filter(s => s.is_owner);
        }

        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="text-center py-4 text-muted">분석 조건에 맞는 세션 내역이 없습니다.</td></tr>`;
            return;
        }

        filtered.forEach(s => {
            // OWNER 배지 및 KT-iptime 여부
            let ipLabel = `<strong>${escapeHtml(s.ip)}</strong>`;
            if (s.is_owner) {
                ipLabel += ` <span class="badge bg-warning text-dark ms-1" style="font-size:0.7rem;">OWNER</span>`;
            }

            // 기기 아이콘 매핑
            let deviceIcons = "";
            if (s.os === "Windows") deviceIcons += `<i class="fab fa-windows text-primary me-1" title="Windows"></i>`;
            else if (s.os === "macOS") deviceIcons += `<i class="fab fa-apple text-dark me-1" title="macOS"></i>`;
            else if (s.os === "Android") deviceIcons += `<i class="fab fa-android text-success me-1" title="Android"></i>`;
            else if (s.os === "iOS") deviceIcons += `<i class="fas fa-mobile-alt text-dark me-1" title="iOS (iPhone)"></i>`;
            else deviceIcons += `<i class="fas fa-laptop text-secondary me-1" title="기타 OS"></i>`;

            if (s.browser === "Chrome") deviceIcons += `<i class="fab fa-chrome text-warning" title="Chrome"></i>`;
            else if (s.browser === "Safari") deviceIcons += `<i class="fab fa-safari text-primary" title="Safari"></i>`;
            else if (s.browser === "Firefox") deviceIcons += `<i class="fab fa-firefox text-danger" title="Firefox"></i>`;
            else if (s.browser === "Edge") deviceIcons += `<i class="fab fa-edge text-info" title="Edge"></i>`;
            else deviceIcons += `<i class="fas fa-globe text-secondary" title="기타 브라우저"></i>`;

            // 마지막 활동 포맷
            const dateStr = s.last_seen ? formatDateTime(s.last_seen) : "-";

            tbody.innerHTML += `
                <tr style="cursor: pointer;" onclick="showSessionTimeline('${s.ip}')">
                    <td>${ipLabel}</td>
                    <td>
                        <div class="d-flex align-items-center gap-1">
                            ${deviceIcons}
                            <span class="small ms-1">${s.os} / ${s.browser}</span>
                        </div>
                    </td>
                    <td><small>${escapeHtml(s.city)} (${escapeHtml(s.isp)})</small></td>
                    <td class="text-center"><span class="badge bg-light text-dark fw-bold border">${s.visits}회</span></td>
                    <td><small class="text-muted">${dateStr}</small></td>
                </tr>
            `;
        });
    }

    // 세션 검색 필터링
    function filterSessions() {
        const val = document.getElementById("sessionSearchInput").value.toLowerCase();
        const rows = document.getElementById("sessionsTableBody").getElementsByTagName("tr");
        
        for (let i = 0; i < rows.length; i++) {
            const ipCell = rows[i].getElementsByTagName("td")[0];
            if (ipCell) {
                const ipText = ipCell.textContent || ipCell.innerText;
                if (ipText.toLowerCase().indexOf(val) > -1) {
                    rows[i].style.display = "";
                } else {
                    rows[i].style.display = "none";
                }
            }
        }
    }

    // 특정 세션 타임라인 상세 표시
    function showSessionTimeline(ip) {
        const container = document.getElementById("timelineContainer");
        const s = sessionList.find(x => x.ip === ip);
        if (!s) return;

        let timelineHtml = `
            <div class="mb-4 pb-3 border-bottom">
                <div class="d-flex justify-content-between align-items-start mb-2">
                    <h5 class="m-0 fw-bold">${escapeHtml(s.ip)}</h5>
                    ${s.is_owner ? `<span class="badge bg-warning text-dark">OWNER</span>` : `<span class="badge bg-secondary">USER</span>`}
                </div>
                <div class="small text-muted mb-1"><i class="fas fa-laptop me-1"></i>${s.os} / ${s.browser}</div>
                <div class="small text-muted mb-1"><i class="fas fa-map-marker-alt me-1"></i>${escapeHtml(s.city)} - ${escapeHtml(s.isp)}</div>
                <div class="small text-muted"><i class="fas fa-history me-1"></i>마지막 행동: ${formatDateTime(s.last_seen)}</div>
            </div>
            <h6 class="fw-bold mb-3"><i class="fas fa-list-ol me-2"></i>조회/검색 이동 히스토리</h6>
            <div class="timeline-path">
        `;

        s.path.forEach((path, index) => {
            let nodeClass = "";
            if (index === 0) nodeClass = "start";
            else if (index === s.path.length - 1) nodeClass = "end";

            let pathLabel = path;
            let iconHtml = '<i class="fas fa-search text-muted me-2"></i>';
            
            if (path === '/') {
                pathLabel = "홈페이지 방문";
                iconHtml = '<i class="fas fa-home text-success me-2"></i>';
            } else if (path === '/search/by-image') {
                pathLabel = "이미지 매칭 검색 수행";
                iconHtml = '<i class="fas fa-image text-danger me-2"></i>';
            } else if (path.startsWith('/search?q=')) {
                const q = decodeURIComponent(path.substring(10));
                pathLabel = `검색어 입력: <strong>"${escapeHtml(q)}"</strong>`;
                iconHtml = '<i class="fas fa-keyboard text-primary me-2"></i>';
            }

            timelineHtml += `
                <div class="mb-3 position-relative">
                    <span class="timeline-item-node ${nodeClass}"></span>
                    <div class="d-flex align-items-center mb-1">
                        ${iconHtml}
                        <span class="small fw-semibold text-dark">${pathLabel}</span>
                    </div>
                    <code class="text-muted d-block ms-4" style="font-size: 0.75rem;">${escapeHtml(path)}</code>
                </div>
            `;
        });

        timelineHtml += '</div>';
        container.innerHTML = timelineHtml;
    }

    // 3. 실시간 검색 피드 로드
    async function loadRealtimeLogs() {
        try {
            const res = await fetch("/api/admin/visitors/realtime").then(r => r.json());
            if (!res.success) return;

            const tbody = document.getElementById("realtimeTableBody");
            tbody.innerHTML = "";

            if (res.realtime_logs.length === 0) {
                tbody.innerHTML = `<tr><td colspan="6" class="text-center py-4 text-muted">최근 24시간 내 유입된 실시간 검색 정보가 없습니다.</td></tr>`;
                return;
            }

            res.realtime_logs.forEach(log => {
                // 시각 포맷 (날짜 및 시간 전체 출력)
                const dateStr = log.create_dt ? formatDateTime(log.create_dt) : "-";

                // OWNER 및 IP 라벨링
                let ipLabel = `<strong>${escapeHtml(log.ip)}</strong>`;
                let rowBg = "";
                if (log.is_owner) {
                    ipLabel += ` <span class="badge bg-warning text-dark ms-1" style="font-size:0.65rem;">OWNER</span>`;
                    rowBg = `style="background-color: rgba(255, 193, 7, 0.03);"`;
                }

                // 기기 아이콘 매핑
                let osIcon = `<i class="fas fa-laptop text-secondary me-1"></i>`;
                if (log.os === "Windows") osIcon = `<i class="fab fa-windows text-primary me-1"></i>`;
                else if (log.os === "macOS") osIcon = `<i class="fab fa-apple text-dark me-1"></i>`;
                else if (log.os === "Android") osIcon = `<i class="fab fa-android text-success me-1"></i>`;
                else if (log.os === "iOS") osIcon = `<i class="fas fa-mobile-alt text-dark me-1"></i>`;

                // 검색 정보
                let searchInfo = "";
                if (log.input_text) {
                    searchInfo = `<span class="badge bg-light text-primary border me-1"><i class="fas fa-font"></i> 텍스트</span> <strong class="text-dark">"${escapeHtml(log.input_text)}"</strong>`;
                } else if (log.thumbnail_url) {
                    searchInfo = `
                        <div class="d-flex align-items-center gap-2">
                            <span class="badge bg-light text-danger border"><i class="fas fa-image"></i> 이미지</span>
                            <img src="${log.thumbnail_url}" alt="검색 이미지" class="rounded border" style="width: 32px; height: 32px; object-fit: cover;" onerror="this.src='https://placehold.co/32?text=Img'">
                        </div>
                    `;
                } else {
                    searchInfo = `<span class="text-muted">메인 방문</span>`;
                }

                tbody.innerHTML += `
                    <tr ${rowBg}>
                        <td><small class="text-muted"><i class="far fa-clock me-1"></i>${dateStr}</small></td>
                        <td>${ipLabel}</td>
                        <td>
                            <small class="d-flex align-items-center">
                                ${osIcon} ${log.os} / ${log.browser}
                            </small>
                        </td>
                        <td>${searchInfo}</td>
                        <td><small>${escapeHtml(log.city)} (${escapeHtml(log.isp)})</small></td>
                        <td><small class="text-muted">${escapeHtml(log.memo || "-")}</small></td>
                    </tr>
                `;
            });

        } catch (err) {
            console.error("실시간 피드 로드 오류:", err);
        }
    }

    // 실시간 폴링 관리
    function startRealtimePolling() {
        // 기존 타이머 제거
        if (realtimeTimer) clearInterval(realtimeTimer);
        
        // 30초 폴링 타이머 등록
        realtimeTimer = setInterval(function() {
            const chk = document.getElementById("realtimeAutoRefresh");
            if (chk && chk.checked) {
                loadRealtimeLogs();
            }
        }, 30000);
    }

    function toggleRealtimeAutoRefresh() {
        const isChecked = document.getElementById("realtimeAutoRefresh").checked;
        if (isChecked) {
            startRealtimePolling();
            loadRealtimeLogs();
        } else {
            if (realtimeTimer) clearInterval(realtimeTimer);
        }
    }

    // 4. 지도 및 지리 랭킹 통계 표시
    function renderGeoStats() {
        if (!cachedGeoData) return;

        const geo = cachedGeoData[currentSegment];
        if (!geo) return;

        // 1. 도시 순위 리스트 업데이트
        const citiesList = document.getElementById("geoCitiesList");
        citiesList.innerHTML = "";
        if (!geo.cities || geo.cities.length === 0) {
            citiesList.innerHTML = `<li class="list-group-item text-center py-3 text-muted">지정 기간 내 도시 통계가 없습니다.</li>`;
        } else {
            geo.cities.forEach((c, idx) => {
                citiesList.innerHTML += `
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        <div>
                            <span class="badge bg-secondary me-2">${idx + 1}</span>
                            <span class="fw-semibold">${escapeHtml(c.name)}</span>
                        </div>
                        <span class="badge bg-primary rounded-pill">${c.count}회</span>
                    </li>
                `;
            });
        }

        // 2. ISP 순위 리스트 업데이트
        const ispsList = document.getElementById("geoIspsList");
        ispsList.innerHTML = "";
        if (!geo.isps || geo.isps.length === 0) {
            ispsList.innerHTML = `<li class="list-group-item text-center py-3 text-muted">지정 기간 내 ISP 통계가 없습니다.</li>`;
        } else {
            geo.isps.forEach((isp, idx) => {
                ispsList.innerHTML += `
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        <div>
                            <span class="badge bg-secondary me-2">${idx + 1}</span>
                            <span class="fw-semibold">${escapeHtml(isp.name)}</span>
                        </div>
                        <span class="badge bg-success rounded-pill">${isp.count}회</span>
                    </li>
                `;
            });
        }

        // 3. Leaflet.js 지도 마커 레이어 리셋 및 추가
        if (markersLayer) {
            markersLayer.clearLayers();
        }

        if (geo.pins && geo.pins.length > 0) {
            geo.pins.forEach(pin => {
                if (!pin.lat || !pin.lng) return;

                // 마커의 크기와 투명도를 검색 횟수에 가중치로 다르게 렌더링
                const baseRadius = 8;
                const dynamicRadius = baseRadius + Math.min(pin.count * 1.5, 30);
                
                // 마커 스타일 정의
                let isOwnerIp = pin.is_owner || (pin.isp === "Local Loopback");
                let color = isOwnerIp ? "#dc3545" : "#0d6efd";  // OWNER는 빨강, 유저는 파랑

                const marker = L.circleMarker([pin.lat, pin.lng], {
                    radius: dynamicRadius,
                    fillColor: color,
                    color: '#ffffff',
                    weight: 2,
                    opacity: 0.9,
                    fillOpacity: 0.6
                });

                // 마커 팝업 바인딩
                const popupContent = `
                    <div style="font-family: inherit; font-size: 0.85rem;">
                        <strong>${isOwnerIp ? '<span style="color:#dc3545;">[OWNER 세션]</span>' : '<span style="color:#0d6efd;">[USER 세션]</span>'}</strong><br>
                        <strong>IP:</strong> ${escapeHtml(pin.ip)}<br>
                        <strong>지역:</strong> ${escapeHtml(pin.city)}<br>
                        <strong>ISP:</strong> ${escapeHtml(pin.isp)}<br>
                        <strong>검색량:</strong> <span style="font-weight:bold;color:#0d6efd;">${pin.count}회</span>
                    </div>
                `;
                marker.bindPopup(popupContent);
                markersLayer.addLayer(marker);
            });
        }
    }

    // ──────────────────────────────────────────────────────────
    // 유틸리티 함수
    // ──────────────────────────────────────────────────────────
    function escapeHtml(text) {
        if (!text) return "";
        return text
            .toString()
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatDateTime(isoString) {
        if (!isoString) return "-";
        try {
            const date = new Date(isoString);
            const y = date.getFullYear();
            const m = String(date.getMonth() + 1).padStart(2, '0');
            const d = String(date.getDate()).padStart(2, '0');
            const hh = String(date.getHours()).padStart(2, '0');
            const mm = String(date.getMinutes()).padStart(2, '0');
            const ss = String(date.getSeconds()).padStart(2, '0');
            return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
        } catch (e) {
            return isoString;
        }
    }

    // ──────────────────────────────────────────────────────────
    // 관리자(OWNER) IP 관리 기능 추가
    // ──────────────────────────────────────────────────────────
    let currentClientIp = "Unknown";
    let ownerIpModal = null;

    // 모달 생성 및 열기
    function openOwnerIpModal() {
        if (!ownerIpModal) {
            ownerIpModal = new bootstrap.Modal(document.getElementById('ownerIpModal'));
        }
        document.getElementById('currentMyIpDisplay').innerText = currentClientIp;
        loadOwnerIps();
        ownerIpModal.show();
    }

    // 등록된 OWNER IP 리스트 조회
    async function loadOwnerIps() {
        try {
            const res = await fetch("/api/admin/visitors/owner-ips").then(r => r.json());
            const tbody = document.getElementById("ownerIpsTableBody");
            tbody.innerHTML = "";

            if (!res.success || !res.owner_ips || res.owner_ips.length === 0) {
                tbody.innerHTML = `<tr><td colspan="4" class="text-center py-3 text-muted">등록된 관리자 IP가 없습니다.</td></tr>`;
                return;
            }

            res.owner_ips.forEach(item => {
                const dateStr = item.create_dt ? formatDateTime(item.create_dt) : "-";
                tbody.innerHTML += `
                    <tr>
                        <td class="font-monospace fw-bold">${escapeHtml(item.ip_address)}</td>
                        <td><span class="badge bg-light text-dark border">${escapeHtml(item.memo || "-")}</span></td>
                        <td><small class="text-muted">${dateStr}</small></td>
                        <td class="text-center">
                            <button class="btn btn-outline-danger btn-xs py-0 px-2" onclick="deleteOwnerIp('${item.ip_address}')" style="font-size:0.75rem;">
                                <i class="fas fa-trash-alt"></i> 삭제
                            </button>
                        </td>
                    </tr>
                `;
            });
        } catch (err) {
            console.error("관리자 IP 목록 로드 실패:", err);
        }
    }

    // 현재 내 IP 등록
    async function registerCurrentIp() {
        if (currentClientIp === "Unknown") {
            alert("현재 접속 IP를 감지하지 못했습니다.");
            return;
        }
        await sendRegisterIp(currentClientIp, currentClientIp === "127.0.0.1" || currentClientIp === "::1" || currentClientIp === "localhost" ? "Localhost (Auto)" : "KT-iptime (Auto)");
    }

    // 수동 IP 등록
    async function registerManualIp() {
        const ip = document.getElementById('manualIpInput').value.trim();
        const memo = document.getElementById('manualMemoInput').value.trim();
        if (!ip) {
            alert("IP 주소를 입력해주세요.");
            return;
        }
        await sendRegisterIp(ip, memo || "수동 등록");
        document.getElementById('manualIpInput').value = "";
        document.getElementById('manualMemoInput').value = "";
    }

    // IP 등록 API 전송 공통
    async function sendRegisterIp(ipAddress, memo) {
        try {
            const res = await fetch("/api/admin/visitors/owner-ip", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ip_address: ipAddress, memo: memo })
            }).then(r => r.json());

            if (res.success) {
                alert(res.message);
                loadOwnerIps();
                loadAllData(); // 대시보드 통계 새로고침
            } else {
                alert("등록 실패: " + res.detail);
            }
        } catch (err) {
            console.error("IP 등록 통신 에러:", err);
        }
    }

    // 등록된 IP 삭제
    async function deleteOwnerIp(ipAddress) {
        if (!confirm(`관리자 IP(${ipAddress})를 목록에서 삭제하시겠습니까?`)) return;
        try {
            const res = await fetch(`/api/admin/visitors/owner-ip/${encodeURIComponent(ipAddress)}`, {
                method: "DELETE"
            }).then(r => r.json());

            if (res.success) {
                alert(res.message);
                loadOwnerIps();
                loadAllData(); // 대시보드 통계 새로고침
            } else {
                alert("삭제 실패: " + res.detail);
            }
        } catch (err) {
            console.error("IP 삭제 통신 에러:", err);
        }
    }
