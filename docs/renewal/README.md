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
   * AI/ML 모델 서빙을 위해 HuggingFace of Space(VLM, YOLO, CLIP) 및 Gemini API를 연동하여 이미지 특징 추출 및 텍스트 쿼리 임베딩을 수행하고, Cosine Similarity 계산을 SQL 레벨에서 처리합니다.
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
  * 레거시 라이브러리(elasticsearch, pymongo, kafka-python) 패키지를 제거하고, PostgreSQL 연동에 필요한 `psycopg2-binary`, Pydantic v2 관련 호환 라이브러리, DB 표준 연동을 위한 `sqlalchemy`, 그리고 임베딩 처리를 위한 `google-generativeai` 패키지를 추가했습니다.
* **[MODIFY] [scripts 폴더 내 실행 쉘 스크립트](file:///home/ubuntu/lookalike-lightweight/scripts)**
  * 대상 파일: `start_all.sh`, `stop_all.sh`, `restart_all.sh`
  * 분산 미들웨어 컨테이너 모니터링 단계를 스킵하고 경량 단일 서버 구동 중심으로 스크립트를 재정비했습니다.

### C. 웹 백엔드 설정 & 데이터베이스
* **[MODIFY] [base.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/config/base.py)**
  * 어드민 보안 접속용 계정 설정 정보(`ADMIN_USERNAME`, `ADMIN_PASSWORD`)를 환경변수 로더(`Settings`)에 통합 매핑하여 안정적인 로그인 검증 기반을 마련했습니다.
  * `DATABASE_URL`을 Pydantic 필드로 등록하고 `@model_validator(mode="after")`를 활용한 동적 폴백 처리를 적용하여, 로컬/도커 디버그 모드와 외부 프로덕션 모드 설정이 포트 충돌 없이 깔끔하게 분기되도록 개선했습니다.
* **[MODIFY] [database.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/database.py)**
  * 기존 `psycopg2` 전용 `ThreadedConnectionPool` 기반의 로직을 **SQLAlchemy 엔진 및 Connection Pool** 구동으로 전환했습니다.
  * Render 프로덕션 환경 등 외부 DB 접속 시 안전한 암호화 채널을 의무적으로 생성할 수 있도록, 원격지 접속 판별 시 `sslmode=require` 옵션을 커넥션 인자로 자동 주입하는 보안 가드레일을 설치했습니다.
  * 기존에 작성된 개별 라우터/서비스들의 소스코드 정합성을 해치지 않기 위해 `get_pg_connection()` 및 `get_pg_cursor()` 등의 콘텍스트 매니저 헬퍼는 SQLAlchemy의 `engine.raw_connection()`을 활용하도록 파사드(Facade) 패턴 형태로 리팩토링했습니다.
* **[MODIFY] [main.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/main.py)**
  * 애플리케이션 라이프사이클 기동 시 데이터베이스 풀 초기화 및 PostgreSQL 접속 세팅 단계를 재설정하고 정적 디렉토리 마운트 처리를 강화했습니다.
* **[DELETE] [kafka_log_consumer.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/services/kafka_log_consumer.py) & [kafka_metric_consumer.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/services/kafka_metric_consumer.py)**
  * 더 이상 Kafka 토픽을 구독하지 않으므로 백그라운드 스레드로 돌던 모든 컨슈머 관련 클래스 파일을 완벽히 삭제했습니다.

### D. 웹 백엔드 라우터 및 서비스
* **[MODIFY] [search_service.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/services/search_service.py)**
  * 네이버 최저가 비교 API 연동 로직 리팩토링 및 가격 정보가 없는 브랜드 상품(ZARA 등) 조회 시 필수 필드인 `mall_name`, `mall_url`이 `None`으로 전달되어 직렬화 에러를 유발하던 구조를 방어 코드(`Fallback` 및 `origin_url` 사용)로 보강했습니다.
* **[MODIFY] [auth.py](file:///home/ubuntu/lookalike-lightweight/web/backend/app/routers/auth.py)**
  * 누락되었던 관리자 보안 접속 API 엔드포인트(`POST /admin/login`, `POST /admin/logout`)를 추가했습니다.
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

---

## 3. 3대 핵심 리뉴얼 아키텍처 요약 (Core Pillars)

이전 버전의 아키텍처 마이그레이션 이후 추가된 고도화 및 환경 최적화 설계의 중심축은 다음과 같습니다.

### Pillar 1. 완벽하게 격리된 이중화 개발/상용 환경 (DEV vs PROD)
* **물리적 스토리지 격리**: Cloudinary 서버의 최상위 디렉터리 경로를 `DEV/`와 `PROD/`로 완전히 격리 분기하여 개발 테스트가 실제 상용 서비스의 이미지 데이터를 오염시키지 않도록 원천 가두었습니다.
* **샌드박스 검증**: 스위칭(Swap) 전 데이터가 정상적으로 조립되는지 독립적으로 테스트하기 위해 운영 DB와 구조가 100% 동일한 격리형 `TEST DB` 세트를 갖추었습니다.

### Pillar 2. 역할 및 용량 분담형 멀티 DB 설계 (DB vs DW DB)
* **메인 서비스 DB**: `products`, `naver_prices`, `product_embeddings` 등 검색 매칭 실 서비스 노출 테이블만 보관하여 Neon DB의 0.5GB 무료 용량을 방어하고 쿼리 속도를 최대로 확보합니다.
* **DW(데이터웨어하우스) DB**: 크롤러가 수집 격리한 임시 상품 목록(`staging_`), 실시간 생성 부하 방지를 위해 수집 단계에서 선(Pre) 연산한 CLIP 이미지/Gemini 텍스트 임베딩 정보, 그리고 시스템 장애 로그 및 메트릭 기록을 전담하도록 나누어 물리적 분산 배치를 완료했습니다.

### Pillar 3. 실시간 리소스 동적 감지 및 클라우드 실측 관제
* **cgroups 기반 감지**: 512MB RAM, 1vCPU가 할당된 클라우드 컨테이너의 물리 사양을 하드코딩 마스킹하지 않고 cgroups 커널 시스템 파일을 읽어 동적으로 점유율을 추적합니다.
* **Neon DB 이중 용량 감지**: 어드민 대시보드 화면 내에서 DEV DB 세트와 PROD DB 세트의 상세 크기 및 합계 용량을 직관적으로 분리 모니터링할 수 있도록 렌더링을 고도화했습니다.

---

## 4. 핵심 수정 파일 요약 표 (Change Registry)

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
| **Frontend** | `web/frontend/static/js/admin_infra.js` | `MODIFY` | Cloudinary API 마이너스 지표 응답 시 Math.max(0, ...) 보정 예외 필터 장치 주입 |
| **Scripts** | `scripts/insert_DB/init_dev_db.py` | `NEW` | 신규 개발용 Neon 데이터베이스 DDL 스키마 및 pgvector 생성 스크립트 구축 |
| **Scripts** | `scripts/insert_DB/init_dw_db.py` | `NEW` | 분산 배치 처리를 위한 데이터웨어하우스 DB 구조화 자동 생성 스크립트 구축 |
| **Scripts** | `scripts/supabase/` | `DELETE` | 루트의 supabase/migrations 와 꼬여있던 중복 레거시 마이그레이션 폴더 영구 삭제 |
| **Scripts** | `scripts/start_all.sh 등 쉘` | `DELETE` | 로컬 도커 컨테이너를 더 이상 띄울 필요가 없으므로 불필요한 레거시 쉘 파일 6종 영구 격하 |

---

## 5. 6차 리팩토링 및 3세대 격리 아키텍처 보강 (2026-06-02)

Neon DB 다중 계정 분리 및 로컬 런타임 최적화를 위해 프로젝트 전반에 남아있던 레거시 및 임시 파일들을 완전히 정리하고 환경을 3세대 경량 아키텍처에 맞게 고도화했습니다.

### A. Cloudinary 계정 및 데이터베이스 분기 정돈
* `DEV_CLOUDINARY_...` 환경 변수의 등호(`=`) 전후에 있던 공백을 제거하여 런타임 시 파싱 및 바인딩 오류가 발생할 수 있는 소지를 제거했습니다.
* 기존에 사용 중인 서버가 존재하지 않는 레거시 컨테이너용 변수(MongoDB, Redis, Jupyter, Airflow, Spark, Hadoop, Kafka, ES 통신용 변수) 및 GCP 가상머신 접속 주소(`GCP_PG_HOST` 등)를 주석(`#`) 처리하여 환경 변수 오염을 원천 차단했습니다.
* 이로써 `ENV_MODE` 분기(`local`/`dev` vs `production`)에 따라 알맞은 Cloudinary 및 Neon DB 주소가 자동으로 동적 바인딩되도록 정비했습니다.

### B. Neon 데이터베이스 뼈대 스키마(migrations) 정리 및 신규 DB 구성
* **신규 개발/용량 분담 DB 테이블 구축**:
  * 신규 개발용 데이터베이스(`DEV_DATABASE_URL`)의 테이블을 독립적으로 안전하게 초기화할 수 있도록 지원하는 전용 스크립트(`init_dev_db.py`, `init_dw_db.py`)를 작성하여 구동했습니다.
  * 이 스크립트를 통해 새로운 개발용 Neon DB에 `pgvector` 확장을 자동으로 로드하고 상품/임베딩/관리자 관련 테이블을 성공적으로 구축했습니다.
* **마이그레이션 도면 단일화**:
  * 프로젝트 내에 이중으로 저장되어 유지보수 혼선을 주던 `scripts/supabase/migrations` 디렉토리를 완전히 삭제하고, 데이터베이스의 근간 설계도인 `001_create_tables.sql`과 `002_admin_tables.sql`을 루트 경로의 `supabase/migrations/`로 이관하여 스키마 관리 일관성을 확보했습니다.

### C. 미사용 임시 스크립트 및 레거시 쉘 파일 정리
* 작업 중 HTML 복구를 위해 임시로 만들어 활용했던 `scratch/` 폴더 내 `restore_html.py` 스크립트를 삭제했습니다.
* Git 민감 정보 히스토리 치환용 임시 스크립트(`clean_git_history.sh`) 및 캐시 강제 갱신용 임시 파일(`.github_cache_refresh`)을 삭제했습니다.
* 로컬 DB 초기화용 중복 파일 `init_db.py`를 제거했습니다.
* 로컬 환경에서 더 이상 도커를 구동 및 제어할 필요가 없으므로 `scripts/` 바로 하위에 남아있던 `start_all.sh`, `stop_all.sh`, `restart_all.sh`, `init-db.sh`, `restore_prod.sh`, `check_status.sh` 등 쉘 스크립트 6개를 일괄 영구 삭제했습니다.

### D. 프론트엔드 모니터링 수치 보정
* Cloudinary API 캐시 지연 등으로 인해 일시적으로 전송량이나 리소스 개수가 음수(-) 값으로 반환될 경우, UI 대시보드 상에서 마이너스 수치가 노출되던 화면 오류를 발견하여 `Math.max(0, ...)` 보정 코드를 적용해 `0` 단위로 출력되도록 수정했습니다.

---

## 3. HuggingFace Space 운영 구조

### 3-1. 파일 역할 및 배포 방법

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

### 3-2. HF Space 콜드 스타트(Cold Start) 문제

HuggingFace Space 무료 플랜은 **15분 동안 요청이 없으면 절전 상태**에 들어갑니다.
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

### 3-3. Keep-Alive 백그라운드 태스크 구조

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

## 4. YOLO 박스 후처리 파이프라인

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

