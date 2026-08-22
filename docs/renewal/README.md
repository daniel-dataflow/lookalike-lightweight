# Lookalike 서비스 경량화 및 아키텍처 마이그레이션 변경사항 정리 (Renewal Summary)

본 문서는 Lookalike 서비스의 레거시 분산 환경 아키텍처(Elasticsearch, MongoDB, HDFS, Kafka 등)에서 **단일 서버 및 PostgreSQL/pgvector 중심의 경량 아키텍처**로 마이그레이션하면서 변경되거나 수정된 파일들과 그 상세 변경 사양을 정리합니다.

---

## 1. 주요 아키텍처 변경 요약 (Architectural Shifts)

1. **분산 레거시 미들웨어 제거**:
   * 대규모 인프라 의존성인 **Elasticsearch, MongoDB, Hadoop HDFS, Apache Kafka**를 완전히 제거했습니다.
   * 검색 로그, 파이프라인 지표, 상품 데이터 수집 이력을 모두 **PostgreSQL** 테이블로 단일화했습니다.
   * 백엔드의 백그라운드 Kafka 로그/메트릭 컨슈머 서비스(`kafka_log_consumer.py`, `kafka_metric_consumer.py`)를 삭제하고, FastAPI 라우터 진입점에서 PostgreSQL 직접 적재로 전환하여 인프라를 극적으로 경량화했습니다.
2. **벡터 유사도 검색 전환 (pgvector)**:
   * Elasticsearch Dense Vector 검색 방식을 **PostgreSQL pgvector 익스텐션 및 `product_embeddings` 테이블**을 활용하는 방식으로 마이그레이션했습니다.
   * AI/ML 모델 서빙을 위해 허깅페이스 스페이스 (HuggingFace Space) 연동(VLM, YOLO, CLIP) 및 로컬 CLIP 모델을 연동하여 이미지 특징 추출 및 텍스트 쿼리 임베딩을 수행하고, Cosine Similarity 계산을 SQL 레벨에서 처리합니다.
3. **데이터 파이프라인(Airflow) 구조 단순화**:
   * 기존의 병렬 데이터 파이프라인 단계를 HDFS/MongoDB 적재 대신 **로컬 JSON 보존 + PostgreSQL 직접 삽입** 구조로 대폭 간소화했습니다.
4. **SQLAlchemy 기반 표준 데이터베이스 연동 & SSL 강화**:
   * 기존의 raw `psycopg2` 커넥션 풀 대신 **SQLAlchemy 엔진 및 세션 팩토리**로 전환하여 연결 안정성과 세션 관리를 현대화했습니다.
   * 클라우드 프로덕션 환경(Render 등)의 필수 요구사항인 SSL 보안 연동을 위해 원격지 접속 판별 시 `sslmode=require` 접속 인자가 자동으로 동적 주입되도록 풀 구성을 고도화했습니다.

---

## 2. 컴포넌트별 상세 변경 내역 (Detailed File Changes)

현재까지 커밋되지 않은 수정 사항(`git status` 기준)을 논리적 컴포넌트 단위로 세분화하여 정리합니다.

### A. 데이터 파이프라인 (Airflow)
* **[MODIFY] [airflow.cfg](file:///home/ubuntu/lookalike-lightweight/data-pipeline/airflow/airflow.cfg)**
  * Airflow 환경 내 데이터베이스 커넥션, 로그 경로 및 로컬 실행기(LocalExecutor) 설정을 경량 마이그레이션 환경에 맞춰 최적화했습니다.
* **[MODIFY] [fashion_total_pipeline.py](file:///home/ubuntu/lookalike-lightweight/data-pipeline/airflow/dags/fashion_total_pipeline.py)**
  * 전체 수집 파이프라인 DAG를 PostgreSQL 단일 적재 단계 위주로 재구성하고 불필요한 HDFS/Kafka 전송 오퍼레이터를 제거했습니다.
* **[MODIFY] [tasks 및 functions 폴더 내 파이썬 모듈들](file:///home/ubuntu/lookalike-lightweight/data-pipeline/airflow/dags/tasks)**
  * 대상 파일: `db_tasks.py`, `embed_tasks.py`, `operator_tasks.py`, `text_embed_tasks.py`, `vlm_tasks.py`, `yolo_tasks.py` 및 하위 함수 모듈 (`db_funcs.py`, `embed_funcs.py` 등)
  * MongoDB 및 Elasticsearch API 호출 로직을 완전 삭제하고, PostgreSQL 커넥션을 활용해 `products` 및 `product_embeddings` 테이블로 데이터를 직접 적재하도록 쿼리 로직을 리뉴얼했습니다.

### B. 컨테이너 및 인프라 스크립트
* **[MODIFY] [docker-compose.yml](file:///home/ubuntu/lookalike-lightweight/docker-compose.yml)**
  * 로컬 개발 환경에서 불필요한 kafka, zookeeper, mongodb, elasticsearch, hadoop 관련 서비스 정의를 제거하고 PostgreSQL(5433), Redis, FastAPI, Airflow 서비스 중심으로 도커 환경을 간소화했습니다.
* **[MODIFY] [requirements.txt](file:///home/ubuntu/lookalike-lightweight/ml-models/api/requirements.txt) & [backend/requirements.txt](file:///home/ubuntu/lookalike-lightweight/web/backend/requirements.txt)**
  * 레거시 라이브러리(elasticsearch, pymongo, kafka-python) 패키지를 제거하고, PostgreSQL 연동에 필요한 `psycopg2-binary`, Pydantic v2 관련 호환 라이브러리, DB 표준 연동을 위한 `sqlalchemy`, 그리고 임베딩 처리를 위한 패키지들을 추가했습니다.
* **[MODIFY] [scripts 폴더 내 실행 쉘 스크립트](file:///home/ubuntu/lookalike-lightweight/scripts)**
  * 대상 파일: `start_all.sh`, `stop_all.sh`, `restart_all.sh`
  * 분산 미들웨어 컨테이너 모니터링 단계를 스킵하고 경량 단일 서버 구동 중심으로 스크립트를 재정비했습니다.

### C. 웹 백엔드 설정 & 데이터베이스
* **[MODIFY] [base.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/config/base.py)**
  * 어드민 보안 접속용 계정 설정 정보(`ADMIN_USERNAME`, `ADMIN_PASSWORD`)를 환경변수 로더(`Settings`)에 통합 매핑하여 안정적인 로그인 검증 기반을 마련했습니다.
  * `DATABASE_URL`을 Pydantic 필드로 등록하고 `@model_validator(mode="after")`를 활용한 동적 폴백 처리를 적용하여, 로컬/도커 디버그 모드와 외부 프로덕션 모드 설정이 포트 충돌 없이 깔끔하게 분기되도록 개선했습니다.
* **[MODIFY] [database.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/database.py)**
  * 기존 `psycopg2` 전용 `ThreadedConnectionPool` 기반의 로직을 **SQLAlchemy 엔진 및 Connection Pool** 구동으로 전환했습니다.
  * 렌더(Render) 프로덕션 환경 등 외부 DB 접속 시 안전한 암호화 채널을 의무적으로 생성할 수 있도록, 원격지 접속 판별 시 `sslmode=require` 옵션을 커넥션 인자로 자동 주입하는 보안 가드레일을 설치했습니다.
  * 기존에 작성된 개별 라우터/서비스들의 소스코드 정합성을 해치지 않기 위해 `get_pg_connection()` 및 `get_pg_cursor()` 등의 콘텍스트 매니저 헬퍼는 SQLAlchemy의 `engine.raw_connection()`을 활용하도록 파사드(Facade) 패턴 형태로 리팩토링했습니다.
* **[MODIFY] [main.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/main.py)**
  * 애플리케이션 라이프사이클 기동 시 데이터베이스 풀 초기화 및 PostgreSQL 접속 세팅 단계를 재설정하고 정적 디렉토리 마운트 처리를 강화했습니다.
* **[DELETE] [kafka_log_consumer.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/services/kafka_log_consumer.py) & [kafka_metric_consumer.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/services/kafka_metric_consumer.py)**
  * 더 이상 Kafka 토픽을 구독하지 않으므로 백그라운드 스레드로 돌던 모든 컨슈머 관련 클래스 파일을 완벽히 삭제했습니다.

### D. 웹 백엔드 라우터 및 서비스
* **[MODIFY] [search_service.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/services/search_service.py)**
  * 네이버 최저가 비교 API 연동 로직 리팩토링 및 가격 정보가 없는 브랜드 상품(자라 등) 조회 시 필수 필드인 `mall_name`, `mall_url`이 `None`으로 전달되어 직렬화 에러를 유발하던 구조를 방어 코드(`Fallback` 및 `origin_url` 사용)로 보강했습니다.
* **[MODIFY] [auth.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/auth.py)**
  * 로그인 인증 관련 로직을 개선하고, 관리자 로그인 시 쿠키 보안 옵션을 적절히 세팅했습니다.
  * 로그인 요청 시 데이터베이스 세션 외래키 제약조건(`user_sessions_user_id_fkey`)을 통과할 수 있도록 `users` 테이블에 어드민 시스템 유저 레코드의 자동 생성(`ON CONFLICT DO NOTHING`)을 보장했습니다.
  * 로컬 HTTP 개발 환경에서 크롬 브라우저가 관리자 쿠키를 정상 저장할 수 있도록 SameSite 정책을 `lax`로, Secure 속성을 `False`로 완화했습니다.
* **[MODIFY] [inquiry.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/inquiry.py)**
  * 기존 하나의 세션 헬퍼(`_get_session`)가 무조건 어드민 토큰을 우선 조회하여 일반 사용자의 문의 목록 페이지까지 `admin` 세션으로 오염시키던 심각한 문제를 해결하기 위해, 유저용(`_get_user_session`)과 어드민용(`_get_admin_session`) 세션 헬퍼를 완벽하게 분리했습니다.
* **[MODIFY] [product.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/product.py) & [search.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/search.py)**
  * 사용자 검색 시 `search_logs` 및 `search_results`를 DB에 실시간 동기 방식으로 안전하게 삽입하는 구조를 복구 및 검증했습니다.
* **[MODIFY] [admin.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/admin.py)**
  * 어드민 인프라 상태 모니터링 API에서 Docker API 및 Kafka 지표 조회 코드를 걷어내고, PostgreSQL 테이블 통계 데이터 요약 중심으로 리팩토링했습니다.

### E. 웹 프론트엔드 정적 파일 & 템플릿
* **[MODIFY] [common.js](file:///home/ubuntu/lookalike-lightweight/web/frontend/static/js/common.js)**
  * 좋아요(Likes)와 최근 본 상품(Recent Views) 렌더링 이벤트 리스너가 중복 실행되던 경로 매핑 오류를 고치고, 0건 조회 시 빈 공간(`emptyState`) 안내 창이 깔끔하게 표시되도록 보완했습니다.
* **[MODIFY] [product_detail.html](file:///home/ubuntu/lookalike-lightweight/web/frontend/templates/product_detail.html)**
  * 상품 상세 화면에서 쿠키를 백엔드로 보낼 때 `credentials: 'include'` 방식을 명시하여, 로그인 쿠키 누락으로 좋아요 및 조회수 반영이 안 되던 이슈를 수정했습니다.
* **[MODIFY] [base.html, search_history.html 등 템플릿 파일들](file:///home/ubuntu/lookalike-lightweight/web/frontend/templates)**
  * 브라우저가 변경된 자바스크립트 소스를 강하게 캐싱하여 화면이 갱신되지 않는 문제를 강제 무효화하기 위해, 스크립트 로드 경로 뒤에 난수 기반 캐시 버스터(`?v={{ range(1, 999999) | random }}`) 파라미터를 추가했습니다.

### F. 방문자 분석 대시보드 및 공식몰 연동
* **[MODIFY] [admin.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/admin.py)**
  * IP 기반의 유저 기기(OS/Browser) 경량 분석 기능과 South Korea 가상 지오 해시 매핑(`_get_ip_geo`) 기능을 추가하고, 이중화 통계(전체/일반/관리자) 처리 및 KST 시간 직렬화 API를 탑재했습니다.
* **[MODIFY] [pages.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/pages.py)**
  * `127.0.0.1`, `::1` 등 로컬 루프백 접속 제한 필터를 완전히 제거하여 로컬 환경에서도 관리자 IP 정보가 자동/수동 등록 및 식별되도록 개선했습니다.
* **[MODIFY] [product.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/product.py)**
  * 상세조회 시 데이터 누락으로 인해 공식몰 가격 클릭 시 페이지가 새로고침만 되며 외부 페이지로 이동하지 않던 문제를 해결하기 위해 `origin_url` 필드를 복구했습니다.
* **[MODIFY] [admin_visitors.html](file:///home/ubuntu/lookalike-lightweight/web/frontend/templates/admin_visitors.html)**
  * 분석 기간의 `[직접 입력]`을 지원하기 위한 Date Picker 연동 및 동적 쿼리 전달 인터페이스를 추가하고, 컴포넌트 짤림 현상을 극복하기 위해 UI 레이아웃 CSS(155px 확장)를 정교화했습니다.

---


## 3. 3대 핵심 리뉴얼 아키텍처 요약 (Core Pillars)

이전 버전의 아키텍처 마이그레이션 이후 추가된 고도화 및 환경 최적화 설계의 중심축은 다음과 같습니다.

### Pillar 1. 완벽하게 격리된 이중화 개발/상용 환경 (DEV vs PROD)
* **물리적 스토리지 격리**: 클라우디너리(Cloudinary) 서버의 최상위 디렉터리 경로를 `DEV/`와 `PROD/`로 완전히 격리 분기하여 개발 테스트가 실제 상용 서비스의 이미지 데이터를 오염시키지 않도록 원천 가두었습니다.
* **샌드박스 검증**: 스위칭(Swap) 전 데이터가 정상적으로 조립되는지 독립적으로 테스트하기 위해 운영 DB와 구조가 100% 동일한 격리형 `TEST DB` 세트를 갖추었습니다.

### Pillar 2. 역할 및 용량 분담형 멀티 DB 설계 (DB vs DW DB)
* **메인 서비스 DB**: `products`, `naver_prices`, `product_embeddings` 등 검색 매칭 실 서비스 노출 테이블만 보관하여 네온 DB의 0.5GB 무료 용량을 방어하고 쿼리 속도를 최대로 확보합니다.
* **DW(데이터웨어하우스) DB**: 크롤러가 수집 격리한 임시 상품 목록(`staging_`), 실시간 생성 부하 방지를 위해 수집 단계에서 선(Pre) 연산한 CLIP 이미지/텍스트 임베딩 정보, 그리고 시스템 장애 로그 및 메트릭 기록을 전담하도록 나누어 물리적 분산 배치를 완료했습니다.

### Pillar 3. 실시간 리소스 동적 감지 및 클라우드 실측 관제
* **cgroups 기반 감지**: 512MB RAM, 1vCPU가 할당된 클라우드 컨테이너의 물리 사양을 하드코딩 마스킹하지 않고 cgroups 커널 시스템 파일을 읽어 동적으로 점유율을 추적합니다.
* **네온 DB 이중 용량 감지**: 어드민 대시보드 화면 내에서 DEV DB 세트와 PROD DB 세트의 상세 크기 및 합계 용량을 직관적으로 분리 모니터링할 수 있도록 렌더링을 고도화했습니다.

---

## 4. HuggingFace Space 운영 구조

### 4-1. 파일 역할 및 배포 방법

`ml-models/backup/` 폴더에는 두 개의 파일이 있습니다.

| 파일 | 역할 |
|------|------|
| `app.py` | **로컬 편집용 원본** — 이 파일을 항상 먼저 수정합니다 |
| `hf_space_app.py` | `app.py`의 **이름 변경 복사본** — 나중에 보고 "HF Space에 올라가는 파일이구나" 바로 알 수 있도록 이름을 구분해 둔 것입니다 |

> **배포 흐름**
> 1. `app.py` 수정
> 2. `Copy-Item app.py hf_space_app.py` 로 동기화
> 3. `app.py`를 HuggingFace Space 레포지토리의 `app.py`로 복사 후 커밋/푸시

두 파일을 모두 유지하는 이유는, `hf_space_app.py`라는 이름만 봐도 "이게 HF Space에 올라가는 코드"임을 나중에 폴더를 봤을 때 바로 알 수 있기 때문입니다.

---

### 4-2. HF Space 콜드 스타트(Cold Start) 문제

허깅페이스 스페이스(HuggingFace Space) 무료 플랜은 **15분 동안 요청이 없으면 절전 상태**에 들어갑니다.
절전 해제 시 YOLO + Fashion-CLIP 모델을 다시 메모리에 올리는 데 **30초 ~ 1분**이 걸립니다.

```
최초 페이지 방문
       ↓
  [HF Space 절전 중]
       ↓ 30~60초 소요
  [모델 로딩: YOLO + Fashion-CLIP]
       ↓ 2~5초 소요
  [탐지 결과 반환]
```

이미 깨어 있는(웜) 상태라면:
```
이미지 업로드
       ↓ 2~5초
  [탐지 결과 반환]
```

---

### 4-3. Keep-Alive 백그라운드 태스크 구조

#### ❌ 기존 방식 (cron-job.org) — 효과 없음

```
cron-job.org
    │
    │  GET https://daniel0708-lookalike-yolo.hf.space/
    ▼
 [HTML 페이지 반환] ← 모델은 깨어나지 않음!
```

HF Space 웹 URL(`/`)에 단순 GET 요청을 보내면 **HTML 페이지만 반환**하고, YOLO·CLIP 모델은 전혀 실행되지 않습니다. 따라서 절전이 해제되지 않습니다.

#### ✅ 새로운 방식 (백엔드 Keep-Alive 태스크) — 실제 모델 호출

```
FastAPI 서버 기동 완료
       │
       │ (60초 대기 후 시작)
       ▼
┌─────────────────────────────────────────────────┐
│           _hf_space_keepalive_loop()            │
│                                                 │
│  1x1 흰색 더미 PNG 이미지 생성                   │
│       │                                         │
│       │  Gradio /predict API 호출               │
│       ▼                                         │
│  [HF Space: YOLO + CLIP 실제 추론]  ← 모델 깨움! │
│       │                                         │
│       │  응답 수신 후 9분 대기                   │
│       ▼                                         │
│  (반복 → 15분 제한 이전에 계속 호출)              │
└─────────────────────────────────────────────────┘
```

**핵심 차이**: 실제 추론 API(`/predict`)를 호출해야만 모델이 메모리에 유지됩니다.

#### 구현 위치

```
web/backend/app/main.py
└── lifespan()
    ├── start_metric_collector()        ← 인프라 메트릭 수집
    └── _hf_space_keepalive_loop()      ← HF Space Keep-Alive (9분 간격)
```

#### 동작 타이밍

```
서버 기동
  │
  │ +60초  첫 번째 더미 predict 호출
  │
  │ +9분   두 번째 더미 predict 호출
  │
  │ +18분  세 번째 더미 predict 호출
  │
  ... (계속 반복, HF Space는 항상 웜 상태 유지)
```

> **기존 cron-job.org 설정은 삭제해도 됩니다.**
> 이제 백엔드 서버가 직접 Keep-Alive를 담당하므로 cron-job이 불필요합니다.

---

## 5. YOLO 박스 후처리 파이프라인

이미지 업로드 → YOLO 탐지 → 후처리 → 프론트엔드 표시 흐름:

```
[이미지 업로드]
      │
      ▼
[YOLO 탐지] → raw_boxes (신뢰도 0.25 이상 전부)
      │
      ▼ 1단계: 노이즈 제거
      │  · 신뢰도 < 0.35 → 제거
      │  · 면적 < 이미지의 4% → 제거
      │
      ▼ 2단계: 동일 카테고리 Union 병합
      │  · Bottom이 2개 → 두 박스의 합집합(큰 박스)으로 합침
      │  · Top이 2개 → 마찬가지로 합침
      │
      ▼ 3단계: 포함 관계 필터
      │  · Top 박스가 Outer 박스 안에 75% 이상 포함됨 → Top 제거
      │  · (Outer를 입었을 때 안의 상의가 중복 표시되는 문제 해결)
      │
      ▼
[프론트엔드 박스 표시]
  · Outer 박스: 아우터 전체
  · Bottom 박스: YOLO가 탐지한 모든 하의 영역의 합집합(Union)
```

> **핵심 포인트**: 1단계에서 Bottom은 더 낮은 신뢰도 임계값(0.20)을 적용합니다.
> 오른쪽 다리처럼 가려진 부분은 YOLO가 낮은 신뢰도로 탐지하므로,
> 0.35 기준이면 걸러지지만 0.20 기준으로는 살아남아 Union에 포함됩니다.

---

## 6. 핵심 수정 파일 요약 표 (Change Registry)

| 컴포넌트 | 파일 경로 | 변경 구분 | 핵심 사유 및 내용 |
| :--- | :--- | :---: | :--- |
| **Backend** | `web/backend/app/routers/auth.py` | `MODIFY` | `/admin/login` 엔드포인트 신설, 로컬용 세션 쿠키 정책(`Lax`, `Secure=False`) 조정 |
| **Backend** | `web/backend/app/routers/inquiry.py` | `MODIFY` | 세션 하이재킹 방지를 위해 일반/어드민 세션 인증 헬퍼 전면 격리 및 분리 |
| **Backend** | `web/backend/app/routers/search.py` | `MODIFY` | 네이버 최저가 검색 예외 복구 및 검색 기록 DB 저장 복구 |
| **Backend** | `web/backend/app/services/search_service.py` | `MODIFY` | 최저가 미기재 상품 파싱 시 다운되는 버그(Serializer Null error) 방어 로직 추가 |
| **Backend** | `web/backend/app/database.py` | `MODIFY` | psycopg2 풀 대신 SQLAlchemy 엔진으로 연동 전환 및 SSL 접속(sslmode=require) 강제화 |
| **Backend** | `web/backend/app/services/kafka_*_consumer.py` | `DELETE` | 불필요한 백그라운드 Kafka 로그 및 지표 컨슈머 코드 제거 |
| **Frontend** | `web/frontend/static/js/common.js` | `MODIFY` | 좋아요 및 최근 본 상품의 렌더링 영역이 상호 오염 및 혼선되던 버그 분기 수정 |
| **Frontend** | `web/frontend/templates/base.html` | `MODIFY` | 템플릿 스크립트 캐싱 방지용 난수 캐시 버스터(`Cache Buster`) 추가 |
| **Frontend** | `web/frontend/static/js/admin_infra.js` | `MODIFY` | 클라우디너리 API 마이너스 지표 응답 시 Math.max(0, ...) 보정 예외 필터 장치 주입 |
| **Scripts** | `scripts/insert_DB/init_dev_db.py` | `NEW` | 신규 개발용 네온 데이터베이스 DDL 스키마 및 pgvector 생성 스크립트 구축 |
| **Scripts** | `scripts/insert_DB/init_dw_db.py` | `NEW` | 분산 배치 처리를 위한 데이터웨어하우스 DB 구조화 자동 생성 스크립트 구축 |
| **Scripts** | `scripts/supabase/` | `DELETE` | 루트의 supabase/migrations 와 꼬여있던 중복 레거시 마이그레이션 폴더 영구 삭제 |
| **Scripts** | `scripts/start_all.sh 등 쉘` | `DELETE` | 로컬 DB 초기화용 중복 파일 및 로컬 컨테이너 제어 쉘 파일 6종 영구 삭제 |
| **Backend** | `web/backend/app/routers/admin.py` | `MODIFY` | 이중화 방문자 세션 분석, 로컬 지오 해시 매핑, KST 시간대 직렬화 API 탑재 |
| **Backend** | `web/backend/app/routers/pages.py` | `MODIFY` | 로컬 루프백 IP 제한 해제 및 자동 어드민 IP 등록 기능 고도화 |
| **Backend** | `web/backend/app/routers/product.py` | `MODIFY` | 공식몰 아웃링크 이동 에러 해결을 위해 `origin_url` 맵핑 복구 |
| **Frontend** | `web/frontend/templates/admin_visitors.html` | `MODIFY` | 기간 직접 입력 Date Picker 컴포넌트 추가 및 짤림 방지 UI 수정 |
| **Docs** | `docs/renewal/admin_visitors_renewal.md` | `NEW` | 방문자 분석 대시보드 리뉴얼 아키텍처 및 상세 사양서 작성 |
| **Backend** | `web/backend/app/routers/admin_error.py` | `NEW` | 에러 상품 상세 조회, 벌크 조치 및 단일 상품 핀포인트 복구 API 라우터 신설 |
| **Frontend** | `web/frontend/templates/admin_crawling.html` | `MODIFY` | "에러 상품 통제 & 복구" 탭 및 행별 핀포인트 재수집 단추와 벌크 액션 JS 핸들러 탑재 |
| **Docs** | `docs/renewal/admin_users_security_renewal.md` | `NEW` | 관리자 페이지 단위 체크박스 세부 권한(RBAC) 및 유저 비밀번호 보안 찾기 사양서 작성 |
| **Frontend** | `web/frontend/templates/admin_users.html` | `NEW` | 독립된 관리자 및 유저 모니터링 관리 전용 페이지 신설 및 권한 체크박스 모달 연동 |
| **Frontend** | `web/frontend/templates/forgot_password.html` | `NEW` | 보안 질문/답변 기반 1회용 임시 패스워드 즉시 노출 발급 신설 화면 |

---

## 7. 리팩토링 및 최적화 이력 (1차 ~ 11차 고도화)

* **1차 리팩토링: Actions 기반 수집 분리 (2026-05-24)**
  * 렌더(Render) 무료 서버의 CPU/메모리 부하 경감을 위해 대규모 크롤링 연산을 깃허브 액션(GitHub Actions) 환경으로 이관했습니다.
  * 명령줄 인터페이스(CLI) 엔트리포인트(`crawling_pipeline_cli.py`)를 개발하고, 수집 데이터를 임시 스테이징 테이블에 1차 격리 저장하는 구조를 설계했습니다.
* **2차 최적화: HDFS 의존성 제거 및 수집 캐시(CDC) 도입 (2026-05-28)**
  * 레거시 하둡(Hadoop) 및 HDFS 연동 모듈을 완전히 제거하고 PostgreSQL 네온 DB 구조로 통합했습니다.
  * 크롤링 시 이전 수집 캐시를 대조하여, 가격이나 이미지에 변경이 없는 상품의 경우 클라우디너리 업로드를 건너뛰는 변경 감지(CDC) 로직을 적용해 API 비용과 실행 시간을 절감했습니다.
* **3차 리팩토링: 물리 DB 이중화 및 한국 표준시(KST) 동기화 (2026-05-30)**
  * 네온 DB의 무료 플랜 용량(0.5GB) 한계를 준수하고 실시간 성능을 보장하기 위해, 메인 프로덕션 DB와 데이터웨어하우스 DB(DW DB)로 데이터베이스 커넥션을 물리적으로 분리했습니다.
  * 모든 데이터베이스 커넥션 세션 연결 시 `SET TIME ZONE 'Asia/Seoul';`을 실행하여 시계열 데이터가 한국 표준시(KST) 기준으로 저장되도록 동기화했습니다.
* **4차 최적화: 어드민 런타임 오류 복구 (2026-06-04)**
  * 어드민 API 서버가 `data-pipeline` 내의 `base_utils.py` 모듈을 참조할 때, 실행 디렉터리 경로에 무관하게 작동하도록 절대 경로 산출법으로 전면 수정했습니다.
  * 실행 가상환경(`ml-env`) 상의 누락 패키지(`aiohttp`)를 추가하고, 임시 테스트 이관 대기 시간을 기존 스펙인 24시간(`hours_elapsed < 24`)으로 원복 조치했습니다.
* **5차 리팩토링: YOLOv11 + Fashion-CLIP 사전 임베딩 추출 설계 (2026-06-05)**
  * 데이터 이관 시점에 대량의 벡터 연산을 수행해 발생하는 병목을 차단하기 위해, **Phase 1 (수집)** 단계에서 상품 수집 직후 YOLOv11 추론 서버(HuggingFace Space)를 거쳐 의류 영역을 사전 크롭(Pre-Cropping)하고, 512차원 패션 클립(Fashion-CLIP) 이미지 임베딩과 텍스트 임베딩을 선 생성하여 DW DB의 `staging_product_embeddings`에 사전 보존하도록 고도화했습니다.
  * **Phase 2 (이관/스위칭)** 단계에서는 이미 계산 완료된 벡터 데이터를 대상 운영 DB로 단순 복사(`INSERT INTO ... SELECT`)하여 트랜잭션 처리 시간을 1초 내외로 단축했습니다.
* **6차 최적화: 환경 변수 정돈 및 마이그레이션(Migrations) 설계서 단일화 (2026-06-08)**
  * `.env` 파일 내 환경변수의 등호 전후 공백을 제거하여 런타임 에러를 방지하고, 미사용 레거시 컨테이너 변수(MongoDB, Redis, Airflow, Hadoop 등)를 주석 처리 및 정리했습니다.
  * 중복으로 존재하던 `scripts/supabase/migrations` 디렉터리를 완전 삭제하고, 스키마 관리 일관성을 위해 루트의 `supabase/migrations/`로 단일화했습니다.
  * HTML 복구용 임시 스크립트 및 쉘 파일 6종을 일괄 영구 삭제했습니다.
  * 클라우디너리 API 수치 전달 지연 시 마이너스 값이 노출되던 모니터링 화면 오류를 `Math.max(0, ...)` 보정 코드를 통해 개선했습니다.
* **7차 리팩토링: 모니터 대시보드 리뉴얼 및 긴급 단일 재수집 기능 추가 (2026-06-16)**
  * 자동 크롤링 모니터링 탭을 읽기 전용 대시보드로 개편하고, 실시간 스캔 결과 총량 대비 수집 현황 게이지바를 연동했습니다.
  * 에러 로그 클릭 시 상세한 예외 원인을 확인할 수 있는 '에러 정밀 진단 모달'을 추가했습니다.
  * 실패한 특정 상품 1건만 단독 타겟팅하여 가상환경(`ml-env`)에서 즉각 백그라운드로 수집을 재시도하는 연동 기능을 탑재했습니다.
* **8차 최적화: 스파오 상세 수집 개선 및 임베딩 3중 안전망 구축 (2026-06-24)**
  * **스파오 상세 수집 실패 차단**: 상세 페이지 URL 구성 시 `itemNo`뿐만 아니라 `lowerVendNo`를 함께 추출하도록 크롤러 파싱 로직을 고도화하여 리다이렉트로 인한 수집 스킵 버그를 제거했습니다.
  * **네트워크 3중 재시도 로직**: 일시적 DNS 실패나 Gradio API 콜드 스타트 지연 등에 대응하기 위해 이미지 다운로드 **3회 재시도** 및 Gradio API **2회 호출 재시도**를 구현하여 에러율을 최소화했습니다.
  * **로컬 CLIP 모델 이미지 임베딩 폴백(Fallback)**: API 서버 장애나 YOLOv11 크롭 실패 시 `image_vector`가 `NULL`로 수집되는 현상을 막기 위해, 실패 발생 시 자동으로 로컬 캐시 모델(`openai/clip-vit-base-patch32`)을 가동하여 **원본 이미지 전체에 대해 로컬에서 직접 512차원 임베딩을 생성**해 적재하는 자동 방어막을 구축했습니다.
  * **브랜드 구성 최신화**: 기존의 무신사, 자라 브랜드를 수집 대상에서 제외하고, 에잇세컨즈, 탑텐, 유니클로, 스파오, 지오다노, 폴햄의 6대 브랜드 체제로 전면 리뉴얼했습니다. 어드민 페이지에 강제 정지, 캐시 무시, 강제 이관 등의 관리 제어 기능을 추가했습니다.
* **9차 고도화: 방문자 분석 대시보드 리뉴얼, 관리자 IP 식별 체계 개편 및 Neon 리소스 한도 모니터링 (2026-06-26)**
  * **이중화 통계 격리**: 대시보드 내 방문 이력 조회 시 서비스 일반 유입 트래픽과 어드민(OWNER) 테스트 트래픽을 분류(전체/일반/관리자)하여 각각 격리된 통계를 실시간 제공합니다.
  * **로컬 IP 필터 해제 및 하드코딩 제거**: 정적 통신망 IP(`220.116.*.*`) 하드코딩 조건을 전면 폐기하고 DB 조인 방식으로 통일했습니다. 또한 로컬 루프백(`127.0.0.1`, `::1`) 제한 필터를 해제하여 개발 접속 시에도 어드민으로 안전하게 기록 및 식별되도록 개선했습니다.
  * **KST 및 시계열 날짜 정밀도 확보**: 로그 시간대 오프셋(+09:00)을 보장하여 날짜와 시각이 `YYYY-MM-DD HH:mm:ss` 전체 형식으로 직렬화되어 반환되도록 하였습니다.
  * **기간 직접 지정 범위 조회**: `[직접 입력]` 옵션을 추가하여 Date Picker로 시작일/종료일 지정 조회가 가능하도록 하였으며, 셀렉트박스 글자 짤림 현상 방지를 위해 UI 넓이를 155px로 조절했습니다.
  * **공식몰 아웃링크 복구**: 상품 상세조회에서 공식몰 가격 클릭 시 페이지가 무한 새로고침되던 현상을 `ProductResponse` 및 조회 SQL 내 `origin_url` 필드를 복구하여 정상 연결되도록 해결했습니다.
  * **Neon Compute/Network 리소스 한도 모니터링 & HuggingFace Space 실시간 자원 연동**: 어드민 인프라 페이지에서 Neon DB의 월별 누적 Compute 시간(CU-hours)과 네트워크 전송량(Network transfer)을 실시간으로 집계해 게이지바 및 사용률 텍스트로 표시하고, 개별 DB 프로젝트 단위의 한도(100 CU-hours, 5 GB)에 도달하거나 초과할 경우 '리밋 도달'이라는 붉은색 경고 뱃지가 실시간 활성화되도록 모니터링을 고도화했습니다. (용량은 총합계로 관리하지만 Compute/Network는 각각 개별 DB 단위 리밋이 존재하므로 합계 행에는 게이지바를 표시하지 않고 '-' 단순 텍스트 처리 및 각 DB 단위의 개별 임계치 초과 여부 감지 로직 적용. 또한 모바일 반응형 찌러짐 방지를 위해 progress bar 숨김 및 text-nowrap 레이아웃 보완 처리 완료). 추가로, AI 추론용 HuggingFace Space 가상 머신의 실시간 CPU 사용률(%), 실시간 RAM 사용량(MB/GB) 정보 수집용 SSE Metrics 파싱 API와 Space의 빌드/런타임 상태(RUNNING, BUILDING 등 stage)를 백엔드에서 동적으로 추가 연동하여 대시보드 UI에 실시간 바인딩 완료했습니다.
  * **상세 가이드 연동**: 신규 상세 아키텍처 사양은 [admin_visitors_renewal.md](file:///d:/dev/lookalike-lightweight/docs/renewal/admin_visitors_renewal.md) 파일에 정리했습니다.
* **10차 고도화: 통합 에러 상품 통제 및 핀포인트 복구 대시보드 구축 (2026-06-30)**
  * **4대 치명 에러 격리 배제**: 스위칭 이관 시 필수 메타 유실, 네이버 가격 전무, 임베딩 누락, 이미지 URL 깨짐이 발생한 불량 상품들을 자동 이관 대상에서 배제하여 Staging 테이블에 잔류하도록 처리했습니다.
  * **핀포인트 1초 즉시 복구 API**: 에러 상품 목록에서 `[재수집]` 버튼을 누르면 실시간으로 네이버 쇼핑 openAPI 5대 최저가를 재수집하고 CLIP 임베딩을 보정하여 즉각 오류를 소거하는 백엔드 API를 신설했습니다.
  * **벌크 복구 액션 및 UI 연동**: 에러 상품들을 다중 선택하여 일괄 강제 이관(`partial_switch`), 벌크 최저가 재수집(`re_crawl`), 임베딩 재생성(`re_embed`), 또는 스테이징 비우기(`delete_staging`)를 처리하는 통합 관리자 제어 탭을 구축했습니다.
* **11차 고도화: 어드민 세부 권한(RBAC) 제어 및 유저 비밀번호 보안 찾기 개편 (2026-07-01)**
  * **PostgreSQL SELECT DISTINCT 에러 핫픽스**: `SELECT DISTINCT`와 `ORDER BY` 표현식 불일치 문제 해결을 위해 `GROUP BY`와 `MAX(create_dt)` 기반의 표준 쿼리문으로 전면 보완하였습니다.
  * **페이지 단위 다중 권한(RBAC) 설정**: 최고 관리자(SUPER_ADMIN)의 고유 권한으로 하위 어드민의 페이지(인프라/크롤링/로그/방문자/문의)별 접근 권한을 체크박스 모달을 통해 맞춤 설정할 수 있도록 구조 개편했습니다. 허용되지 않은 메뉴는 사이드바에서 자동 숨김 처리 및 경로 차단 미들웨어를 연동했습니다.
  * **임시 비밀번호 & 강제 패스워드 변경**: 이메일 가입 유저가 비밀번호 분실 시 본인 확인용 보안 질문/답변 매칭을 거쳐 12자리 임시 패스워드를 화면에 즉시 노출 발급해 주는 전용 독립 뷰 페이지(`forgot_password.html`)를 제작했습니다. 발급된 임시 번호로 최초 로그인 성공 시, 강제로 새 암호를 설정하기 전까지 다른 화면 접근을 차단하는 격리 팝업 모달을 연동했습니다.
  * **상세 가이드 연동**: 신규 상세 아키텍처 사양은 [admin_users_security_renewal.md](file:///d:/dev/lookalike-lightweight/docs/renewal/admin_users_security_renewal.md) 파일에 정리했습니다.
* **12차 고도화: 프론트엔드 리소스 삼단 분리 및 동일 파일명 1:1 매핑 표준화 (2026-07-03)**
  * **HTML/CSS/JS 삼단 완전 모듈화**: 주요 8개 활성 화면의 인라인 스타일과 스크립트 코드를 모두 걷어내고, 동일한 베이스 파일명을 지닌 외부 독립 CSS/JS 정적 파일 구조로 정돈하였습니다.
  * **Jinja2 템플릿 변수 캡슐화**: 외부 스크립트의 Jinja2 문법 에러를 회피하기 위하여 HTML 루트 컨테이너에 `data-*` 속성으로 데이터 바인딩 우회 아키텍처를 도입했습니다.
* **13차 최적화: 4세대 절전 친화적(Zero-Compute Idle) 아키텍처 및 Neon DW 자동 페일오버 (2026-08-22)**
  * **백그라운드 DB 쿼리 제거 및 인메모리 링 버퍼 전환**: 5분 주기 메트릭 수집 및 실시간 에러 로깅으로 인해 Neon DB가 24시간 깨어있던 문제를 해결하기 위해, 메트릭을 `collections.deque(maxlen=12)` 메모리에만 저장하고 DB Write를 전면 차단하여 유휴 시간대 0 CU(절전)를 달성했습니다.
  * **어드민 온디맨드 헬스체크**: 10분 주기 상시 백그라운드 DB 호출 루프를 제거하고, 관리자가 어드민에 접속할 때만 On-Demand로 상태를 조회하며 10분 인메모리 TTL 캐시를 적용했습니다.
  * **DW DB 듀얼 계정 및 자동 페일오버**: `PROD_DW_DATABASE_URL`이 한도에 도달(Compute/Network 90% 이상)하거나 차단/연결 실패 시, 설정된 보조 DW DB(`PROD_DW_DATABASE_URL_2`)로 런타임에 무중단 자동 스위칭되도록 구현했습니다.
  * **상세 가이드 연동**: 상세한 장애 분석 및 아키텍처 사양은 [neon_compute_optimization.md](file:///home/daniel/dev/lookalike-lightweight/docs/renewal/neon_compute_optimization.md) 에 정리했습니다.



