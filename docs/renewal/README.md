# Lookalike 서비스 경량화 및 아키텍처 마이그레이션 변경사항 정리 (Renewal Summary)

본 문서는 Lookalike 서비스의 레거시 분산 환경 아키텍처(Elasticsearch, MongoDB, HDFS, Kafka 등)에서 **단일 서버 및 PostgreSQL/pgvector 중심의 경량 아키텍처**로 마이그레이션하면서 변경되거나 수정된 파일들과 그 상세 변경 사양을 정리합니다.

---

## 1. 주요 아키텍처 변경 요약 (Architectural Shifts)

1. **분산 레거시 미들웨어 제거**:
   - 대규모 인프라 의존성인 **Elasticsearch, MongoDB, Hadoop HDFS, Apache Kafka**를 완전히 제거했습니다.
   - 검색 로그, 파이프라인 지표, 상품 데이터 수집 이력을 모두 **PostgreSQL** 테이블로 단일화했습니다.
   - 백엔드의 백그라운드 Kafka 로그/메트릭 컨슈머 서비스(`kafka_log_consumer.py`, `kafka_metric_consumer.py`)를 삭제하고, FastAPI 라우터 진입점에서 PostgreSQL 직접 적재로 전환하여 인프라를 극적으로 경량화했습니다.
2. **벡터 유사도 검색 전환 (pgvector)**:
   - Elasticsearch Dense Vector 검색 방식을 **PostgreSQL pgvector 익스텐션 및 `product_embeddings` 테이블**을 활용하는 방식으로 마이그레이션했습니다.
   - AI/ML 모델 서빙을 위해 HuggingFace의 Space(VLM, YOLO, CLIP) 및 Gemini API를 연동하여 이미지 특징 추출 및 텍스트 쿼리 임베딩을 수행하고, Cosine Similarity 계산을 SQL 레벨에서 처리합니다.
3. **데이터 파이프라인(Airflow) 구조 단순화**:
   - 기존의 병렬 데이터 파이프라인 단계를 HDFS/MongoDB 적재 대신 **로컬 JSON 보존 + PostgreSQL 직접 삽입** 구조로 대폭 간소화했습니다.
4. **SQLAlchemy 기반 표준 데이터베이스 연동 & SSL 강화**:
   - 기존의 raw `psycopg2` 커넥션 풀 대신 **SQLAlchemy 엔진 및 세션 팩토리**로 전환하여 연결 안정성과 세션 관리를 현대화했습니다.
   - 클라우드 프로덕션 환경(Render 등)의 필수 요구사항인 SSL 보안 연동을 위해 원격지 접속 판별 시 `sslmode=require` 접속 인자가 자동으로 동적 주입되도록 풀 구성을 고도화했습니다.

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

## 3. 핵심 수정 파일 요약 표 (Change Registry)

| 컴포넌트 | 파일 경로 | 변경 구분 | 핵심 사유 및 내용 |
| :--- | :--- | :---: | :--- |
| **Airflow** | `fashion_total_pipeline.py` | `MODIFY` | HDFS/Kafka 오퍼레이터 배제 후 PostgreSQL 위주 수집 최적화 |
| **Docker** | `docker-compose.yml` | `MODIFY` | 레거시 분산 솔루션 컨테이너(Elasticsearch, MongoDB 등) 6개 서비스 영구 격하 |
| **Backend** | `web/backend/app/routers/auth.py` | `MODIFY` | `/admin/login` 엔드포인트 신설, 로컬용 세션 쿠키 정책(`Lax`, `Secure=False`) 조정 |
| **Backend** | `web/backend/app/routers/inquiry.py` | `MODIFY` | 세션 하이재킹 방지를 위해 일반/어드민 세션 인증 헬퍼 전면 격리 및 분리 |
| **Backend** | `web/backend/app/routers/search.py` | `MODIFY` | 네이버 최저가 검색 예외 복구 및 검색 기록 DB 저장 복구 |
| **Backend** | `web/backend/app/services/search_service.py` | `MODIFY` | 최저가 미기재 상품 파싱 시 다운되는 버그(Serializer Null error) 방어 로직 추가 |
| **Backend** | `web/backend/app/database.py` | `MODIFY` | psycopg2 풀 대신 SQLAlchemy 엔진으로 연동 전환 및 SSL 접속(sslmode=require) 강제화 |
| **Backend** | `web/backend/app/services/kafka_*_consumer.py` | `DELETE` | 불필요한 백그라운드 Kafka 로그 및 지표 컨슈머 코드 제거 |
| **Frontend** | `web/frontend/static/js/common.js` | `MODIFY` | 좋아요 및 최근 본 상품의 렌더링 영역이 상호 오염 및 혼선되던 버그 분기 수정 |
| **Frontend** | `web/frontend/templates/base.html` | `MODIFY` | 템플릿 스크립트 캐싱 방지용 난수 캐시 버스터(`Cache Buster`) 추가 |
| **Scripts** | `init_db.py` | `NEW` | Neon DB 및 원격지 데이터베이스용 DDL 스키마 초기화 자동화 스크립트 |
| **Scripts** | `migrate_to_neon.py` | `NEW` | 로컬 DB 상품/임베딩/최저가 등 12,000+건 데이터를 Neon DB로 고속 배치 마이그레이션 스크립트 |
