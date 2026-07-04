let pendingSwapBrand = null;
let currentStagingCounts = {};
let currentRunPage = 1;
let currentErrPage = 1;
let currentAutoRunPage = 1;
let currentAutoErrPage = 1;
const pageSize = 10;

let activeDashboardMode = 'auto'; // 'manual' or 'auto' (기본 자동 크롤링 모니터링 활성화)
let autoErrorsCache = {}; // 에러 정보 상세 모달 맵
let selectedRunId = null; // 특정 파이프라인 구동 건 필터링 ID
let selectedAutoRunId = null; // 특정 자동 파이프라인 구동 건 필터링 ID
let cachedManualErrors = []; // 프론트엔드 실시간 필터링 캐시 저장소
let fetchController = null; // 경합 방지용 비동기 요청 취소 컨트롤러
let isFetchingManual = false; // 현재 수동 로그가 fetch 중인지 추적하는 플래그
let cachedProgressState = {}; // 각 브랜드별 실시간 진행 상태 캐시

// 아코디언 상태 유지용 전역 Set
const openedBrands = new Set();

// 아코디언 토글 함수
function toggleDetails(brand) {
  const el = document.getElementById(`details-${brand}`);
  if (!el) return;
  if (el.style.display === 'none') {
    el.style.display = 'block';
    openedBrands.add(brand);
  } else {
    el.style.display = 'none';
    openedBrands.delete(brand);
  }
}


document.addEventListener('DOMContentLoaded', () => {
  // Bootstrap 탭 전환 시 shown.bs.tab 이벤트를 감지하여 데이터 로딩 및 모드 스위칭
  const autoTabBtn = document.getElementById('auto-tab');
  const manualTabBtn = document.getElementById('manual-tab');
  const errorTabBtn = document.getElementById('error-tab');
  
  if (autoTabBtn) {
    autoTabBtn.addEventListener('shown.bs.tab', () => {
      switchDashboardMode('auto');
    });
  }
  if (manualTabBtn) {
    manualTabBtn.addEventListener('shown.bs.tab', () => {
      switchDashboardMode('manual');
    });
  }
  if (errorTabBtn) {
    errorTabBtn.addEventListener('shown.bs.tab', () => {
      switchDashboardMode('error');
    });
  }

  const savedMode = localStorage.getItem('activeCrawlingTab') || 'auto';
  activeDashboardMode = savedMode;
  
  if (savedMode === 'manual') {
    if (manualTabBtn) {
      const tab = new bootstrap.Tab(manualTabBtn);
      tab.show();
    }
    loadManualData();
  } else if (savedMode === 'error') {
    if (errorTabBtn) {
      const tab = new bootstrap.Tab(errorTabBtn);
      tab.show();
    }
    loadErrorProducts();
  } else {
    if (autoTabBtn) {
      const tab = new bootstrap.Tab(autoTabBtn);
      tab.show();
    }
    loadAutoData();
  }

  // 5초 주기로 활성화 탭 데이터 자동 새로고침 (에러 탭은 운영자 통제 집중을 위해 자동 주기 갱신 제외)
  setInterval(() => {
    if (activeDashboardMode === 'manual') {
      loadManualData(true);
    } else if (activeDashboardMode === 'auto') {
      loadAutoData();
    }
  }, 5000);
});

function switchDashboardMode(mode) {
  if (activeDashboardMode === mode) return; // 중복 호출 방지
  activeDashboardMode = mode;
  localStorage.setItem('activeCrawlingTab', mode);
  if (mode === 'manual') {
    loadManualData();
  } else if (mode === 'error') {
    loadErrorProducts();
  } else {
    loadAutoData();
  }
}

function loadManualData(isSilent = false) {
  fetchStagingStatus();
  fetchManualLogs(isSilent);
}

// 브랜드별 실시간 크롤링/파이프라인 진행 상황 체크 API 연동
async function checkBrandProgress(brand) {
  const container = document.getElementById(`progress-container-${brand}`);
  const stepEl = document.getElementById(`progress-step-${brand}`);
  const percentEl = document.getElementById(`progress-percent-${brand}`);
  const barEl = document.getElementById(`progress-bar-${brand}`);
  const itemEl = document.getElementById(`progress-item-${brand}`);
  const timeEl = document.getElementById(`progress-time-${brand}`);
  const countEl = document.getElementById(`progress-count-${brand}`);

  if (!container) return;

  try {
    const res = await fetch(`/api/admin/crawling/progress?brand=${brand}&t=${Date.now()}`);
    const data = await res.json();
    
    if (data.success && data.found && (data.status === 'running' || data.status === 'done')) {
      const isRunning = data.status === 'running';
      const step = isRunning ? (data.step || '수집 진행 중') : '수집 완료';
      const percent = `${data.percent}%`;
      const item = isRunning
        ? (data.current_item ? `처리 중: ${data.current_item}` : '대기 중...')
        : (data.current_item ? `완료: ${data.current_item}` : '완료');
      
      let time = '경과 시간: 0초';
      const elapsed = data.elapsed_sec || 0;
      if (elapsed > 60) {
        const min = Math.floor(elapsed / 60);
        const sec = elapsed % 60;
        time = `경과 시간: ${min}분 ${sec}초`;
      } else {
        time = `경과 시간: ${elapsed}초`;
      }
      const count = `${data.current} / ${data.total}`;
      
      // 캐시 업데이트
      cachedProgressState[brand] = {
        step, percent, width: `${data.percent}%`, item, time, count, isRunning: isRunning
      };
      
      stepEl.textContent = step;
      percentEl.textContent = percent;
      barEl.style.width = `${data.percent}%`;
      if (isRunning) {
        barEl.classList.add('progress-bar-animated', 'progress-bar-striped');
      } else {
        barEl.classList.remove('progress-bar-animated', 'progress-bar-striped');
      }
      itemEl.textContent = item;
      timeEl.textContent = time;
      countEl.textContent = count;
    } else {
      // 캐시 업데이트 (Idle 상태)
      cachedProgressState[brand] = {
        step: '대기 중 (Idle)', percent: '0%', width: '0%', item: '준비 완료', time: '경과 시간: 0초', count: '0 / 0', isRunning: false
      };
      
      stepEl.textContent = '대기 중 (Idle)';
      percentEl.textContent = '0%';
      barEl.style.width = '0%';
      barEl.classList.remove('progress-bar-animated', 'progress-bar-striped');
      itemEl.textContent = '준비 완료';
      timeEl.textContent = '경과 시간: 0초';
      countEl.textContent = '0 / 0';
    }
  } catch (err) {
    console.error(`[${brand}] progress check error:`, err);
  }
}

// ──────────────────────────────────────
// Tab 1: 수동 스테이징 상태 및 로그 조회
// ──────────────────────────────────────
async function fetchStagingStatus() {
  const grid = document.getElementById('brandStatusGrid');
  try {
    const res = await fetch(`/api/admin/crawling/staging?t=${Date.now()}`);
    const data = await res.json();
    if (!data.success) {
      grid.innerHTML = `<div class="col-12 text-center text-danger py-4">현황 조회 실패: ${data.detail}</div>`;
      return;
    }

    // 상단 요약 카드 데이터 업데이트
    document.getElementById('totalStagingCount').textContent = data.total_staging.toLocaleString();
    
    let totalEmbed = 0;
    let totalNaver = 0;
    
    let html = '';
    data.brands.forEach(b => {
      // 데이터 없는 브랜드 기본 숨김 처리 (상품이 하나도 수집되거나 운영 등록되지 않은 경우)
      const isEmpty = b.staging_count === 0 && b.prod_count === 0;
      if (window.hideEmptyBrands && isEmpty) return;

      totalEmbed += b.embed_count;
      totalNaver += b.naver_count;
      
      currentStagingCounts[b.brand] = {
        staging: b.staging_count,
        prod: b.prod_count,
        embed: b.embed_count,
        naver: b.naver_count,
        img: b.img_count,
        status: b.integrity_status
      };

      const statusBadge = getStatusBadge(b.integrity_status);
      const isStagingEmpty = b.staging_count === 0;
      const isReadyOrWaiting = b.integrity_status === 'ready' || b.integrity_status === 'waiting' || b.integrity_status === 'img_missing';

      const latestDtStr = b.latest_dt ? new Date(b.latest_dt).toLocaleString('ko-KR') : '구동 이력 없음';
      const hoursLeft = 24 - b.hours_elapsed;
      const waitingText = (b.integrity_status === 'waiting' && hoursLeft > 0) 
        ? `<div class="text-warning small mt-1"><i class="far fa-clock me-1"></i>배치 이관 대기: ${hoursLeft}시간 남음</div>`
        : '';

      const pCache = cachedProgressState[b.brand] || {
        step: '대기 중 (Idle)', percent: '0%', width: '0%', item: '준비 완료', time: '경과 시간: 0초', count: '0 / 0', isRunning: false
      };
      const animClasses = pCache.isRunning ? 'progress-bar-striped progress-bar-animated' : '';
      const actualWaitingBadge = pCache.isRunning 
        ? `<span class="text-primary text-end fw-semibold" style="font-size:.7rem;"><i class="fas fa-spinner fa-spin me-1"></i>작업 진행 중</span>`
        : (waitingText ? `<span class="text-warning text-end fw-semibold" style="font-size:.7rem;"><i class="far fa-clock me-1"></i>대기: ${hoursLeft}h</span>` : `<span class="text-muted" style="font-size:.7rem;">이관 대기 없음</span>`);
      const isDetailsVisible = openedBrands.has(b.brand) ? 'block' : 'none';

      const crawlBtnHtml = pCache.isRunning
        ? `<button class="btn btn-sm btn-danger flex-fill" onclick="stopCrawl('${b.brand}')">
             <i class="fas fa-stop me-1"></i>중지
           </button>`
        : `<button class="btn btn-sm btn-primary flex-fill" onclick="openCrawlModal('${b.brand}')">
             <i class="fas fa-play me-1"></i>크롤링 실행
           </button>`;

      const isSwapDisabled = (pCache.isRunning || !isReadyOrWaiting) ? 'disabled' : '';
      const isClearDisabled = (pCache.isRunning || isStagingEmpty) ? 'disabled' : '';

      // 수집 진행도(%) 계산
      const targetCount = b.staging_count === 0 ? 0 : (b.target_count || b.staging_count || 1);
      const progressPercent = targetCount === 0 ? 0 : Math.min(100, Math.round((b.staging_count / targetCount) * 100));

      // 카테고리별 테이블 HTML 조립
      let catRowsHtml = '';
      if (b.categories && b.categories.length > 0) {
        b.categories.forEach(cat => {
          // 정합성 불일치 체크 (수집수 != 임베딩수)
          const mismatchWarning = cat.staging_count !== cat.embed_count 
            ? `<i class="fas fa-exclamation-triangle text-warning ms-1" title="상품수(${cat.staging_count})와 임베딩수(${cat.embed_count}) 불일치!"></i>` 
            : '';
          
          const targetStr = cat.target_count ? ` / ${cat.target_count}` : '';
          
          catRowsHtml += `
            <tr style="font-size:0.72rem;">
              <td class="fw-bold text-secondary">${cat.category}</td>
              <td class="text-end">${cat.staging_count}${targetStr} ${mismatchWarning}</td>
              <td class="text-end text-muted">${cat.embed_count}</td>
              <td class="text-end text-muted">${cat.img_count}</td>
              <td class="text-end text-muted">${cat.naver_count}</td>
            </tr>`;
        });
      } else {
        catRowsHtml = `<tr><td colspan="5" class="text-center py-2 text-muted" style="font-size:0.7rem;">집계된 카테고리 데이터 없음</td></tr>`;
      }

      html += `
      <div class="col-12 col-md-6 col-xl-4">
        <div class="brand-card shadow-sm border-0 mb-3">
          <div class="card-header px-3 py-2.5 d-flex justify-content-between align-items-center bg-dark text-white">
            <span class="fw-bold" style="letter-spacing:0.5px;"><i class="fas fa-store me-2 text-warning"></i>${b.brand}</span>
            ${statusBadge}
          </div>
          <div class="card-body px-3 py-3">
            <!-- 목표 대비 진행도 게이지바 -->
            <div class="mb-3 p-2 bg-light rounded-3">
              <div class="d-flex justify-content-between align-items-center mb-1" style="font-size:0.75rem;">
                <span class="text-muted fw-semibold">목표 수집률 (${b.staging_count}/${targetCount}개)</span>
                <span class="fw-bold text-primary">${progressPercent}%</span>
              </div>
              <div class="progress" style="height: 8px; background-color: #e9ecef;">
                <div class="progress-bar bg-gradient-primary" role="progressbar" style="width: ${progressPercent}%; background: linear-gradient(90deg, #3b82f6 0%, #1d4ed8 100%);"></div>
              </div>
            </div>

            <div class="row g-2 mb-3">
              <!-- 상품 수 -->
              <div class="col-6 border-end">
                <div class="metric-label text-primary"><i class="fas fa-hourglass-start me-1"></i>대기 상품 수</div>
                <div class="metric-value text-primary">${b.staging_count.toLocaleString()}</div>
              </div>
              <div class="col-6 ps-2">
                <div class="metric-label text-success"><i class="fas fa-check-circle me-1"></i>운영 상품 수</div>
                <div class="metric-value text-success">${b.prod_count.toLocaleString()}</div>
              </div>
              
              <!-- 임베딩 수 -->
              <div class="col-6 border-end">
                <div class="metric-label"><i class="fas fa-calculator me-1"></i>대기 임베딩</div>
                <div class="fw-semibold text-secondary" style="font-size:0.9rem; ${getMetricStyle(b.embed_count, b.staging_count)}">${b.embed_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
              <div class="col-6 ps-2">
                <div class="metric-label"><i class="fas fa-brain me-1 text-success"></i>운영 임베딩</div>
                <div class="fw-bold text-success" style="font-size:0.95rem; ${getMetricStyle(b.prod_embed_count, b.prod_count)}">${b.prod_embed_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
              
              <!-- 최저가 가격 수 -->
              <div class="col-6 border-end">
                <div class="metric-label"><i class="fas fa-tag me-1"></i>대기 최저가</div>
                <div class="fw-semibold text-secondary" style="font-size:0.9rem; ${getMetricStyle(b.naver_count, b.staging_count * 5)}">${b.naver_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
              <div class="col-6 ps-2">
                <div class="metric-label"><i class="fas fa-tags me-1 text-success"></i>운영 최저가</div>
                <div class="fw-bold text-success" style="font-size:0.95rem; ${getMetricStyle(b.prod_naver_count, b.prod_count * 5)}">${b.prod_naver_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>

              <!-- 이미지 수 -->
              <div class="col-6 border-end">
                <div class="metric-label"><i class="fas fa-image me-1"></i>대기 이미지</div>
                <div class="fw-semibold text-secondary" style="font-size:0.9rem; ${getMetricStyle(b.img_count, b.staging_count)}">${b.img_count.toLocaleString()} / ${b.staging_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
              <div class="col-6 ps-2">
                <div class="metric-label"><i class="fas fa-images me-1 text-success"></i>운영 이미지</div>
                <div class="fw-bold text-success" style="font-size:0.95rem; ${getMetricStyle(b.prod_img_count, b.prod_count)}">${b.prod_img_count.toLocaleString()} / ${b.prod_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
            </div>

            <!-- 실시간 진행률 + 아코디언 세부 지표 + 버튼 -->
            <div id="progress-container-${b.brand}" class="mt-3 p-2 border rounded bg-light" style="font-size: 0.73rem;">
              <div class="d-flex justify-content-between align-items-center mb-1">
                <span class="fw-bold text-secondary" id="progress-step-${b.brand}">${pCache.step}</span>
                <span class="fw-bold text-secondary" id="progress-percent-${b.brand}">${pCache.percent}</span>
              </div>
              <div class="progress mb-1" style="height: 6px;">
                <div class="progress-bar ${animClasses} bg-primary" id="progress-bar-${b.brand}" role="progressbar" style="width: ${pCache.width}"></div>
              </div>
              <div class="text-truncate text-muted mb-1" id="progress-item-${b.brand}" style="font-size: 0.68rem; max-width: 100%;">${pCache.item}</div>
              <div class="d-flex justify-content-between text-muted" style="font-size: 0.63rem;">
                <span id="progress-time-${b.brand}">${pCache.time}</span>
                <span id="progress-count-${b.brand}">${pCache.count}</span>
              </div>
            </div>

            <div class="pt-2 mt-2 border-top">
              <div class="d-flex justify-content-between align-items-center mb-2" style="font-size:.73rem;">
                <span class="text-muted text-truncate" style="max-width: 60%;" title="최근 파이프라인: ${latestDtStr}">
                  <i class="fas fa-history me-1"></i>${latestDtStr.split('오')[1] ? '오' + latestDtStr.split('오')[1] : latestDtStr}
                  ${b.pipeline_error_count > 0 ? `<span class="badge bg-danger ms-1">에러 ${b.pipeline_error_count}</span>` : ''}
                </span>
                ${actualWaitingBadge}
              </div>
            </div>

            <!-- 총 소요 시간 표시 -->
            ${b.last_duration_sec !== undefined && b.last_duration_sec !== null ? 
            '<div class="d-flex justify-content-between align-items-center mb-2 p-2 bg-light rounded text-secondary" style="font-size: 0.73rem;">' +
              '<span class="fw-semibold text-muted"><i class="fas fa-stopwatch me-1 text-primary"></i>총 소요 시간</span>' +
              '<span class="fw-bold text-dark">' + (b.last_duration_sec >= 60 ? Math.floor(b.last_duration_sec / 60) + '분 ' + (b.last_duration_sec % 60) + '초' : b.last_duration_sec + '초') + '</span>' +
            '</div>' : ''}

            <!-- 카테고리별 세부 지표 (아코디언 - 상태 보존형) -->
            <div class="mb-2">
              <button class="btn btn-xs btn-outline-secondary w-100 mb-2 py-1" onclick="toggleDetails('${b.brand}')" style="font-size: 0.7rem;">
                <i class="fas fa-list-alt me-1"></i>카테고리별 세부 지표 보기/접기
              </button>
              <div id="details-${b.brand}" class="p-2 border rounded bg-white" style="display: ${isDetailsVisible};">
                <div class="table-responsive">
                  <table class="table table-sm table-borderless mb-0 align-middle">
                    <thead>
                      <tr class="text-muted border-bottom" style="font-size:0.68rem;">
                        <th>카테고리</th>
                        <th class="text-end">수집/목표</th>
                        <th class="text-end">벡터</th>
                        <th class="text-end">이미지</th>
                        <th class="text-end">최저가</th>
                      </tr>
                    </thead>
                    <tbody>${catRowsHtml}</tbody>
                  </table>
                </div>
              </div>
            </div>

            <div class="d-flex gap-2">
              ${crawlBtnHtml}
              <button class="btn btn-sm btn-success flex-fill" ${isSwapDisabled} onclick="openSwapModal('${b.brand}')">
                <i class="fas fa-exchange-alt me-1"></i>스위칭(Swap)
              </button>
              <button class="btn btn-sm btn-outline-danger" title="스테이징 비우기" ${isClearDisabled} onclick="clearStaging('${b.brand}')">
                <i class="fas fa-trash-alt"></i>
              </button>
            </div>
          </div>
        </div>
      </div>`;
    });

    if (html === '') {
      html = `<div class="col-12 text-center text-muted py-5"><i class="fas fa-info-circle me-1"></i>현재 표시할 수집 활성 데이터가 존재하지 않습니다. 상단의 '전체 브랜드 보기'를 클릭해 주세요.</div>`;
    }
    grid.innerHTML = html;
    document.getElementById('totalEmbeddingCount').textContent = totalEmbed.toLocaleString();
    document.getElementById('totalNaverCount').textContent = totalNaver.toLocaleString();
    document.getElementById('lastRefreshTimeManual').textContent = `마지막 새로고침: ${new Date().toLocaleTimeString()}`;
    
    // 브랜드별 실시간 진행 상황 조회 구동
    data.brands.forEach(b => {
      checkBrandProgress(b.brand);
    });
  } catch (err) {
    grid.innerHTML = `<div class="col-12 text-center text-danger py-4">서버 오류: ${err.message}</div>`;
  }
}

async function fetchManualLogs(isSilent = false) {
  if (isFetchingManual && isSilent) {
    // 이미 백그라운드 요청이 진행 중이면 추가적인 silent 갱신 요청은 경합 방지를 위해 스킵
    return;
  }

  if (!isSilent) {
    if (fetchController) {
      fetchController.abort();
    }
    fetchController = new AbortController();
  }
  
  const signal = fetchController ? fetchController.signal : null;
  isFetchingManual = true;

  try {
    // 최신 수동 에러를 100건 한 번에 가져와서 로컬 메모리 캐시에 탑재
    let url = `/api/admin/crawling/logs?run_page=${currentRunPage}&run_limit=${pageSize}&err_page=1&err_limit=100&t=${Date.now()}`;
    if (selectedRunId) {
      url += `&run_id=${selectedRunId}`;
    }
    const res = await fetch(url, signal ? { signal } : {});
    const data = await res.json();
    isFetchingManual = false;
    if (!data.success) return;

    // 1. 구동 내역 테이블 바인딩
    const runsBody = document.getElementById('runsTableBody');
    if (data.runs && data.runs.length > 0) {
      let runsHtml = '';
      data.runs.forEach(r => {
        let statusBadge = `<span class="badge bg-secondary">${r.status}</span>`;
        if (r.status === 'SUCCESS' || r.status === 'completed') {
          if (r.error_count > 0) {
            statusBadge = `<span class="badge bg-warning text-dark">일부성공</span>`;
          } else {
            statusBadge = `<span class="badge bg-success">성공</span>`;
          }
        } else if (r.status === 'FAILED' || r.status === 'failed') {
          statusBadge = `<span class="badge bg-danger">실패</span>`;
        } else if (r.status === 'RUNNING' || r.status === 'running') {
          statusBadge = `<span class="badge bg-primary spinner-grow-sm">실행 중</span>`;
        }

        const dateStr = r.finished_at ? new Date(r.finished_at).toLocaleString() : new Date(r.started_at).toLocaleString();
        
        let actionBadge = '';
        if (r.pipeline_name && r.pipeline_name.includes('swap')) {
          actionBadge = `<span class="badge bg-secondary text-white me-1" style="font-size:0.68rem; padding: 2px 4px; font-weight: 500;">이관</span>`;
        } else {
          actionBadge = `<span class="badge bg-light text-dark border me-1" style="font-size:0.68rem; padding: 2px 4px; font-weight: 500;">수집</span>`;
        }
        
        const isSelected = selectedRunId === r.run_id;
        const rowStyle = isSelected ? 'background-color: #f0fdf4; border-left: 4px solid #16a34a;' : 'cursor: pointer;';

        runsHtml += `
          <tr style="${rowStyle}" onclick="selectRun(${r.run_id})">
            <td>
              <div class="d-flex align-items-center">
                ${actionBadge}
                <strong>[#${r.display_run_id}] ${r.brand || 'ALL'}</strong>
              </div>
            </td>
            <td>${statusBadge}</td>
            <td class="text-end">${r.total_items.toLocaleString()}</td>
            <td class="text-end">${(r.embed_count || 0).toLocaleString()}</td>
            <td class="text-end">${r.new_items}/${r.updated_items}</td>
            <td class="text-end text-danger fw-semibold">${r.error_count.toLocaleString()}</td>
            <td><small class="text-muted">${dateStr}</small></td>
          </tr>`;
      });
      runsBody.innerHTML = runsHtml;
    } else {
      runsBody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">구동 내역이 없습니다.</td></tr>';
    }

    const totalRunPages = Math.max(1, Math.ceil(data.total_runs / pageSize));
    renderPaginationHTML('runsPagination', currentRunPage, totalRunPages, 'goToRunPage');

    // 2. 에러 로그 로컬 캐시 업데이트
    if (data.errors) {
      cachedManualErrors = data.errors;
    }
    
    // 로컬 메모리 엔진을 기동하여 즉시 필터링/페이징 렌더링 (렉 0%)
    renderErrorsEngine();

  } catch (err) {
    isFetchingManual = false;
    if (err.name === 'AbortError') {
      // AbortError인 경우에도 200ms 후에 로딩 락이 풀리도록 Fallback으로 renderErrorsEngine()을 한 번 더 수행
      setTimeout(() => {
        renderErrorsEngine();
      }, 200);
      return;
    }
    console.error("로그 조회 실패:", err);
    // 에러 발생 시에도 화면 락을 풀고 로컬 캐시 백업 렌더링 유지
    renderErrorsEngine();
  }
}

// ──────────────────────────────────────
// Tab 2: 자동 크롤링 대시보드 통계 & 로그
// ──────────────────────────────────────
async function loadAutoData() {
  fetchAutoBrandStatus(); // 브랜드별 현황 카드 로딩 추가
  fetchAutoStats();
  fetchAutoLogs();
}

async function fetchAutoStats() {
  try {
    const res = await fetch('/api/admin/crawling/auto/stats');
    const data = await res.json();
    if (!data.success) return;

    // 에러 유형 통계
    const typeContainer = document.getElementById('errorTypesContainer');
    if (data.top_error_types && data.top_error_types.length > 0) {
      const maxCount = Math.max(...data.top_error_types.map(t => t.count));
      let html = '';
      data.top_error_types.forEach(t => {
        const pct = maxCount > 0 ? Math.round((t.count / maxCount) * 100) : 0;
        
        // 에러 유형 카테고리 한글 매핑
        const catMap = getErrorCategoryAndClass(t.error_type);
        
        html += `
          <div class="stat-bar-item">
            <div class="stat-bar-label text-truncate" title="${t.error_type}">
              <span class="badge ${catMap.badgeClass} me-1" style="font-size:0.65rem;">${catMap.label}</span>${t.error_type}
            </div>
            <div class="stat-bar-fill-wrap">
              <div class="stat-bar-fill bg-danger" style="width: ${pct}%"></div>
            </div>
            <div class="stat-bar-value">${t.count.toLocaleString()}건</div>
          </div>`;
      });
      typeContainer.innerHTML = html;
    } else {
      typeContainer.innerHTML = '<div class="text-center text-muted py-5">에러 유형 기록이 없습니다.</div>';
    }

    // 브랜드별 통계
    const brandContainer = document.getElementById('errorBrandsContainer');
    if (data.error_brands && data.error_brands.length > 0) {
      const maxCount = Math.max(...data.error_brands.map(b => b.count));
      let html = '';
      data.error_brands.forEach(b => {
        const pct = maxCount > 0 ? Math.round((b.count / maxCount) * 100) : 0;
        html += `
          <div class="stat-bar-item">
            <div class="stat-bar-label fw-bold">${b.brand}</div>
            <div class="stat-bar-fill-wrap">
              <div class="stat-bar-fill bg-warning" style="width: ${pct}%"></div>
            </div>
            <div class="stat-bar-value">${b.count.toLocaleString()}건</div>
          </div>`;
      });
      brandContainer.innerHTML = html;
    } else {
      brandContainer.innerHTML = '<div class="text-center text-muted py-5">브랜드별 에러 기록이 없습니다.</div>';
    }

  } catch (err) {
    console.error("자동 통계 로딩 실패:", err);
  }
}

async function fetchAutoLogs() {
  try {
    let url = `/api/admin/crawling/auto/logs?run_page=${currentAutoRunPage}&run_limit=${pageSize}&err_page=${currentAutoErrPage}&err_limit=${pageSize}`;
    if (selectedAutoRunId) {
      url += `&run_id=${selectedAutoRunId}`;
    }
    const res = await fetch(url);
    const data = await res.json();
    if (!data.success) return;

    document.getElementById('totalAutoRunsCount').textContent = data.total_runs.toLocaleString();
    document.getElementById('totalAutoErrorsCount').textContent = data.total_errors.toLocaleString();
    
    // 성공율
    let successCount = 0;
    if (data.runs && data.runs.length > 0) {
      data.runs.forEach(r => {
        if (r.status === 'SUCCESS' || r.status === 'completed') successCount++;
      });
      const rate = data.total_runs > 0 ? Math.round((successCount / data.total_runs) * 100) : 0;
      document.getElementById('autoSuccessRate').textContent = `${rate}%`;
    }

    // 1) 자동 구동 내역 테이블 바인딩
    const autoRunsBody = document.getElementById('autoRunsTableBody');
    if (data.runs && data.runs.length > 0) {
      let runsHtml = '';
      data.runs.forEach(r => {
        let statusBadge = `<span class="badge bg-secondary">${r.status}</span>`;
        if (r.status === 'SUCCESS' || r.status === 'completed') {
          statusBadge = r.error_count > 0 
            ? `<span class="badge bg-warning text-dark">일부성공</span>` 
            : `<span class="badge bg-success">성공</span>`;
        } else if (r.status === 'FAILED' || r.status === 'failed') {
          statusBadge = `<span class="badge bg-danger">실패</span>`;
        } else if (r.status === 'RUNNING' || r.status === 'running') {
          statusBadge = `<span class="badge bg-primary spinner-grow-sm">실행 중</span>`;
        }
        
        let actionBadge = '';
        if (r.pipeline_name && r.pipeline_name.includes('swap')) {
          actionBadge = `<span class="badge bg-secondary text-white me-1" style="font-size:0.68rem; padding: 2px 4px; font-weight: 500;">이관</span>`;
        } else {
          actionBadge = `<span class="badge bg-light text-dark border me-1" style="font-size:0.68rem; padding: 2px 4px; font-weight: 500;">수집</span>`;
        }
        
        const dateStr = r.finished_at ? new Date(r.finished_at).toLocaleString() : new Date(r.started_at).toLocaleString();
        const isSelected = selectedAutoRunId === r.run_id;
        const rowStyle = isSelected ? 'background-color: #f0fdf4; border-left: 4px solid #16a34a;' : 'cursor: pointer;';
        
        runsHtml += `
          <tr style="${rowStyle}" onclick="selectAutoRun(${r.run_id})">
            <td>
              <div class="d-flex align-items-center">
                ${actionBadge}
                <strong>[#${r.display_run_id}] ${r.brand}</strong>
              </div>
            </td>
            <td>${statusBadge}</td>
            <td class="text-end">${r.total_items.toLocaleString()}</td>
            <td class="text-end">${(r.embed_count || 0).toLocaleString()}</td>
            <td class="text-end">${r.new_items}/${r.updated_items}</td>
            <td class="text-end text-danger fw-semibold">${r.error_count.toLocaleString()}</td>
            <td><small class="text-muted">${dateStr}</small></td>
          </tr>`;
      });
      autoRunsBody.innerHTML = runsHtml;
    } else {
      autoRunsBody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-muted">자동 배치 구동 내역이 없습니다.</td></tr>';
    }

    const totalRunPages = Math.max(1, Math.ceil(data.total_runs / pageSize));
    renderPaginationHTML('autoRunsPagination', currentAutoRunPage, totalRunPages, 'goToAutoRunPage');

    // 2) 실시간 자동 에러 내역 바인딩 (피드백 반영: 격리된 컬럼으로 브랜드, 분류, 상품명, 메시지 노출)
    const autoErrorsBody = document.getElementById('autoErrorsTableBody');
    if (data.errors && data.errors.length > 0) {
      let errHtml = '';
      autoErrorsCache = {};
      
      data.errors.forEach(e => {
        autoErrorsCache[e.error_id] = e;
        const timeStr = new Date(e.created_at).toLocaleTimeString();
        const brandStr = e.brand ? e.brand.toUpperCase() : 'UNKNOWN';
        const catMap = getErrorCategoryAndClass(e.error_type);
        const prodNameStr = e.prod_name ? e.prod_name : (e.product_id ? `ID: ${e.product_id}` : '공통오류');
        
        const runIdStr = e.display_run_id ? `[#${e.display_run_id}]` : (e.run_id ? `[#${e.run_id}]` : '[#-]');
        
        const sourceUrlBtn = e.source_url ? `<a href="${e.source_url}" target="_blank" class="btn btn-xs btn-outline-primary" title="원본 페이지 이동" onclick="event.stopPropagation()"><i class="fas fa-external-link-alt"></i></a>` : '';
        const retryBtn = (e.brand && e.product_id) ? `<button class="btn btn-xs btn-outline-warning ms-1" title="단일 재수집" onclick="event.stopPropagation(); retrySingleProduct('${e.brand}', '${e.product_id}')"><i class="fas fa-redo"></i></button>` : '';

        errHtml += `
          <tr onclick="openAutoErrDetailModal(${e.error_id})">
            <td><small class="text-muted">${timeStr} <span class="text-primary fw-bold" style="font-size:.65rem;">${runIdStr}</span></small></td>
            <td><span class="badge bg-secondary" style="font-size:0.7rem;">${brandStr}</span></td>
            <td><span class="badge-category ${catMap.badgeClass}">${catMap.label}</span></td>
            <td>
              <div class="fw-semibold text-truncate" style="max-width: 130px;" title="${prodNameStr}">${prodNameStr}</div>
              <small class="text-muted font-monospace" style="font-size:0.68rem;">ID: ${e.product_id || '누락'}</small>
            </td>
            <td><div class="text-truncate text-danger" style="max-width: 180px;" title="${e.error_message}">${e.error_message}</div></td>
            <td>
              <div class="d-flex align-items-center">
                ${sourceUrlBtn}
                ${retryBtn}
              </div>
            </td>
          </tr>`;
      });
      autoErrorsBody.innerHTML = errHtml;
    } else {
      autoErrorsBody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-muted">최근 에러 내역이 없습니다.</td></tr>';
    }

    const totalErrPages = Math.max(1, Math.ceil(data.total_errors / pageSize));
    renderPaginationHTML('autoErrorsPagination', currentAutoErrPage, totalErrPages, 'goToAutoErrPage');

    document.getElementById('lastRefreshTimeAuto').textContent = `마지막 새로고침: ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    console.error("자동 로그 로딩 실패:", err);
  }
}

// ──────────────────────────────────────
// Tab 2 추가: 자동 크롤링 브랜드 현황 카드
// ──────────────────────────────────────
async function fetchAutoBrandStatus() {
  const grid = document.getElementById('autoBrandStatusGrid');
  if (!grid) return;
  try {
    const res = await fetch(`/api/admin/crawling/staging?t=${Date.now()}`);
    const data = await res.json();
    if (!data.success) {
      grid.innerHTML = `<div class="col-12 text-center text-danger py-4">현황 조회 실패: ${data.detail}</div>`;
      return;
    }

    let html = '';
    data.brands.forEach(b => {
      // 데이터 없는 브랜드 기본 숨김 처리 (상품이 하나도 수집되거나 운영 등록되지 않은 경우)
      const isEmpty = b.staging_count === 0 && b.prod_count === 0;
      if (window.hideEmptyBrands && isEmpty) return;

      const statusBadge = getStatusBadge(b.integrity_status);
      const targetCount = b.staging_count === 0 ? 0 : (b.target_count || b.staging_count || 1);
      const progressPercent = targetCount === 0 ? 0 : Math.min(100, Math.round((b.staging_count / targetCount) * 100));

      // 카테고리별 세부 테이블 행 조립
      let catRowsHtml = '';
      if (b.categories && b.categories.length > 0) {
        b.categories.forEach(cat => {
          const mismatchWarning = cat.staging_count !== cat.embed_count
            ? `<i class="fas fa-exclamation-triangle text-warning ms-1" title="상품수(${cat.staging_count})와 임베딩수(${cat.embed_count}) 불일치!"></i>`
            : '';
          const targetStr = cat.target_count ? ` / ${cat.target_count}` : '';
          catRowsHtml += `
            <tr style="font-size:0.72rem;">
              <td class="fw-bold text-secondary">${cat.category}</td>
              <td class="text-end">${cat.staging_count}${targetStr} ${mismatchWarning}</td>
              <td class="text-end text-muted">${cat.embed_count}</td>
              <td class="text-end text-muted">${cat.img_count}</td>
              <td class="text-end text-muted">${cat.naver_count}</td>
            </tr>`;
        });
      } else {
        catRowsHtml = `<tr><td colspan="5" class="text-center py-2 text-muted" style="font-size:0.7rem;">집계된 카테고리 데이터 없음</td></tr>`;
      }

      // 수집률에 따른 게이지 색상 결정
      let gaugeColor = 'linear-gradient(90deg, #10b981 0%, #059669 100%)';
      if (progressPercent < 30) gaugeColor = 'linear-gradient(90deg, #ef4444 0%, #dc2626 100%)';
      else if (progressPercent < 70) gaugeColor = 'linear-gradient(90deg, #f59e0b 0%, #d97706 100%)';

      // 최근 수집 시각
      const latestDtStr = b.latest_dt ? new Date(b.latest_dt).toLocaleString('ko-KR') : '수집 이력 없음';

      html += `
      <div class="col-12 col-md-6 col-xl-4">
        <div class="brand-card shadow-sm border-0 mb-3">
          <div class="card-header px-3 py-2 d-flex justify-content-between align-items-center"
               style="background: linear-gradient(135deg, #064e3b 0%, #065f46 100%); color: #fff; border-radius: 14px 14px 0 0;">
            <span class="fw-bold" style="letter-spacing:0.5px;">
              <i class="fas fa-robot me-2 text-emerald-300" style="color: #6ee7b7;"></i>${b.brand}
            </span>
            ${statusBadge}
          </div>
          <div class="card-body px-3 py-3">

            <!-- 목표 대비 수집률 게이지 -->
            <div class="mb-3 p-2 bg-light rounded-3">
              <div class="d-flex justify-content-between align-items-center mb-1" style="font-size:0.75rem;">
                <span class="text-muted fw-semibold">목표 수집률 (${b.staging_count.toLocaleString()}/${targetCount.toLocaleString()}개)</span>
                <span class="fw-bold" style="color: ${progressPercent >= 70 ? '#059669' : progressPercent >= 30 ? '#d97706' : '#dc2626'}">${progressPercent}%</span>
              </div>
              <div class="progress" style="height: 8px; background-color: #e9ecef;">
                <div class="progress-bar" role="progressbar"
                     style="width: ${progressPercent}%; background: ${gaugeColor};"></div>
              </div>
            </div>

            <!-- 4개 지표 격자 -->
            <div class="row g-2 mb-3">
              <!-- 상품 수 -->
              <div class="col-6 border-end">
                <div class="metric-label text-primary"><i class="fas fa-hourglass-start me-1"></i>대기 상품 수</div>
                <div class="metric-value text-primary">${b.staging_count.toLocaleString()}</div>
              </div>
              <div class="col-6 ps-2">
                <div class="metric-label text-success"><i class="fas fa-check-circle me-1"></i>운영 상품 수</div>
                <div class="metric-value text-success">${b.prod_count.toLocaleString()}</div>
              </div>
              
              <!-- 임베딩 수 -->
              <div class="col-6 border-end">
                <div class="metric-label"><i class="fas fa-calculator me-1"></i>대기 임베딩</div>
                <div class="fw-semibold text-secondary" style="font-size:0.9rem; ${getMetricStyle(b.embed_count, b.staging_count)}">${b.embed_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
              <div class="col-6 ps-2">
                <div class="metric-label"><i class="fas fa-brain me-1 text-success"></i>운영 임베딩</div>
                <div class="fw-bold text-success" style="font-size:0.95rem; ${getMetricStyle(b.prod_embed_count, b.prod_count)}">${b.prod_embed_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
              
              <!-- 최저가 가격 수 -->
              <div class="col-6 border-end">
                <div class="metric-label"><i class="fas fa-tag me-1"></i>대기 최저가</div>
                <div class="fw-semibold text-secondary" style="font-size:0.9rem; ${getMetricStyle(b.naver_count, b.staging_count * 5)}">${b.naver_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
              <div class="col-6 ps-2">
                <div class="metric-label"><i class="fas fa-tags me-1 text-success"></i>운영 최저가</div>
                <div class="fw-bold text-success" style="font-size:0.95rem; ${getMetricStyle(b.prod_naver_count, b.prod_count * 5)}">${b.prod_naver_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>

              <!-- 이미지 수 -->
              <div class="col-6 border-end">
                <div class="metric-label"><i class="fas fa-image me-1"></i>대기 이미지</div>
                <div class="fw-semibold text-secondary" style="font-size:0.9rem; ${getMetricStyle(b.img_count, b.staging_count)}">${b.img_count.toLocaleString()} / ${b.staging_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
              <div class="col-6 ps-2">
                <div class="metric-label"><i class="fas fa-images me-1 text-success"></i>운영 이미지</div>
                <div class="fw-bold text-success" style="font-size:0.95rem; ${getMetricStyle(b.prod_img_count, b.prod_count)}">${b.prod_img_count.toLocaleString()} / ${b.prod_count.toLocaleString()} <span class="text-muted" style="font-size:.7rem;">개</span></div>
              </div>
            </div>

            <!-- 카테고리별 세부 지표 (항시 노출형으로 변경) -->
            <div class="mb-3">
              <div class="p-2 border rounded bg-white">
                <div class="fw-bold mb-2 pb-1 border-bottom text-success" style="font-size:0.75rem;">
                  <i class="fas fa-list-alt me-1 text-success"></i>카테고리별 세부 지표
                </div>
                <div class="table-responsive">
                  <table class="table table-sm table-borderless mb-0 align-middle">
                    <thead>
                      <tr class="text-muted border-bottom" style="font-size:0.68rem;">
                        <th>카테고리</th>
                        <th class="text-end">수집/목표</th>
                        <th class="text-end">벡터</th>
                        <th class="text-end">이미지</th>
                        <th class="text-end">최저가</th>
                      </tr>
                    </thead>
                    <tbody>${catRowsHtml}</tbody>
                  </table>
                </div>
              </div>
            </div>

            <!-- 최근 수집 시각 -->  
            <div class="pt-2 border-top">
              <small class="text-muted" style="font-size:.72rem;">
                <i class="fas fa-history me-1"></i>${latestDtStr}
                ${b.pipeline_error_count > 0 ? `<span class="badge bg-danger ms-1">에러 ${b.pipeline_error_count}</span>` : ''}
              </small>
            </div>

          </div>
        </div>
      </div>`;
    });

    if (html === '') {
      html = `<div class="col-12 text-center text-muted py-5"><i class="fas fa-info-circle me-1"></i>현재 표시할 수집 활성 데이터가 존재하지 않습니다. 상단의 '전체 브랜드 보기'를 클릭해 주세요.</div>`;
    }
    grid.innerHTML = html;
  } catch (err) {
    grid.innerHTML = `<div class="col-12 text-center text-danger py-4">서버 오류: ${err.message}</div>`;
  }
}





// ──────────────────────────────────────
// Helper: 에러 분류 헬퍼 함수
// ──────────────────────────────────────
function getErrorCategoryAndClass(errType) {
  if (errType === 'IMAGE_UPLOAD_WARN') {
    return { label: '이미지 수집', badgeClass: 'badge-cat-image' };
  } else if (errType === 'NAVER_API_WARN') {
    return { label: '네이버 최저가', badgeClass: 'badge-cat-naver' };
  } else if (errType === 'EMBEDDING_WARN') {
    return { label: '임베딩 추출', badgeClass: 'badge-cat-embed' };
  } else {
    return { label: '상품 수집', badgeClass: 'badge-cat-crawl' };
  }
}

// ─── 단일 상품 재수집 실행 ───
async function retrySingleProduct(brand, productId) {
  if (!confirm(`상품 [${productId}] 에 대해 즉시 단일 재수집을 백그라운드에서 실행하시겠습니까?\n이 작업은 스테이징 데이터를 실시간 갱신합니다.`)) {
    return;
  }
  try {
    const res = await fetch('/api/admin/crawling/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand, product_id: productId })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message);
      // 3초 후 데이터 갱신
      setTimeout(loadAutoData, 3000);
    } else {
      alert(`재수집 요청 에러: ${data.detail || '알 수 없는 오류'}`);
    }
  } catch (err) {
    alert(`통신 실패: ${err.message}`);
  }
}

// ─── 자동 에러 상세 진단 모달 오픈 ───
function openAutoErrDetailModal(errorId) {
  const err = autoErrorsCache[errorId];
  if (!err) return;

  document.getElementById('modalBrand').textContent = err.brand ? err.brand.toUpperCase() : 'UNKNOWN';
  document.getElementById('modalErrType').textContent = err.error_type;
  document.getElementById('modalProductId').textContent = err.product_id || '누락';
  document.getElementById('modalTime').textContent = new Date(err.created_at).toLocaleString();
  document.getElementById('modalMessage').textContent = err.error_message;
  
  const urlSection = document.getElementById('modalUrlSection');
  const urlLink = document.getElementById('modalUrl');
  if (err.source_url && err.source_url.startsWith('http')) {
    urlSection.style.display = 'block';
    urlLink.href = err.source_url;
    urlLink.textContent = err.source_url;
  } else {
    urlSection.style.display = 'none';
  }

  const trace = err.stack_trace ? err.stack_trace.trim() : '이 오류에 기록된 추가 Stack Trace 정보가 없습니다.';
  document.getElementById('modalStackTrace').textContent = trace;

  // 단일 상품 재수집 버튼 동적 이벤트 연결
  const retryBtn = document.getElementById('modalRetryBtn');
  if (retryBtn) {
    if (err.brand && err.product_id) {
      retryBtn.disabled = false;
      retryBtn.onclick = async () => {
        bootstrap.Modal.getInstance(document.getElementById('autoErrDetailModal')).hide();
        await retrySingleProduct(err.brand, err.product_id);
      };
    } else {
      retryBtn.disabled = true;
      retryBtn.onclick = null;
    }
  }

  const modal = new bootstrap.Modal(document.getElementById('autoErrDetailModal'));
  modal.show();
}

function copyStackTrace() {
  const traceText = document.getElementById('modalStackTrace').textContent;
  navigator.clipboard.writeText(traceText).then(() => {
    alert("스택 트레이스 정보가 클립보드에 성공적으로 복사되었습니다.");
  }).catch(err => {
    alert("복사에 실패했습니다: " + err);
  });
}

function getMetricStyle(value, total) {
  if (total <= 0) return '';
  const diff = total - value;
  if (diff <= 0) return '';
  
  const diffRate = (diff / total) * 100;
  if (diffRate >= 20) {
    // 20% 이상 누락 (심각한 경고: 옅은 빨간색 배경 배지 + 진한 빨강 폰트)
    return 'color: #b91c1c !important; font-weight: 800; background-color: #fee2e2 !important; border: 1px solid #fca5a5 !important; padding: 1px 5px; border-radius: 4px; display: inline-block;';
  } else {
    // 20% 미만 누락 (경미한 경고: 진한 주황색 폰트만 강조)
    return 'color: #ea580c !important; font-weight: 800;';
  }
}

function getStatusBadge(status) {
  switch(status) {
    case 'ready': return `<span class="status-badge badge-ready"><span class="pulse-dot dot-ready"></span>대기 완료 (Ready)</span>`;
    case 'waiting': return `<span class="status-badge badge-waiting"><span class="pulse-dot dot-waiting"></span>검증 대기 (Waiting)</span>`;
    case 'img_missing': return `<span class="status-badge badge-img-missing"><span class="pulse-dot dot-failed"></span>이미지 누락</span>`;
    case 'empty': return `<span class="status-badge badge-empty"><span class="pulse-dot dot-unknown"></span>수집 대기 (Empty)</span>`;
    case 'running': return `<span class="status-badge badge-running"><span class="pulse-dot dot-running"></span>수집 중 (Running)</span>`;
    case 'blocked': return `<span class="status-badge badge-blocked"><span class="pulse-dot dot-blocked"></span>차단됨 (Blocked)</span>`;
    default: return `<span class="status-badge badge-unknown"><span class="pulse-dot dot-unknown"></span>상태 불명</span>`;
  }
}

// ─── 실행 모드 토글에 따른 입력란 비활성화 제어 ───
function toggleCrawlModeInput() {
  const isAuto = document.getElementById('modeAuto').checked;
  const limitInput = document.getElementById('crawlLimit');
  const limitHelp = document.getElementById('crawlLimitHelp');
  if (isAuto) {
    limitInput.disabled = true;
    limitInput.value = "";
    limitHelp.style.display = "none";
  } else {
    limitInput.disabled = false;
    limitInput.value = "10";
    limitHelp.style.display = "block";
  }
}

// ─── 수동 크롤링 실행 모달 오픈 ───
function openCrawlModal(brand) {
  document.getElementById('crawlBrand').value = brand;
  document.getElementById('crawlBrandDisplay').value = brand.toUpperCase();
  document.getElementById('modeManual').checked = true;
  
  // 모드 및 입력창 초기화
  const limitInput = document.getElementById('crawlLimit');
  limitInput.disabled = false;
  limitInput.value = 10;
  document.getElementById('crawlLimitHelp').style.display = "block";
  
  const modal = new bootstrap.Modal(document.getElementById('runCrawlModal'));
  modal.show();
}

async function submitCrawl() {
  const brand = document.getElementById('crawlBrand').value;
  const isAuto = document.getElementById('modeAuto').checked;
  let limit = 0;
  
  if (!isAuto) {
    const rawVal = document.getElementById('crawlLimit').value.trim();
    if (rawVal === "") {
      alert("수동 크롤링 시 최대 수집 상품 수를 입력해 주세요.");
      return;
    }
    const parsedLimit = parseInt(rawVal, 10);
    if (isNaN(parsedLimit) || parsedLimit <= 0) {
      alert("최대 수집 상품 수는 1 이상의 정수로 입력해 주세요.");
      return;
    }
    limit = parsedLimit;
  }
  
  const category = document.getElementById('crawlCategory').value;
  const forceDownload = document.getElementById('crawlForceDownload').checked;
  
  const btn = document.getElementById('confirmRunCrawlBtn');
  btn.disabled = true;
  btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>요청 중...';
  
  try {
    const res = await fetch('/api/admin/crawling/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand, limit, category, is_auto: isAuto, force_download: forceDownload })
    });
    const data = await res.json();
    
    bootstrap.Modal.getInstance(document.getElementById('runCrawlModal')).hide();
    
    if (res.ok) {
      const displayLimitStr = isAuto ? "전체" : limit;
      // 프론트엔드 캐시 즉시 강제 갱신으로 반응속도 최적화
      cachedProgressState[brand] = {
        step: '크롤러 기동 중...',
        percent: '1%',
        width: '1%',
        item: '크롤링 파이프라인 리소스 준비 중',
        time: '경과 시간: 0초',
        count: `0 / ${displayLimitStr}`,
        isRunning: true
      };
      
      // UI 요소 즉시 강제 갱신
      const stepEl = document.getElementById(`progress-step-${brand}`);
      const percentEl = document.getElementById(`progress-percent-${brand}`);
      const barEl = document.getElementById(`progress-bar-${brand}`);
      const itemEl = document.getElementById(`progress-item-${brand}`);
      if (stepEl) stepEl.textContent = '크롤러 기동 중...';
      if (percentEl) percentEl.textContent = '1%';
      if (barEl) {
        barEl.style.width = '1%';
        barEl.classList.add('progress-bar-animated', 'progress-bar-striped');
      }
      if (itemEl) itemEl.textContent = '크롤링 파이프라인 리소스 준비 중';

      alert(data.message || '수동 크롤링이 성공적으로 시작되었습니다.');
      // 딜레이 없이 즉시 데이터 갱신
      loadManualData();
    } else {
      alert(`크롤링 실행 에러: ${data.detail || '알 수 없는 서버 오류'}`);
    }
  } catch (err) {
    alert(`네트워크 통신 오류: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = '백그라운드 실행';
  }
}

// ─── 수동 크롤링 강제 중지 실행 ───
async function stopCrawl(brand) {
  if (!confirm(`정말로 [${brand.toUpperCase()}] 브랜드의 크롤링 작업을 강제 중지하시겠습니까?\n실행 중인 브라우저와 파이프라인 프로세스가 강제로 종료됩니다.`)) {
    return;
  }
  
  try {
    const res = await fetch('/api/admin/crawling/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message);
      // UI 즉시 강제 중지 상태 갱신
      const stepEl = document.getElementById(`progress-step-${brand}`);
      const percentEl = document.getElementById(`progress-percent-${brand}`);
      const barEl = document.getElementById(`progress-bar-${brand}`);
      const itemEl = document.getElementById(`progress-item-${brand}`);
      if (stepEl) stepEl.textContent = '강제 중지됨';
      if (percentEl) percentEl.textContent = '0%';
      if (barEl) {
        barEl.style.width = '0%';
        barEl.classList.remove('progress-bar-animated', 'progress-bar-striped');
      }
      if (itemEl) itemEl.textContent = '사용자에 의해 작업이 강제 중지되었습니다.';
      
      loadManualData();
    } else {
      alert(`작업 중지 오류: ${data.detail || '알 수 없는 서버 오류'}`);
    }
  } catch (err) {
    alert(`네트워크 통신 오류: ${err.message}`);
  }
}

// ─── 수동 스위칭 모달 오픈 ───
function openSwapModal(brand) {
  pendingSwapBrand = brand;

  const stats = currentStagingCounts[brand] || { staging: 0, prod: 0, embed: 0, naver: 0, img: 0, status: 'unknown' };
  const stagingCount = stats.staging;
  const prodCount = stats.prod;
  const imgCount = stats.img;
  const embedCount = stats.embed;
  const naverCount = stats.naver;

  let imgMsg = '';
  if (imgCount < stagingCount) {
    imgMsg = `<div class="alert-banner alert-danger-banner p-3 mb-3">
      <i class="fas fa-exclamation-triangle me-1"></i> 이미지 업로드 누락 건이 발견되었습니다. 
      스위칭 시 일부 상품에 이미지가 표시되지 않을 수 있습니다.
    </div>`;
  }

  let countDetails = `
    <div class="row g-2 mb-3">
      <div class="col-6 text-center p-3 rounded-3" style="background:#f0f9ff;">
        <div class="text-muted mb-1" style="font-size:.78rem;">수집 데이터 (Staging)</div>
        <div class="fw-bold fs-4 text-primary">${stagingCount.toLocaleString()}</div>
        <div class="text-muted text-start mt-2 border-top pt-1" style="font-size:.7rem;">
          - 이미지 업로드: ${imgCount.toLocaleString()} 개<br>
          - 임베딩(벡터): ${embedCount.toLocaleString()} 개<br>
          - 네이버 최저가: ${naverCount.toLocaleString()} 개
        </div>
      </div>
      <div class="col-6 text-center p-3 rounded-3" style="background:#f0fdf4;">
        <div class="text-muted mb-1" style="font-size:.78rem;">운영 데이터 (Production)</div>
        <div class="fw-bold fs-4 text-success">${prodCount.toLocaleString()}</div>
        <div class="text-muted text-start mt-2 border-top pt-1" style="font-size:.7rem;">
          이관 시 기존 운영 데이터가 Staging 데이터로 완전히 교체되며, Staging의 기생성된 이미지/텍스트 벡터와 네이버 쇼핑 데이터가 초고속 이관(1초 미만)됩니다.
        </div>
      </div>
    </div>`;

  document.getElementById('swapModalBody').innerHTML = `
    ${imgMsg}
    ${countDetails}
    <p class="text-muted mb-0 mt-3" style="font-size:.82rem;">
      <strong>${brand.toUpperCase()}</strong> 브랜드의 스테이징 데이터를 운영 테이블로 전환합니다.<br>
      이 작업은 트랜잭션 방식으로 안전하게 수행됩니다.
    </p>`;

  const modal = new bootstrap.Modal(document.getElementById('swapConfirmModal'));
  modal.show();
}

async function executeSwap() {
  const brand = pendingSwapBrand;
  if (!brand) return;

  const btn = document.getElementById('confirmSwapBtn');
  btn.disabled = true;
  btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>처리 중...';

  try {
    const res = await fetch('/api/admin/crawling/swap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand, force: true }),
    });
    const data = await res.json();

    if (res.ok && data.success) {
      alert(data.message || '스위칭이 성공적으로 완료되었습니다.');
      bootstrap.Modal.getInstance(document.getElementById('swapConfirmModal')).hide();
      loadManualData();
    } else {
      alert(`스위칭 실패: ${data.detail || '알 수 없는 서버 에러'}`);
    }
  } catch (err) {
    alert(`네트워크 통신 에러: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = '스위칭 시작';
  }
}

// ─── 스테이징 임시 데이터 삭제 ───
async function clearStaging(brand) {
  if (!confirm(`${brand.toUpperCase()} 브랜드의 모든 스테이징 임시 데이터(상품, 임베딩, 네이버 최저가 포함)를 삭제하시겠습니까?\n이 작업은 되돌릴 수 없습니다.`)) {
    return;
  }

  try {
    const res = await fetch(`/api/admin/crawling/staging/${brand}`, {
      method: 'DELETE'
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(`${brand.toUpperCase()} 스테이징 데이터가 성공적으로 초기화되었습니다.`);
      loadManualData();
    } else {
      alert(`삭제 에러: ${data.detail || '알 수 없는 오류'}`);
    }
  } catch (err) {
    alert(`삭제 요청 중 에러 발생: ${err.message}`);
  }
}

// ─── 페이징 이벤트 핸들러 ───
// ─── 공통 페이지네이션 동적 렌더러 ───
function renderPaginationHTML(containerId, currentPage, totalPages, clickHandlerName) {
  const container = document.getElementById(containerId);
  if (!container) return;

  let html = '';
  
  // 이전 버튼
  const prevDisabled = currentPage <= 1 ? 'disabled' : '';
  const prevClick = currentPage <= 1 ? '' : `onclick="${clickHandlerName}(${currentPage - 1})"`;
  html += `<li class="page-item ${prevDisabled}">
    <a class="page-link" href="javascript:void(0)" ${prevClick} aria-label="Previous" style="font-size: 0.72rem; padding: 4px 8px;">
      <span aria-hidden="true">&laquo; 이전</span>
    </a>
  </li>`;

  // 10개 단위 슬라이딩 페이지 표시
  const pageGroup = Math.ceil(currentPage / 10);
  const startPage = (pageGroup - 1) * 10 + 1;
  const endPage = Math.min(totalPages, pageGroup * 10);

  for (let i = startPage; i <= endPage; i++) {
    const activeClass = i === currentPage ? 'active' : '';
    html += `<li class="page-item ${activeClass}">
      <a class="page-link" href="javascript:void(0)" onclick="${clickHandlerName}(${i})" style="font-size: 0.72rem; padding: 4px 8px;">${i}</a>
    </li>`;
  }

  // 다음 버튼
  const nextDisabled = currentPage >= totalPages ? 'disabled' : '';
  const nextClick = currentPage >= totalPages ? '' : `onclick="${clickHandlerName}(${currentPage + 1})"`;
  html += `<li class="page-item ${nextDisabled}">
    <a class="page-link" href="javascript:void(0)" ${nextClick} aria-label="Next" style="font-size: 0.72rem; padding: 4px 8px;">
      <span>다음 &raquo;</span>
    </a>
  </li>`;

  container.innerHTML = html;
}

function goToRunPage(page) {
  if (page < 1) return;
  
  // 페이지 이동 시 선택 필터가 있으면 해제하여 정합성 보장
  selectedRunId = null;
  const clearBtn = document.getElementById('clearRunFilterBtn');
  if (clearBtn) {
    clearBtn.classList.add('d-none');
  }
  
  // 구동 내역 목록에서 모든 행 스타일 초기화
  const rows = document.querySelectorAll('#runsTableBody tr');
  rows.forEach(row => {
    row.style.backgroundColor = '';
    row.style.borderLeft = '';
  });

  currentRunPage = page;
  fetchManualLogs();
}

function goToErrPage(page) {
  if (page < 1) return;
  currentErrPage = page;
  renderErrorsEngine();
}

function goToAutoRunPage(page) {
  if (page < 1) return;
  currentAutoRunPage = page;
  fetchAutoLogs();
}

function goToAutoErrPage(page) {
  if (page < 1) return;
  currentAutoErrPage = page;
  fetchAutoLogs();
}

// ─── 특정 구동건 에러 필터링 핸들러 ───
function selectRun(runId) {
  runId = Number(runId);

  if (selectedRunId === runId) {
    clearRunFilter();
    return;
  }
  selectedRunId = runId;
  currentErrPage = 1; // 에러 목록 페이지 초기화
  
  const clearBtn = document.getElementById('clearRunFilterBtn');
  if (clearBtn) {
    clearBtn.classList.remove('d-none');
  }
  
  // 구동 내역 목록에서 클릭된 행 스타일 즉시 업데이트 (딜레이 없음)
  const rows = document.querySelectorAll('#runsTableBody tr');
  rows.forEach(row => {
    if (row.getAttribute('onclick') && row.getAttribute('onclick').includes(`selectRun(${runId})`)) {
      row.style.backgroundColor = '#f0fdf4';
      row.style.borderLeft = '4px solid #16a34a';
    } else {
      row.style.backgroundColor = '';
      row.style.borderLeft = '';
    }
  });

  // 백엔드 API 딜레이 없이 로컬 캐시에서 즉시 필터 렌더링 (렉 0%)
  renderErrorsEngine();

  // 최신 에러 로그 갱신을 위해 백그라운드로 조용히 fetch
  loadManualData(true);
}

function clearRunFilter() {
  selectedRunId = null;
  currentErrPage = 1;
  
  const clearBtn = document.getElementById('clearRunFilterBtn');
  if (clearBtn) {
    clearBtn.classList.add('d-none');
  }
  
  // 구동 내역 목록에서 모든 행 스타일 즉시 초기화
  const rows = document.querySelectorAll('#runsTableBody tr');
  rows.forEach(row => {
    row.style.backgroundColor = '';
    row.style.borderLeft = '';
  });

  // 백엔드 API 딜레이 없이 로컬 캐시에서 즉시 전체 렌더링 (렉 0%)
  renderErrorsEngine();

  // 최신 에러 로그 갱신을 위해 백그라운드로 조용히 fetch
  loadManualData(true);
}

function renderErrorsEngine() {
  const errorsBody = document.getElementById('errorsTableBody');
  if (!errorsBody) return;
  
  errorsBody.style.opacity = '1';
  
  let filtered = cachedManualErrors;
  if (selectedRunId) {
    filtered = cachedManualErrors.filter(e => Number(e.run_id) === Number(selectedRunId));
  }
  
  const totalErrCount = filtered.length;
  const totalErrPages = Math.max(1, Math.ceil(totalErrCount / pageSize));
  
  // 현재 에러 페이지가 범위를 벗어나지 않도록 보정
  if (currentErrPage > totalErrPages) {
    currentErrPage = totalErrPages;
  }
  if (currentErrPage < 1) {
    currentErrPage = 1;
  }
  
  // 페이징에 따른 슬라이싱
  const startIdx = (currentErrPage - 1) * pageSize;
  const endIdx = startIdx + pageSize;
  const pageItems = filtered.slice(startIdx, endIdx);
  
  // 페이징 UI 업데이트
  renderPaginationHTML('errorsPagination', currentErrPage, totalErrPages, 'goToErrPage');
  
  if (pageItems && pageItems.length > 0) {
    let errHtml = '';
    pageItems.forEach(e => {
      const timeStr = new Date(e.created_at).toLocaleTimeString();
      const runIdStr = e.display_run_id ? `[#${e.display_run_id}]` : (e.run_id ? `[#${e.run_id}]` : '[#-]');
      const brandPrefix = e.brand ? `<span class="badge bg-secondary me-1">[${e.brand}]</span>` : '';
      const prodSuffix = e.product_id ? `<span class="badge bg-light text-dark border ms-1" style="font-size:.68rem;">ID: ${e.product_id}</span>` : '';
      
      errHtml += `
        <tr>
          <td><small class="text-muted">${timeStr} <span class="text-primary fw-bold" style="font-size:.7rem;">${runIdStr}</span></small></td>
          <td><span class="text-danger fw-semibold" style="font-size:.73rem;">${e.error_type}</span></td>
          <td>
            <div class="d-flex align-items-center flex-wrap" style="gap: 2px;">
              ${brandPrefix}
              <div class="text-wrap text-break" style="font-size:.75rem; max-width: 480px;" title="${e.error_message}">
                ${e.error_message}
              </div>
              ${prodSuffix}
            </div>
          </td>
        </tr>`;
    });
    errorsBody.innerHTML = errHtml;
  } else {
    errorsBody.innerHTML = '<tr><td colspan="3" class="text-center py-4 text-muted">최근 에러 내역이 없습니다.</td></tr>';
  }
}

// ─── 자동 구동 내역 에러 필터링 핸들러 ───
function selectAutoRun(runId) {
  runId = Number(runId);
  if (selectedAutoRunId === runId) {
    clearAutoRunFilter();
    return;
  }
  selectedAutoRunId = runId;
  currentAutoErrPage = 1; // 에러 목록 페이지 초기화
  
  const clearBtn = document.getElementById('clearAutoRunFilterBtn');
  if (clearBtn) {
    clearBtn.classList.remove('d-none');
  }
  
  fetchAutoLogs();
}

function clearAutoRunFilter() {
  selectedAutoRunId = null;
  currentAutoErrPage = 1;
  
  const clearBtn = document.getElementById('clearAutoRunFilterBtn');
  if (clearBtn) {
    clearBtn.classList.add('d-none');
  }
  
  fetchAutoLogs();
}

// ─── 크롤링 히스토리 초기화 ───
async function clearCrawlingHistory(type) {
  const typeText = type === 'manual' ? '수동 크롤링' : (type === 'auto' ? '자동 크롤링' : '전체 크롤링');
  if (!confirm(`${typeText}의 기록된 모든 히스토리(구동 내역 및 에러 로그)를 초기화하시겠습니까?\n이 작업은 되돌릴 수 없습니다.`)) {
    return;
  }
  
  try {
    const res = await fetch(`/api/admin/crawling/history?pipeline_type=${type}`, {
      method: 'DELETE'
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message || '히스토리가 성공적으로 초기화되었습니다.');
      if (type === 'manual') {
        currentRunPage = 1;
        currentErrPage = 1;
        selectedRunId = null;
        const clearBtn = document.getElementById('clearRunFilterBtn');
        if (clearBtn) clearBtn.classList.add('d-none');
        loadManualData();
      } else {
        currentAutoRunPage = 1;
        currentAutoErrPage = 1;
        selectedAutoRunId = null;
        const clearBtn = document.getElementById('clearAutoRunFilterBtn');
        if (clearBtn) clearBtn.classList.add('d-none');
        loadAutoData();
      }
    } else {
      alert(`초기화 실패: ${data.detail || '알 수 없는 서버 오류'}`);
    }
  } catch (err) {
    alert(`네트워크 통신 오류: ${err.message}`);
  }
}

// 동적 생성된 브랜드 카드의 카테고리별 상세 정보 아코디언 토글 제어 함수
function toggleCategoryAccordion(btn, brand) {
  const el = document.getElementById(`catDetail-${brand}`);
  if (!el) return;
  
  let collapseInstance = bootstrap.Collapse.getInstance(el);
  if (!collapseInstance) {
    collapseInstance = new bootstrap.Collapse(el, {
      toggle: false
    });
  }
  collapseInstance.toggle();

  if (btn) {
    const icon = btn.querySelector('.fa-chevron-down, .fa-chevron-up');
    if (icon) {
      el.addEventListener('shown.bs.collapse', () => {
        icon.classList.remove('fa-chevron-down');
        icon.classList.add('fa-chevron-up');
      }, { once: true });
      el.addEventListener('hidden.bs.collapse', () => {
        icon.classList.remove('fa-chevron-up');
        icon.classList.add('fa-chevron-down');
      }, { once: true });
    }
  }
}

// ─── [Tab 3] 에러 상품 통제 및 핀포인트 복구 관련 JS ───
let selectedErrorProductIds = new Set();
let currentErrorProductPage = 1;

async function loadErrorProducts(page = 1) {
  currentErrorProductPage = page;
  const brand = document.getElementById('errorBrandSelect').value;
  const tableBody = document.getElementById('errorProductsTableBody');
  const pagination = document.getElementById('errorProductsPagination');
  
  if (!tableBody) return;
  
  tableBody.innerHTML = `<tr><td colspan="8" class="text-center py-5"><i class="fas fa-spinner fa-spin me-1"></i>에러 상품 목록을 가져오는 중...</td></tr>`;
  
  try {
    const res = await fetch(`/api/admin/pipeline/errors/products?brand=${brand}&page=${page}&limit=15`);
    const data = await res.json();
    
    if (res.ok && data.success) {
      if (data.products.length === 0) {
        tableBody.innerHTML = `<tr><td colspan="8" class="text-center py-5 text-muted"><i class="fas fa-check-circle me-1 text-success"></i>격리 보류된 에러 상품이 없습니다. 수집 상태가 무결합니다!</td></tr>`;
        pagination.innerHTML = '';
        updateSelectedBadge();
        return;
      }
      
      tableBody.innerHTML = '';
      data.products.forEach(p => {
        // 오류 유형 한글 배지 치환
        let typeBadge = '';
        if (p.error_type === 'NAVER_PRICE_MISSING') typeBadge = '<span class="badge bg-danger">가격 5개 유실</span>';
        else if (p.error_type === 'IMAGE_BROKEN') typeBadge = '<span class="badge bg-warning text-dark">이미지 깨짐</span>';
        else if (p.error_type === 'EMBEDDING_MISSING') typeBadge = '<span class="badge bg-info text-dark">임베딩 누락</span>';
        else if (p.error_type === 'METADATA_LOSS') typeBadge = '<span class="badge bg-secondary">필수메타 누락</span>';
        else typeBadge = `<span class="badge bg-dark">${p.error_type}</span>`;

        const isChecked = selectedErrorProductIds.has(p.product_id) ? 'checked' : '';
        const imgHtml = p.image_url ? `<img src="${p.image_url}" class="rounded" style="width:36px; height:36px; object-fit:cover;">` : '<span class="text-muted small">없음</span>';

        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td class="text-center">
            <input class="form-check-input error-select-chk" type="checkbox" data-id="${p.product_id}" ${isChecked} onchange="toggleErrorProductSelection('${p.product_id}', this.checked)">
          </td>
          <td class="font-monospace text-secondary">${p.product_id}</td>
          <td class="fw-semibold">${p.product_name || '상품명 없음'}</td>
          <td><span class="badge bg-light text-dark">${p.category || '미지정'}</span></td>
          <td>${typeBadge}</td>
          <td class="text-muted text-wrap" style="max-width: 250px; font-size: 0.8rem;">${p.error_message}</td>
          <td class="text-center">${imgHtml}</td>
          <td class="text-center">
            <button class="btn btn-xs btn-outline-warning fw-bold" style="font-size: 0.72rem;" onclick="retrySingleProductInError('${p.product_id}')">
              <i class="fas fa-redo me-1"></i>재수집
            </button>
          </td>
        `;
        tableBody.appendChild(tr);
      });
      
      // 페이지네이션 바인딩
      renderErrorPagination(data.total, page, 15);
      
    } else {
      tableBody.innerHTML = `<tr><td colspan="8" class="text-center py-5 text-danger">조회 실패: ${data.detail || '서버 오류'}</td></tr>`;
    }
  } catch (err) {
    tableBody.innerHTML = `<tr><td colspan="8" class="text-center py-5 text-danger">네트워크 통신 오류: ${err.message}</td></tr>`;
  }
  updateSelectedBadge();
}

function toggleErrorProductSelection(prodId, isChecked) {
  if (isChecked) {
    selectedErrorProductIds.add(prodId);
  } else {
    selectedErrorProductIds.delete(prodId);
  }
  updateSelectedBadge();
}

function toggleSelectAllErrors(headerCheckbox) {
  const chks = document.querySelectorAll('.error-select-chk');
  chks.forEach(chk => {
    chk.checked = headerCheckbox.checked;
    const prodId = chk.getAttribute('data-id');
    toggleErrorProductSelection(prodId, headerCheckbox.checked);
  });
}

function updateSelectedBadge() {
  const badge = document.getElementById('selectedCountBadge');
  if (badge) {
    badge.textContent = `${selectedErrorProductIds.size}개 선택됨`;
  }
}

function renderErrorPagination(total, currentPage, limit) {
  const totalPages = Math.ceil(total / limit);
  const pagination = document.getElementById('errorProductsPagination');
  if (!pagination) return;
  
  pagination.innerHTML = '';
  
  if (totalPages <= 1) return;
  
  // Previous button
  const prevLi = document.createElement('li');
  prevLi.className = `page-item ${currentPage === 1 ? 'disabled' : ''}`;
  prevLi.innerHTML = `<a class="page-link" href="#" onclick="loadErrorProducts(${currentPage - 1}); return false;">이전</a>`;
  pagination.appendChild(prevLi);
  
  // Pages
  for (let i = 1; i <= totalPages; i++) {
    const li = document.createElement('li');
    li.className = `page-item ${currentPage === i ? 'active' : ''}`;
    li.innerHTML = `<a class="page-link" href="#" onclick="loadErrorProducts(${i}); return false;">${i}</a>`;
    pagination.appendChild(li);
  }
  
  // Next button
  const nextLi = document.createElement('li');
  nextLi.className = `page-item ${currentPage === totalPages ? 'disabled' : ''}`;
  nextLi.innerHTML = `<a class="page-link" href="#" onclick="loadErrorProducts(${currentPage + 1}); return false;">다음</a>`;
  pagination.appendChild(nextLi);
}

async function resolveSelectedErrors(action) {
  if (selectedErrorProductIds.size === 0) {
    alert('조치할 상품을 최소 1개 이상 선택해 주세요.');
    return;
  }
  
  const brand = document.getElementById('errorBrandSelect').value;
  const actionText = action === 'partial_switch' ? '선택 상품 운영 DB 강제 이관' : (action === 're_embed' ? '선택 상품 임베딩 재연산' : '선택 상품 스테이징 데이터 영구 삭제');
  
  if (!confirm(`선택한 ${selectedErrorProductIds.size}개 상품에 대해 [${actionText}] 조치를 진행하시겠습니까?`)) {
    return;
  }
  
  try {
    const res = await fetch('/api/admin/pipeline/errors/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        brand: brand,
        action: action,
        product_ids: Array.from(selectedErrorProductIds)
      })
    });
    
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message || '조치가 성공적으로 완료되었습니다.');
      selectedErrorProductIds.clear();
      document.getElementById('selectAllErrors').checked = false;
      loadErrorProducts(currentErrorProductPage);
      
      // 수동/자동 데이터 현황 카드 갱신을 위해 데이터 릴로드 호출
      loadManualData(true);
      loadAutoData();
    } else {
      alert(`조치 실패: ${data.detail || '서버 오류'}`);
    }
  } catch (err) {
    alert(`네트워크 통신 중 오류: ${err.message}`);
  }
}

async function retrySingleProductInError(productId) {
  const brand = document.getElementById('errorBrandSelect').value;
  if (!confirm(`상품 [${productId}] 에 대해 즉시 핀포인트 재수집(가격비교 및 임베딩 갱신)을 실행하시겠습니까?`)) {
    return;
  }
  
  try {
    const res = await fetch('/api/admin/pipeline/crawling/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand: brand, product_id: productId })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message || '재수집 및 임베딩 갱신 조치가 성공적으로 수립되었습니다.');
      loadErrorProducts(currentErrorProductPage);
      loadManualData(true);
      loadAutoData();
    } else {
      alert(`재수집 조치 실패: ${data.detail || '서버 오류'}`);
    }
  } catch (err) {
    alert(`네트워크 통신 중 오류: ${err.message}`);
  }
}

// ─── 데이터 없는 브랜드 필터링 전역 상태 및 제어 함수 ───
window.hideEmptyBrands = true; // 기본값: 수집 데이터가 없는 브랜드는 숨김

function toggleEmptyBrandsFilter() {
  window.hideEmptyBrands = !window.hideEmptyBrands;
  
  const manualBtn = document.getElementById('toggleFilterBtnManual');
  const autoBtn = document.getElementById('toggleFilterBtnAuto');
  
  const btns = [manualBtn, autoBtn];
  btns.forEach(btn => {
    if (!btn) return;
    if (window.hideEmptyBrands) {
      btn.className = "btn btn-sm btn-outline-success fw-bold";
      btn.innerHTML = `<i class="fas fa-eye-slash me-1"></i>데이터 없는 브랜드 숨김 중`;
    } else {
      btn.className = "btn btn-sm btn-outline-secondary fw-bold";
      btn.innerHTML = `<i class="fas fa-eye me-1"></i>전체 브랜드 보기 중`;
    }
  });

  // 두 탭의 카드 그리드 리로드 호출
  fetchStagingStatus();
  fetchAutoBrandStatus();
}


