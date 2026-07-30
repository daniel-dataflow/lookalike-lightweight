# 👗 Lookalike — AI 패션 이미지 검색 플랫폼

> **"비슷한 옷, 더 싸게"** — 이미지 한 장으로 유사 패션 상품을 찾고, 최저가 쇼핑몰을 한 번에 비교합니다.

**부트캠프 팀 프로젝트 (최우수상) → 개인 리팩토링 → 월 운영비 0원, 365일 무중단 서비스**
**URL : [lookalike-ml.onrender.com](https://lookalike-ml.onrender.com)**

**[🇺🇸 English Version →](./README.en.md)**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109.0-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/Neon_PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql)](https://neon.tech)
[![HuggingFace](https://img.shields.io/badge/HuggingFace-Spaces-FFD21E?style=flat-square&logo=huggingface)](https://huggingface.co)
[![Cloudinary](https://img.shields.io/badge/Cloudinary-CDN-3448C5?style=flat-square&logo=cloudinary)](https://cloudinary.com)
[![Render](https://img.shields.io/badge/Render-Free_Plan-46E3B7?style=flat-square&logo=render)](https://render.com)
[![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Automated-2088FF?style=flat-square&logo=githubactions)](https://github.com/features/actions)

---

## 📋 목차

1. [프로젝트 개요](#-프로젝트-개요)
2. [아키텍처 진화 (4단계)](#-아키텍처-진화-4단계)
3. [현재 아키텍처 (Stage 4)](#-현재-아키텍처-stage-4)
4. [핵심 기능](#-핵심-기능)
5. [프로젝트 구조](#-프로젝트-구조)
6. [시작하기](#-시작하기)
7. [기술 의사결정 배경](#-기술-의사결정-배경)
8. [어드민 API 레퍼런스](#-어드민-api-레퍼런스)
9. [관련 링크](#-관련-링크)

---

## 🎯 프로젝트 개요

Lookalike는 사용자가 업로드한 패션 이미지에서 **유사 상품을 검색**하고 **실시간 최저가를 비교**하는 서비스입니다.

```
사용자 이미지 업로드
       │
       ▼
  YOLO 객체 탐지 (아우터 / 상의 / 하의 영역 검출)
       │
       ▼
  Fashion-CLIP 벡터 임베딩 추출
       │
       ▼
  pgvector HNSW 유사도 검색 (0.1초 이내)
       │
       ▼
  Late Fusion RRF (이미지 70% + 텍스트 30%)
       │
       ▼
  Naver 쇼핑 API → 5개 쇼핑몰 최저가 비교
       │
       ▼
  사용자에게 결과 반환
```

---

## 🚀 아키텍처 진화 (4단계)

단순한 기능 구현이 아닌, **각 단계의 제약을 어떻게 극복했는지**에 집중한 여정입니다.

```
[Local Docker] ──► [AWS + GPU] ──► [GCP 무료] ──► [Render 무료 · 현재]
  Stage 1           Stage 2         Stage 3          Stage 4
  팀 환경 통일       클라우드 확장    비용 절감         월 0원 달성
```

---

### 📍 Stage 1 — 팀 개발 환경 통일 (로컬)

| 구분 | 내용 |
| :--- | :--- |
| **문제** | 팀원마다 다른 로컬 환경 → 의존성 충돌, 개발 속도 저하 |
| **해결** | Docker Compose 단일 파일로 PostgreSQL, Elasticsearch, Redis, Kafka, Hadoop, Spark, Airflow 전체 인프라 통일 |
| **성과** | 환경 설정 오류 제거, 팀 개발 효율 극대화 |

---

### 📍 Stage 2 — AWS 클라우드 확장 (2026.01 ~ 2026.03)

| 구분 | 내용 |
| :--- | :--- |
| **상황** | 팀 프로젝트 성공 후 실제 클라우드 환경에서 검증 필요 |
| **성과** | Proof of Concept 완성, 부트캠프 최우수상 수상 🏆 |

**구성 스택:**
- AWS EC2 + GPU 서버에서 YOLO / Fashion-CLIP 실시간 추론
- Spark 분산 처리 + Kafka 메시지 스트리밍
- Filebeat → Logstash → Elasticsearch 로그 파이프라인

---

### 📍 Stage 3 — GCP 무료 크레딧 운영 (2026.03 ~ 2026.06)

| 구분 | 내용 |
| :--- | :--- |
| **상황** | 부트캠프 종료 → AWS 비용 부담 → GCP 무료 크레딧으로 이전 |
| **문제** | GPU 사용 불가 → 기존 ML 파이프라인 전면 재설계 필요 |
| **성과** | 무료 크레딧만으로 약 3개월 운영 |

**해결 전략:**
- GPU 연산을 외부 API 위탁 구조로 전환 (아키텍처 준비)
- VM 사양 최적화 (비용/성능 트레이드오프 분석)
- 면접 외 시간대 서버 비활성화로 크레딧 집중 배분

---

### 📍 Stage 4 — 극한의 경량화 ⭐ 현재 (2026.06 ~ 2026.07)

> **제약**: Render 무료 플랜 — 메모리 512MB, 비용 0원  
> **핵심 문제**: YOLO + Fashion-CLIP만 해도 ~3GB 메모리 필요

| 문제 | 기존 방식 | 새 해결책 | 절감 효과 |
|------|---------|--------|:-------:|
| ML 연산 (3GB+) | 로컬 GPU 서버 | HuggingFace Space API 위탁 | **1GB+** |
| 메시지 큐 | Kafka 브로커 | PostgreSQL 직접 INSERT | **~200MB** |
| 로그 저장 | ELK Stack | PostgreSQL Ring Buffer (24h) | **~150MB** |
| 세션 관리 | Redis | PostgreSQL 테이블 | **~50MB** |
| 오케스트레이션 | Airflow 서버 | GitHub Actions | **~100MB** |
| 파일 저장 | 로컬 디스크 | Cloudinary CDN | Ephemeral 해결 |
| DB 용량 | 0.5GB 단일 | 2개 계정 분산 (PROD + DW) | 총 1GB 관리 |
| **합계** | | | **✅ 500MB+ 절감** |

**결과**: 512MB 제약 안에서 모든 핵심 기능 유지, **월 운영비 0원 달성**

---

## 🏗️ 현재 아키텍처 (Stage 4)

### 실시간 검색 흐름

```
사용자 이미지
     │
     ▼
FastAPI 메인 서버 (Render, 512MB)
     ├──► HuggingFace Space API ──► YOLO 객체 탐지
     └──► HuggingFace Space API ──► Fashion-CLIP 임베딩
                                          │
                                          ▼
                               Neon PostgreSQL PROD_DB
                               (pgvector HNSW 코사인 검색)
                                          │
                                          ▼
                               Naver 쇼핑 API (실시간 최저가)
                                          │
                                          ▼
                                   사용자에게 결과 반환
```

### 크롤링 파이프라인 흐름

```
GitHub Actions (자동 스케줄)
     │
     ├── Phase 1: 5개 브랜드 병렬 크롤링
     │        └── Cloudinary staging/ 업로드
     │        └── DW DB에 임시 저장 + 임베딩 생성
     │
     └── Phase 2: 24시간 후 자동 검증 & 스위칭
              └── 데이터 정합성 / 이미지 유효성 검증
              └── Cloudinary staging/ → products/ 이동
              └── DW DB → PROD DB 원자적 복사 (<1초)
```

### 이중 DB 구조

```
┌─────────────────────────────┐   ┌─────────────────────────────────┐
│    PROD_DB  (Neon, 0.5GB)   │   │   PROD_DW_DB  (Neon, 0.5GB)     │
│    핵심 서비스 전용          │   │    크롤링 버퍼 + 로그             │
│  ──────────────────────     │   │  ───────────────────────────    │
│  products                   │   │  staging_products               │
│  product_embeddings         │   │  staging_product_embeddings     │
│  user_sessions              │   │  app_logs (24h Ring Buffer)     │
│                             │   │  infra_metrics (1h Ring Buffer) │
└─────────────────────────────┘   └─────────────────────────────────┘
    ↑ HNSW 인덱스 성능 보호                ↑ 메인 DB 오염 방지
```

---

## ✨ 핵심 기능

### 🔍 검색 엔진
- YOLO 기반 의류 영역 탐지 (아우터 / 상의 / 하의)
- Fashion-CLIP 벡터 임베딩 + pgvector HNSW 인덱스로 0.1초 내외 유사 검색
- Late Fusion RRF: 이미지 벡터 70% + 텍스트 의미 30% 결합

### 🛒 쇼핑 비교
- Naver 쇼핑 API로 5개 쇼핑몰 실시간 최저가 연동

### ⚙️ 크롤링 파이프라인
- 2단계 Blue-Green 배포: Phase 1(수집) → 24시간 검증 → Phase 2(스위칭)
- CDC 필터링으로 변경된 상품만 재수집
- 긴급 롤백 지원

### 📊 어드민 모니터링
- cgroups(v1/v2) + psutil로 CPU/RAM/Disk 동적 감지
- PROD/DW 이중 DB 용량 실시간 표시
- Phase별 크롤링 진행 상황 + 다음 스위칭 예정 시간
- ERROR/WARN 로그 실시간 스트리밍 + 다운로드

---

## 📁 프로젝트 구조

```
snap-match/
├── .github/
│   └── workflows/                 # ⚡ GitHub Actions 크롤링 자동화
├── docs/
│   └── renewal/                   # 📚 마이그레이션 명세서 (아키텍처 의사결정)
├── data-pipeline/
│   └── crawlers/web_crawlers/     # 🕷️ 브랜드별 스파이더 + CLI 도구
├── supabase/
│   └── migrations/                # 🗄️ Neon DB 스키마 DDL
├── scripts/
│   └── insert_DB/                 # 🛠️ DB 초기화 자동화
├── web/
│   ├── backend/
│   │   └── app/
│   │       ├── config/            # Pydantic 환경변수 매핑
│   │       ├── database.py        # Neon 연결 + 테이블 자동 생성
│   │       ├── routers/           # 검색 + 어드민 API
│   │       └── services/          # RRF 로직, HF Space 호출, Cloudinary 연동
│   └── frontend/
│       ├── static/                # CSS, JS (admin_logs.js 등)
│       └── templates/             # Jinja2 HTML 템플릿
└── .env                           # DEV/PROD 환경 분리
```

---

## 🚀 시작하기

### 1. 환경 변수 설정

```ini
# .env
ENV_MODE=local  # local | dev | production

# ── PRODUCTION ──────────────────────────────
PROD_DATABASE_URL=postgresql://...
PROD_DW_DATABASE_URL=postgresql://...
PROD_CLOUDINARY_CLOUD_NAME=...
PROD_CLOUDINARY_API_KEY=...
PROD_CLOUDINARY_API_SECRET=...

# ── DEVELOPMENT (동일 형식, DEV_ 접두사) ────
DEV_DATABASE_URL=...

# ── 공통 API ────────────────────────────────
HF_SPACE_URL=...
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
```

### 2. 로컬 실행

```bash
# DB 초기화
python scripts/insert_DB/init_dev_db.py
python scripts/insert_DB/init_dw_db.py

# 백엔드 실행
cd web/backend
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8900 --reload
```

> 💡 `database.py` 시작 시 `infra_metrics`, `app_logs` 테이블 자동 생성

### 3. 크롤링 테스트

```bash
# Phase 1: 수집
python data-pipeline/crawlers/web_crawlers/crawling_pipeline_cli.py \
  --brand musinsa --limit 10 --action crawl

# Phase 2: 검증 및 스위칭 (--force로 즉시 실행)
python data-pipeline/crawlers/web_crawlers/crawling_pipeline_cli.py \
  --brand musinsa --action swap --force

# 지원 브랜드: musinsa | 8seconds | topten | uniqlo | zara
```

### 4. GitHub Actions 배포 설정

GitHub Secrets에 환경변수 등록 후 스케줄 활성화:

```yaml
schedule:
  - cron: '0 18 * * 0'  # Phase 1 — 매주 일요일 크롤링
  - cron: '0 20 * * *'  # Phase 2 — 매일 검증 & 스위칭
```

---

## 💡 기술 의사결정 배경

> Stage 4의 모든 기술 선택은 **"월 0원 운영 + 365일 무중단"** 이라는 단 하나의 목표에서 나왔습니다.

**🤗 HuggingFace Space API**
YOLO + Fashion-CLIP은 합산 ~3GB 메모리가 필요합니다. 512MB 서버에서 직접 로드하면 즉시 OOM. HF Space의 무료 16GB GPU 환경에 연산을 위탁해 메인 서버는 API 호출만 담당하게 했습니다.

**🗄️ 이중 DB 격리**
Neon 무료 플랜은 0.5GB 제한이 있습니다. 검색 성능에 직결되는 PROD_DB를 상품/임베딩 전용으로 보호하고, 크롤링 임시 데이터와 로그는 별도 계정(DW_DB)으로 격리해 총 1GB를 확보했습니다.

**⚡ GitHub Actions → Airflow 대체**
Airflow 서버 자체가 ~200MB를 점유합니다. GitHub Actions의 무료 월 3,000분 CI/CD로 5개 브랜드 × 2단계 = 10개 병렬 작업을 비용 없이 자동화했습니다.

**📋 PostgreSQL Ring Buffer → ELK 대체**
Elasticsearch는 최소 512MB 이상을 요구합니다. 24시간 이내 ERROR/WARN 로그만 PostgreSQL 테이블에 유지하는 Ring Buffer 구조로 저장소 약 80%를 절감했습니다.

---

## 📡 어드민 API 레퍼런스

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /api/admin/dashboard` | 통합 KPI 대시보드 |
| `GET /api/admin/system/health` | CPU/RAM/Disk + DB 용량 실시간 조회 |
| `GET /api/admin/pipeline/status` | 크롤링 Phase별 상태 + 다음 스위칭 예정 시간 |
| `GET /api/logs/dashboard` | 최근 로그 통계 및 24시간 트렌드 |
| `GET /api/logs/stream` | ERROR/WARN 로그 실시간 스트리밍 |

---

## 🔗 관련 링크

| 문서 | 설명 |
|------|------|
| [이전 버전 팀 프로젝트 레포지토리](https://github.com/daniel-dataflow/main-project-lookalike) | 부트캠프 최우수상 원본 프로젝트 |
| [크롤링 재설계 명세](docs/renewal/crawling.md) | 2단계 파이프라인 상세 명세, CDC 필터링, Blue-Green 배포 |
| [인프라 경량화 명세](docs/renewal/admin_infra_renewal.md) | cgroups(v1/v2) 기반 리소스 감지, Ring Buffer 구조 |
| [로그 시스템 명세](docs/renewal/admin_logs_renewal.md) | 24시간 로그 링 버퍼, 타임존 변환, NeonLogHandler |
| [전체 마이그레이션 가이드](docs/renewal/README.md) | Stage 1~4 전환 과정 및 의사결정 히스토리 |
