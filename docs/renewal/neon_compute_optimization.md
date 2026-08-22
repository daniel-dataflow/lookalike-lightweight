# 4세대 절전 친화적(Zero-Compute Idle) 아키텍처 및 Neon DB 최적화 명세서

> **문서 목적**: 본 문서는 Render 무료 서버와 Neon Serverless PostgreSQL 연동 환경에서 발생한 **Compute Unit(CU-hours) 급증 및 DW DB 일시 정지(Limit Reached) 장애**를 분석하고, 월 0원 무중단 운영을 위해 새롭게 도입된 **4세대 절전 친화적(Zero-Compute Idle) 인메모리 버퍼링 및 자동 페일오버(Failover) 아키텍처**를 정의함.

---

## 1. 장애 분석 및 배경 (Postmortem)

### 1.1 현상
- **`lookalike-PROD_DW`**: 월간 무료 제공 한도인 **100 CU-hrs를 100% 초과(110.2 CU-hrs)**하여 프로젝트가 자동 일시 정지(Paused)됨.
- **`lookalike-PROD_DB`**: 메인 운영 DB 역시 **75.14 CU-hrs (75%)**까지 소모되어 한도 도달 위험에 직면함.
- **총 사용량**: 계정 전체 합산 **179.42 CU-hrs** 소모.

### 1.2 근본 원인 (Root Cause)
1. **Neon Serverless PostgreSQL 절전 메커니즘**:
   - Neon 무료 플랜은 쿼리나 연결이 없을 때 **5분(Auto-suspend)** 후 유휴 상태(Idle / 0 CU)로 들어가야 과금이 발생하지 않음.
   - 최소 활성 크기(0.25 CU)로 24시간 켜져 있으면:
     $$\text{월간 소모량} = 0.25\text{ CU} \times 24\text{시간} \times 30\text{일} = 180\text{ CU-hrs}$$
     → 한 달에 100 CU-hrs 한도를 **약 16~17일 만에 100% 소진**.

2. **서버 측 백그라운드 주기 태스크의 24시간 DB 호출**:
   - 지난 커밋(`a409b95`)에서는 **브라우저 UI의 자동 새로고침(폴링)**만 비활성화함.
   - 하지만 Render 서버가 GitHub Actions의 Keep-Alive 핑(`wakeup_server.yml`, 10분 주기)에 의해 24시간 켜져 있는 상태에서, **FastAPI 내부 백그라운드 태스크**가 브라우저 접속 여부와 무관하게 24시간 내내 DB를 찔러 깨움:
     - `start_metric_collector()`: **5분(300초)마다** `PROD_DW`에 `INSERT`/`DELETE` 실행.
     - `_update_system_health_loop()`: **10분(600초)마다** `PROD_DB` 및 4개 DB에 `SELECT count(*)`, `pg_database_size()` 실행.
     - `NeonLogHandler`: `WARN`/`ERROR` 발생 시마다 `PROD_DW`에 실시간 `INSERT` 수행.
   - **결과**: Neon의 5분 절전 타이머가 매 5분/10분마다 리셋되어 24시간 365일 풀가동됨.

---

## 2. 4세대 아키텍처 핵심 설계 원칙

```
[4세대 절전 친화적 아키텍처]

               FastAPI Server (Render 24/7)
                            │
       ┌────────────────────┼────────────────────┐
       ▼                    ▼                    ▼
[인프라 메트릭]       [실시간 에러 로그]     [시스템 헬스체크]
psutil (5분 주기)     logger.error()       어드민 접속 시에만
       │                    │                    │
       ▼                    ▼                    ▼
Python 메모리 큐     Python 메모리 큐      On-Demand 계산
deque(maxlen=12)     deque(maxlen=200)    (10분 메모리 캐시)
 (DB Write: 0회)     (DB Write: 0회)      (상시 폴링 제거)
                            │
                            ▼
                     Sentry Cloud
                  (영구 보존, 월 5,000건)

────────────────────────────────────────────────────────────
[결과] 사용자 검색/배치가 없을 때 Neon PostgreSQL = 100% 절전 (0 CU)
```

| 구분 | 3세대 (기존) | 4세대 (개선) |
| :--- | :--- | :--- |
| **인프라 CPU/RAM 메트릭** | 5분마다 DW DB `INSERT`/`DELETE` | **Python 인메모리 링 버퍼 (`deque(maxlen=12)`)** (DB 호출 0회) |
| **실시간 에러 모니터링** | WARN/ERROR 시 DW DB 즉시 `INSERT` | **인메모리 버퍼 (`deque(maxlen=200)`) + Sentry Cloud** (DB 호출 0회) |
| **시스템 헬스 상태 수집** | 10분 주기 상시 백그라운드 DB 쿼리 | **관리자 `/admin` 접속 시 On-Demand 조회 + 10분 캐싱** |
| **DW 장애 대응 (Failover)** | 단일 DW DB 장애 시 기능 정지 | **사용량 90% 도달 또는 차단 시 보조 DW DB(`PROD_DW_DATABASE_URL_2`)로 자동 스위칭** |

---

## 3. 세부 구현 사양

### 3.1 인메모리 링 버퍼 메트릭 (`metric.py`)
- `collections.deque(maxlen=12)`를 활용해 최근 1시간(5분 간격 12개 포인트)의 CPU/RAM 시계열 데이터를 프로세스 메모리에만 보관.
- `/api/metrics/stream` 및 `/api/metrics/stats` 요청 시 메모리 큐에서 즉시 연산하여 반환 (응답 속도 0ms, DB 트래픽 0).

### 3.2 인메모리 에러 로그 및 Sentry 분업 (`main.py`, `log.py`)
- Render의 휘발성 파일시스템 제약을 극복하기 위해 이원화:
  1. **실시간 관제**: 어드민 화면에서 최근 발생한 로그를 확인할 수 있도록 `deque(maxlen=200)`에 버퍼링.
  2. **영구 에러 추적**: `sentry-sdk`를 통해 심각한 에러/예외는 Sentry 클라우드에 영구 적재 (무료 티어 월 5,000건).

### 3.3 온디맨드 시스템 헬스 체크 (`admin.py`)
- 상시 실행되던 `_update_system_health_loop()` 백그라운드 루프 제거.
- 관리자가 `/admin` 대시보드에 접근할 때만 `_get_system_health_raw()`를 호출하고, 10분간 인메모리 캐싱(`_system_health_cache`)하여 연속 요청 시 DB 조회를 방지.
- Neon REST API(`https://api.neon.tech/api/v2/projects/{project_id}`)를 최우선으로 사용하여 PostgreSQL 커넥션 없이 메트릭을 수집.

### 3.4 DW 데이터베이스 듀얼 계정, 양방향 자동 전환 및 UI 표기 (`database.py`, `config/base.py`, `admin.py`)
- **환경별 설정 항목 (DEV 및 PROD 대칭 지원)**:
  - **PROD**: `PROD_DW_DATABASE_URL` (1번) / `PROD_DW_DATABASE_URL_2` (2번), `NEON_PROD_DW_PROJECT_ID_2`, `NEON_PROD_DW_API_KEY_2`
  - **DEV**: `DEV_DW_DATABASE_URL` (1번) / `DEV_DW_DATABASE_URL_2` (2번), `NEON_DEV_DW_PROJECT_ID_2`, `NEON_DEV_DW_API_KEY_2`
- **양방향 스위칭 메커니즘 (Failover & Failback)**:
  1. **페일오버 (dw1 $\rightarrow$ dw2)**:
     - 1번 DW의 커넥션 장애(Limit reached, Paused, Timeout) 또는 Neon REST API 사용량이 **90% 이상**(Compute 90 CU-hrs, Network 4.5 GB)에 도달하면 `switch_to_secondary_dw()`를 호출하여 2번 DW로 자동 전환.
     - 2번 DW 활성화 시 필수 테이블 스키마(`_ensure_dw_tables`)를 자동 생성하여 즉시 가동 보장.
  2. **자동 원복 (Failback: dw2 $\rightarrow$ dw1)**:
     - 월초 사용량 리셋(Compute < 80%)이 감지되거나 1번 DW가 정상 복구되면 `switch_to_primary_dw()`가 호출되어 1번 원본 DW로 런타임 자동 복귀.
- **관리자 UI 시각적 인지 장치**:
  - 관리자 대시보드 인프라 모니터링 테이블에서 2번 DW로 전환 시 라벨이 `DEV_DW_DB_2` / `PROD_DW_DB_2`로 변경되고, `[DW_2 전환됨]` 경고 배지가 함께 표시되어 관리자가 즉시 상태를 인지 가능.
- **데이터 영속성 및 안전성**:
  - 실제 서비스 데이터(`products`, `users`, `embeddings` 등)는 `PROD_DB`에 영구 보존되어 있으며, DW는 크롤링 스테이징/배치 임시 버퍼이므로 DW가 dw1 $\leftrightarrow$ dw2로 교체되어도 서비스 연속성에 아무런 영향이 없음.

---

## 4. 기대 효과 및 검증

1. **Neon Compute 소모량 90% 이상 절감**:
   - 백엔드가 24시간 실행되더라도 검색 요청이 없는 유휴 시간대에는 Neon DB가 100% 절전 모드(0 CU)를 유지.
   - 월간 예상 Compute 소모량: 기존 **180 CU-hrs $\rightarrow$ 5~10 CU-hrs 미만**.
2. **DW DB 무중단 연속성 및 완전 자동 순환 (dw1 $\rightarrow$ dw2 $\rightarrow$ dw1)**:
   - 크롤링 배치 등 대량 데이터 적재 시 1번 DW의 한도(100 CU)가 소진되더라도 2번 DW로 무중단 스위칭되고, 월초에 리셋되면 다시 1번으로 자동 원복되어 365일 월 0원 무중단 운영 지속 가능.

