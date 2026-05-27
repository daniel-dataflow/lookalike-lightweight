# 3세대 경량화 인프라 모니터링 시스템 아키텍처 및 워크플로우 명세서

> **문서 목적**: 본 문서는 Lookalike 프로젝트 아키텍처 경량화(Kafka, Elasticsearch, Logstash, MongoDB, Redis 제거) 작업에 맞추어 Neon PostgreSQL과 로컬 psutil을 활용해 새롭게 설계된 **3세대 초경량 링 버퍼(Ring Buffer) 기반 인프라 모니터링 시스템**의 구조, 데이터 흐름, 핵심 기술적 의사결정을 정리하여 기술함.

---

## 1. 3세대 경량화 설계 배경 및 요구사항

Lookalike 프로젝트는 Render 무료 서버 환경(RAM 512MB, CPU 제한) 및 윈도우 로컬 개발망에 맞춰 기존의 무거운 빅데이터 스택(Elasticsearch, Logstash, Kafka 등)을 전면 제거하고 단일 데이터베이스(Neon PostgreSQL) 체계로 아키텍처를 경량화했습니다. 

이에 따라 인프라 모니터링 역시 시스템 리소스를 최소화하며 동작하도록 아래의 세 가지 요구사항을 충족하도록 재설계되었습니다:

| 요구사항 속성 | 3세대 경량화 구현 방식 |
| :--- | :--- |
| **초경량성 (Low Footprint)** | Docker SDK 및 Kafka 브로커 호출을 제거하고, `psutil` 내장 모듈을 활용하여 기기 리소스 직접 측정 |
| **링 버퍼 (Ring Buffer) 구조** | Neon PostgreSQL 단일 테이블을 사용하여 1시간 이내 시계열 데이터만 유지하고 구형 데이터는 자동 청소 |
| **동적 감지 (Dynamic Sensing)** | 로컬 PC(Windows) 및 실 배포 환경(Render Linux)의 운영체제 및 리소스 한계를 백엔드 레벨에서 동적 식별 |

---

## 2. 모니터링 아키텍처 및 데이터 흐름 (Workflow)

```text
[실시간 수집] (10초 주기 폴링)
  브라우저 어드민 페이지 (admin_infra.js)
    ├─► GET /api/metrics/realtime ──► psutil (CPU 코어/주파수, RAM 가용량, Disk 실용량 즉시 측정)
    └─► GET /api/admin/system/health ──► 외부 서비스 (Cloudinary, HF Space) 실시간 API 상태/Latency 측정

[시계열 추이 수집] (5분 주기 백그라운드 크론)
  FastAPI 백그라운드 수집기 (start_metric_collector)
    ├─► psutil 스냅샷 측정 (CPU / RAM %)
    ├─► INSERT INTO infra_metrics (Neon DB)
    └─► DELETE FROM infra_metrics WHERE timestamp < NOW() - INTERVAL '1 hour' (링 버퍼 유지)
```

---

## 3. 핵심 모니터링 대상 및 데이터 소스 변경

기존 분산 컨테이너 및 3대 RDBMS 관제 체계에서, 일체형 단일 FastAPI 아키텍처에 맞게 현실적이고 실용적인 외부 서비스 모니터링으로 전면 개편되었습니다:

1. **FastAPI (백엔드 호스트)**:
   * **CPU**: 실시간 CPU 사용량(%) 외에 코어 수(예: `4C/8T`), 현재 동작 주파수(GHz)를 실시간 감지합니다.
   * **Memory**: 실시간 가용 메모리 상태를 기가바이트(GB) 단위로 표기합니다.
   * **Disk**: 파일 업로드로 인한 디스크 잔여량 확보를 관제하기 위해 호스트 본체 볼륨 노이즈가 섞이지 않도록 현재 작업 디렉터리(`.`) 기준의 사용 용량, 여유 공간 크기를 기가바이트(GB) 및 진행 바로 표시합니다. Render 환경의 권한 및 가상화 에러를 예방하기 위해 try-except 예외 방어 구조와 Fallback 기본값(0)을 사용합니다.
   * **Uptime**: FastAPI가 기동된 후 누적 경과 시간(초/분/시간)을 동적으로 연산하여 표시합니다.

2. **Neon PostgreSQL**:
   * 활성 커넥션 개수(`pg_stat_activity`) 및 데이터베이스가 Neon 클라우드에 차지하고 있는 물리 용량(`pg_database_size`)을 실시간 조회합니다.

3. **Cloudinary (이미지 저장소)**:
   * 기존 MongoDB 카드를 대체합니다.
   * Cloudinary Python SDK를 이용하여 총 미디어 보관 용량(MB) 및 업로드된 이미지 리소스의 개수를 API를 통해 실시간 측정합니다.

4. **HuggingFace Space (ML 모델 호스팅)**:
   * 기존 Redis 카드를 대체합니다.
   * 모델 서빙 API 상태를 동적으로 호출 및 분석하고, 모델 응답 속도(Latency, ms)를 실시간으로 측정하여 통신 상태가 정상인지 식별합니다.

---

## 4. 성능 최적화 및 안정성 보장 (Technical Decisions)

1. **Cloudinary API 파싱 버그 수정**:
   * Cloudinary의 `usage()` 반환 정보 중 `resources` 속성은 정수형(`int`) 변수이므로, 기존 코드의 `usage.get("resources", {}).get("usage")` 조회로 인해 발생하던 `AttributeError`를 `usage.get("resources", 0)`로 바로 조회하도록 수정하여 통신 안정성을 확보했습니다.
   * Settings 클래스([base.py](file:///d:/dev/lookalike-lightweight/web/backend/app/config/base.py))에 누락되었던 Cloudinary Config 속성들을 추가 선언하여 환경변수 바인딩이 정상적으로 작동하도록 조치했습니다.

2. **프론트엔드 자동 갱신 속도 최적화 (10초)**:
   * 대시보드의 실시간 갱신 체감을 살리기 위해 프론트엔드([admin_infra.js](file:///d:/dev/lookalike-lightweight/web/frontend/static/js/admin_infra.js)) 자동 새로고침 인터벌을 **30초**에서 **10초**로 단축했습니다.
   * 1초 단위 갱신은 지속적인 로컬 하드웨어 IO 및 외부 API(Cloudinary, HF Space) 호출 한도 초과(Rate Limit) 위험을 초래할 수 있으므로, 최적의 타협점인 10초로 세팅했습니다.

3. **존재하지 않는 API 프리로드 제거**:
   * 레거시 API인 `/api/admin/infra/dashboard` 404 에러를 방지하기 위해 [admin_common.js](file:///d:/dev/lookalike-lightweight/web/frontend/static/js/admin_common.js)의 프리로드 매핑에서 삭제하여 리소스 낭비를 방지했습니다.

4. **루트 파비콘 404 에러 해결**:
   * 브라우저가 자동 호출하는 `/favicon.ico` 경로에 대응하도록 [main.py](file:///d:/dev/lookalike-lightweight/web/backend/app/main.py) 내에 `FileResponse` 경로를 바인딩하여 404 노이즈 에러를 완전 차단했습니다.

5. **디스크 수집 경로 전환 및 가상화 예외 방어**:
   * 호스트 머신의 불필요한 대형 볼륨(386GB 등) 노이즈 노출을 제거하고자, 디스크 계측 경로를 루트(`/`)에서 현재 작업 디렉터리(`.`)로 변경해 어플리케이션 볼륨을 정확하게 감지하도록 했습니다.
   * Render 서버 가상화 스택에서의 디스크 권한 에러를 안전하게 대비하기 위해 `try-except` 예외 처리 및 Fallback 기본값 설정을 통해 모니터링 시스템의 기동 안정성을 높였습니다.
