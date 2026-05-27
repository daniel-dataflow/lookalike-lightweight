# 3세대 경량화 로그 모니터링 시스템 아키텍처 및 워크플로우 명세서

> **문서 목적**: 본 문서는 Lookalike 프로젝트 아키텍처 경량화(Elasticsearch, Logstash, Kafka, Filebeat 제거) 작업에 맞추어 Neon PostgreSQL을 활용해 새롭게 설계된 **3세대 초경량 링 버퍼(Ring Buffer) 기반 로그 모니터링 시스템**의 구조, 데이터 흐름, 핵심 기술적 의사결정을 정리하여 기술함.

---

## 1. 3세대 경량화 로그 설계 배경 및 요구사항

기존의 분산 서버 환경에서 각 컨테이너(Airflow, Spark 등)의 대용량 로그를 관리하던 무거운 빅데이터 로깅 스택(Elasticsearch, Logstash, Kafka 등)을 전면 제거하고, Neon PostgreSQL 단일 데이터베이스 체계로 아키텍처를 경량화했습니다. 

이에 따라 로그 모니터링 역시 시스템 리소스 및 외부 저장소 트래픽을 최소화하며 동작하도록 아래의 세 가지 요구사항을 충족하도록 설계 및 보완되었습니다:

| 요구사항 속성 | 3세대 경량화 구현 방식 |
| :--- | :--- |
| **초경량성 (Low Footprint)** | 외부 로그 파이프라인 컴포넌트를 모두 제거하고, Python 표준 `logging` 라이브러리와 DB 직접 인서트를 활용 |
| **로그 링 버퍼 (Ring Buffer)** | Neon PostgreSQL 단일 테이블(`app_logs`)을 사용하여 최근 24시간 이내의 에러 로그만 유지하고 구형 데이터는 자동 청소 |
| **정확한 로컬 타임존 지원** | `TIMESTAMPTZ`를 데이터 타입으로 채택하여 데이터베이스 타임존(UTC)에 구애받지 않고 사용자 브라우저 타임존(KST)으로 자동 변환 |
| **불필요 노이즈 배제** | 서버 부담 및 저장 용량 한계를 지키기 위해 단순 `INFO` 로그는 수집에서 전면 제외하고 `WARN` 이상만 기록 |

---

## 2. 로그 모니터링 아키텍처 및 데이터 흐름 (Workflow)

```text
[백엔드 로깅 에러 감지]
  FastAPI 어플리케이션 구동 및 에러 발생 (예: /sentry-debug 500 에러)
     │
     ▼
  글로벌 예외 핸들러 (global_exception_handler)
     │ (미처리 예외를 잡아 명시적으로 logger.error() 수행)
     ▼
  NeonLogHandler (루트 로거 & Uvicorn/FastAPI 로거 전파)
     │ 
     ├─► 재귀 로깅 필터링 (sqlalchemy, psycopg2, app.database 로거 배제)
     ├─► 로그 본문 키워드 분석 후 서비스(PostgreSQL, Cloudinary, HuggingFace, FastAPI) 자동 매핑
     │
     ▼ [Neon PostgreSQL]
  app_logs 테이블 적재 (TIMESTAMPTZ를 통해 실시간 생성 시간 기록)
     │
     └─► 24시간 초과 구형 로그 즉시 파기 (DELETE FROM app_logs WHERE timestamp < NOW() - INTERVAL '24 hours')

[어드민 웹 화면 관제]
  브라우저 어드민 페이지 (admin_logs.js)
     ├─► GET /api/logs/dashboard (최근 1시간 통계, 24h 트렌드 차트, Top 5 에러, 서비스 헬스 통합 조회)
     ├─► GET /api/logs/stream (조건별 필터 필터링 및 100건 제한 스트리밍 조회)
     └─► GET /api/logs/download (현재 필터 조건으로 로그 텍스트 다운로드)
```

---

## 3. 주요 구성 요소 및 기술적 구현 세부사항

### 3.1 app_logs 테이블 및 TIMESTAMPTZ 적용
데이터가 적재되는 `app_logs` 테이블의 생성 및 마이그레이션을 자동 관리합니다.
* **파일**: [database.py](file:///d:/dev/lookalike-lightweight/web/backend/app/database.py)
* **스키마 구조**:
  ```sql
  CREATE TABLE IF NOT EXISTS app_logs (
      id SERIAL PRIMARY KEY,
      level VARCHAR(20),
      service VARCHAR(50) DEFAULT 'FastAPI',
      message TEXT,
      error_type VARCHAR(100),
      timestamp TIMESTAMPTZ DEFAULT NOW()
  );
  ```
* **타임존 마이그레이션**: 기존의 `TIMESTAMP` 타입(타임존 없음)이 가용 시간과 9시간 오차(KST 기준)를 일으키는 문제를 해결하기 위해 기동 시점에 `ALTER COLUMN timestamp TYPE TIMESTAMPTZ;`를 실행하여 타임존 정보 유지를 강제화했습니다.

### 3.2 NeonLogHandler 구현 및 주요 로거 바인딩
Python 표준 `logging.Handler`를 오버라이드하여 데이터베이스 삽입 루틴을 연결하고, Uvicorn 서버 구동 시 로깅이 오버라이드되어 유실되는 현상을 보완했습니다.
* **파일**: [main.py](file:///d:/dev/lookalike-lightweight/web/backend/app/main.py)
* **등록 로거**: `root` 로거 및 `uvicorn`, `uvicorn.error`, `uvicorn.access`, `fastapi` 관련 로거들 전체에 `NeonLogHandler`를 직접 바인딩하고 `propagate = True`로 전파를 활성화했습니다.
* **분류 규칙**:
  * 로그 이름 및 메시지 내의 `database`, `sqlalchemy`, `psycopg` ➡️ `PostgreSQL` 서비스로 분류
  * 로그 이름 및 메시지 내의 `cloudinary` ➡️ `Cloudinary` 서비스로 분류
  * 로그 이름 및 메시지 내의 `hf`, `huggingface`, `gradio` ➡️ `HuggingFace` 서비스로 분류
  * 그 외의 모든 로그 ➡️ `FastAPI` 서비스로 분류

### 3.3 글로벌 예외 처리기(Global Exception Handler) 도입
/sentry-debug 등 미처리 에러로 인해 500 Internal Server Error 발생 시 로그가 손실 없이 즉각 포착되도록 강제합니다.
* **파일**: [main.py](file:///d:/dev/lookalike-lightweight/web/backend/app/main.py)
* **구현**:
  ```python
  @app.exception_handler(Exception)
  async def global_exception_handler(request, exc):
      logger.error(f"서버 내부 예외 발생: {exc}", exc_info=exc)
      return JSONResponse(
          status_code=500,
          content={"detail": "Internal Server Error"}
      )
  ```

### 3.4 어드민 UI 개선 및 INFO 레벨 제외
* **파일**: [admin_logs.html](file:///d:/dev/lookalike-lightweight/web/frontend/templates/admin_logs.html), [admin_logs.js](file:///d:/dev/lookalike-lightweight/web/frontend/static/js/admin_logs.js)
* **조치**: 서버의 경량화 목적을 지키기 위해 `INFO` 로그는 수집 대상에서 제외되었으므로, 화면상의 `INFO` 라디오 필터 버튼, 통계 수치 열 및 24시간 트렌드 차트의 `INFO` 데이터셋을 전면 제거하여 사용자 혼선을 없애고 에러 모니터링 본연의 기능에 집중하도록 개선했습니다.

---

## 4. 성능 최적화 및 안정성 보장 (Technical Decisions)

1. **무한 재귀 로깅 루프 방지**:
   * 데이터베이스에 로그를 기록하는 쿼리를 날릴 때 내부적으로 `psycopg2` 또는 `sqlalchemy`에서 로그가 생성되면 `NeonLogHandler`가 이를 잡고 다시 DB 인서트를 시도하는 무한 루프가 발생할 수 있습니다.
   * `NeonLogHandler.emit()` 내에서 로거 이름이 `sqlalchemy`, `psycopg2`, `app.database` 관련 로거인 경우에는 수집 대상에서 완전히 배제하여 안정성을 확보했습니다.

2. **자동 링 버퍼 청소 정책**:
   * DB에 로그를 인서트할 때마다 `DELETE FROM app_logs WHERE timestamp < NOW() - INTERVAL '24 hours';`를 자동 기동시켜 24시간이 경과한 데이터는 상시 파기되며, 이를 통해 무료 등급 DB 용량을 한계치 안으로 완벽히 보존합니다.
