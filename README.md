# 👗 Lookalike (Snap-Match) — 듀프족을 위한 AI 패션 이미지 검색 플랫폼 (3세대 경량 서버)

> **"비슷한 옷, 더 싸게"** — 이미지 한 장으로 유사 패션 상품을 찾고 최저가 쇼핑몰을 한 번에 비교합니다. 본 프로젝트는 과거 Lookalike 팀원들이 구축한 부트캠프 최우수상 초기 분산 클러스터 아키텍처 유산([이전 버전 프로젝트 레포지토리](https://github.com/daniel-dataflow/main-project-lookalike))을 기반으로 삼아, Render 무료 서버 환경에 맞춰 **3세대 경량 서버 아키텍처**로 리팩토링을 단독 진행한 버전입니다.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109.0-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/Neon_PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql)](https://neon.tech)
[![HuggingFace](https://img.shields.io/badge/HuggingFace-Spaces-FFD21E?style=flat-square&logo=huggingface)](https://huggingface.co)
[![Cloudinary](https://img.shields.io/badge/Cloudinary-Media-3448C5?style=flat-square&logo=cloudinary)](https://cloudinary.com)

---

## 🏗️ 3세대 경량 서버 설계 배경 (Architecture Evolution)

본 프로젝트는 기존의 대규모 인프라망(Elasticsearch, Apache Spark, Hadoop HDFS, Kafka, Redis, MongoDB)을 전면 제거하고, 메모리 512MB 한계인 Render 무료 티어에서도 원활하게 작동할 수 있도록 초경량 싱글 서버 아키텍처로 거듭났습니다.

| 비교 항목 | 레거시 아키텍처 (GCP 컨테이너 클러스터) | 3세대 경량 서버 아키텍처 (현재 리팩토링) |
| :--- | :--- | :--- |
| **코어 RDBMS** | PostgreSQL (GCP VM 컨테이너 운용) | **Neon PostgreSQL (Cloud Serverless DB)** |
| **벡터 데이터베이스** | Elasticsearch kNN (ViT-B/32, SBERT) | **pgvector (HNSW Index 코사인 유사도 검색)** |
| **세션 저장소** | Redis (Stateful Memory DB) | **PostgreSQL 기반 DB 세션 (`user_sessions` 테이블)** |
| **ML/AI 인퍼런스** | 로컬 전용 FastAPI ML 서버 (NVIDIA GPU 가속) | **HuggingFace Space 위탁 추론 (Gradio API 연동)** |
| **이미지/미디어 저장** | 로컬 컨테이너 호스트 파일시스템 | **Cloudinary (3세대 경량 서버 신규 도입 이미지 클라우드)** |
| **리소스 & 로그 모니터링** | Filebeat ➔ Kafka ➔ Logstash ➔ ES ➔ 어드민 | **cgroups/psutil 동적 감지 + DB 로그 링 버퍼(24h)** |

---

## 📋 프로젝트 개요 및 핵심 기능

### 🎯 핵심 기능
1. **YOLO 기반 객체 탐지**: 사진 업로드 시 HuggingFace Space의 YOLO 모델을 위탁 호출하여 아우터/상의/하의 영역을 정확히 검출하고 네모칸을 선택해 정밀 검색을 수행합니다.
2. **Fashion-CLIP 벡터 매칭**: 패션 도메인 특화 CLIP 임베딩을 이용하고 PostgreSQL pgvector HNSW 인덱스를 활용해 0.1초 내외로 초고속 유사 의류를 매칭합니다.
3. **Late Fusion RRF (복합 검색)**: 이미지 벡터(70%)와 텍스트 의미 벡터(30%)를 RRF(Reciprocal Rank Fusion) 알고리즘으로 결합하여 정교한 검색 결과를 산출합니다.
4. **실시간 최저가 연동**: 검색된 유사 상품들에 대해 Naver 쇼핑 API를 통해 최저가 5개 쇼핑몰 가격을 실시간으로 비교 제공합니다.
5. **초경량 어드민 모니터링**: 
   - cgroups(v1 & v2)와 psutil을 이용해 컨테이너의 실제 리소스 한계치(512MB RAM, 1vCPU)를 자동 추적합니다.
   - 데이터베이스 용량 초과 방지를 위한 24시간 제한 로그 링 버퍼가 Neon DB 상에 매끄럽게 흐릅니다.

---

## 📁 프로젝트 구조

```text
snap-match/
├── docs/                          # 📚 3세대 경량 서버 인프라 및 로그 모니터링 명세서
│   └── renewal/                   # admin_infra_renewal.md, admin_logs_renewal.md
├── ml-models/                     # 🤖 머신러닝 인퍼런스 레이어 (HuggingFace Space 배포용)
│   └── api/                       # hf_space_app.py (YOLO 탐지 및 Fashion-CLIP 임베딩 추출)
├── web/                           # 🌐 코어 백엔드 및 웹 프론트엔드
│   ├── backend/                   # 메인 FastAPI 앱 엔진
│   │   ├── app/
│   │   │   ├── config/            # Pydantic 기반 환경변수 매핑 (base.py)
│   │   │   ├── database.py        # Neon DB 연결, 테이블 자동 생성 및 세션 관리
│   │   │   ├── routers/           # /api/* 라우터 (YOLO detect API 내장)
│   │   │   └── services/          # RRF 검색 로직, HF Space 호출 및 Cloudinary 연동
│   │   └── requirements.txt
│   └── frontend/                  # 초경량 SSR Jinja2 뷰
│       ├── static/                # CSS, JS, Image 에셋 (admin_logs.js 등)
│       └── templates/             # HTML 템플릿 파일
├── .env                           # 통합 환경변수 설정 파일
└── README.md                      # 메인 문서
```

---

## 🏗️ 3세대 경량 서버 아키텍처 및 데이터 흐름

```text
[실시간 검색 서비스 흐름]
사용자 이미지 업로드 ──► FastAPI (Main Server)
                             │
                             ├─► [YOLO 객체탐지] ──► HuggingFace Space API (YOLO)
                             ├─► [임베딩 추출] ──► HuggingFace Space API (Fashion-CLIP)
                             │
                             ▼ [유사도 비교]
                     Neon PostgreSQL (pgvector HNSW 인덱스 코사인 검색)
                             │
                             ▼ [최저가 비교]
                     Naver 쇼핑 API 실시간 조회 ──► 사용자 결과 반환
```

---

## 🔧 시작하기 및 실행 방법

### 1. 환경 변수 설정
프로젝트 루트 디렉터리에 `.env` 파일을 생성하고 필수 설정값들을 기입합니다:
```ini
# 공통 설정
ENV_MODE=production
DATABASE_URL=postgresql://[Neon_DB_User]:[Password]@[Host]/[DB_Name]?sslmode=require

# HuggingFace & Cloudinary (필수)
HF_SPACE_URL=https://[Your-HF-Space-Name].hf.space
CLOUDINARY_CLOUD_NAME=[Cloud_Name]
CLOUDINARY_API_KEY=[API_Key]
CLOUDINARY_API_SECRET=[API_Secret]

# Naver API (최저가 검색용)
NAVER_CLIENT_ID=[Naver_Client_ID]
NAVER_CLIENT_SECRET=[Naver_Client_Secret]
```

### 2. 로컬 실행 방법
```bash
# 의존성 설치
cd web/backend
pip install -r requirements.txt

# FastAPI 백엔드 서버 기동 (8900 포트)
uvicorn app.main:app --host 0.0.0.0 --port 8900 --reload
```
서버 기동 시 [database.py](web/backend/app/database.py)에 작성된 초기화 로직에 의해 Neon PostgreSQL의 `infra_metrics` 및 `app_logs` 테이블이 자동으로 검증 및 마그레이션(TIMESTAMPTZ 타입 마이그레이션 포함) 처리됩니다.

## 📜 프로젝트 아키텍처 진화 히스토리 (Architecture Evolution History)

Lookalike 프로젝트는 고비용의 분산 빅데이터 클러스터 환경에서 시작하여 한 달 운영비 0원의 초경량 분산 구조에 이르기까지, 인프라 비용과 하드웨어 제약 조건에 맞춰 점진적으로 고도화 및 다이어트를 반복하며 발전해 왔습니다.

```mermaid
graph TD
    A["[1단계] 로컬 컨테이너 PoC<br>(Docker 기반 팀 공동 개발)"] 
    ──► B["[2단계] AWS 클라우드 확장<br>(ML 탑재 + Kafka/ES 실시간 로그망)"]
    ──► C["[3단계] GCP 인프라 이전<br>(자원 한계 대응 1차 기능 축소 & 시간 제한)"]
    ──► D["[4단계] Render 3세대 경량 서버 (현재)<br>(컨테이너 해체 ➔ Serverless DB + HF API 분산 위탁)"]
    
    style A fill:#f9f,stroke:#333,stroke-width:2px
    style B fill:#bbf,stroke:#333,stroke-width:2px
    style C fill:#fdd,stroke:#333,stroke-width:2px
    style D fill:#dfd,stroke:#333,stroke-width:2px
```

### 1단계: 로컬 컨테이너 PoC (팀 공동 개발 및 기틀 구축)
* **목적**: 듀프 패션 검색 서비스 아이디어를 검증하기 위한 핵심 컴포넌트 간 연동 테스트.
* **특징**: 모든 팀원이 동일한 도커 개발 환경에서 PostgreSQL, Elasticsearch, MongoDB, Redis, Hadoop, Spark, Airflow, Kafka 등 대형 인프라망을 Docker Compose 기반으로 로컬에서 빌드 및 구동하는 데 성공하며 클라우드 배포를 위한 기술적 기틀을 완성했습니다.

### 2단계: AWS 클라우드 배포 및 실시간 분석망 구축
* **목적**: 상용 서비스 가능 여부 검증 및 대용량 데이터 전처리/로그 파이프라인 고도화.
* **특징**: AWS 클라우드로 인프라를 확장 배포하고, YOLO 및 Fashion-CLIP 기반 이미지 유사도 검색 로직을 ML 인프라망에 통합했습니다. 특히 시스템 모니터링 성능 강화를 위해 Filebeat ➔ Kafka ➔ Logstash ➔ Elasticsearch로 흐르는 실시간 로그 스트리밍망을 탑재하여 완벽한 분산 클러스터를 설계했습니다.

### 3단계: GCP 인프라 이전 및 과도기적 경량화 (GCP 컨테이너 클러스터)
* **목적**: 인프라 운영 비용 절감 및 하드웨어 한계 극복을 위한 1차 최적화.
* **특징**: AWS 환경에서 GCP VM 인프라로 이전하며 동일한 컨테이너 클러스터를 구성했습니다. 다만, 클라우드 자원 스펙 축소로 인해 발생하는 메모리 부족 이슈에 대응하고자 일부 무거운 전처리 단계를 간소화하고, 무료 등급 자원 유지를 위해 어드민 대시보드 및 서비스 운영 시간에 한시적 제한을 두는 등 과도기적인 인프라 다이어트를 1차 적용했습니다.

### 4단계: 3세대 경량 서버 구축 및 1인 단독 리팩토링 (현재)
* **목적**: 월 인프라 비용 0원 유지 및 512MB 극소 메모리 환경에서의 365일 무중단 서비스 안착.
* **특징**: 기존의 Docker 클러스터 구조를 전격 해체하고, 무료 클라우드 환경에 최적화된 새로운 설계 패러다임으로 리팩토링을 완수했습니다.
  - **연산 위탁을 통한 리소스 분리**: 512MB 메모리에서 불가능한 YOLO/Fashion-CLIP 연산을 16GB 자원을 무상 제공하는 HuggingFace Space API로 전면 위탁하여 물리적 자원을 격리했습니다.
  - **Serverless DB 전환**: 기존 로컬 PostgreSQL 대신 Serverless 기반의 Neon DB 및 pgvector(HNSW Index)를 채택해 기기 DB 부하를 제로화했습니다.
  - **슬립 모드 및 제약 조건 극복**: Render 무료 인프라의 자동 절전(Sleep) 및 Ephemeral 스토리지 특성을 극복하기 위해, 디스크 실측 롤백 및 cgroups를 활용해 메모리/CPU 사양을 실시간으로 감지하고 24시간 만료 로그 링 버퍼를 활용해 초경량 무결성 모니터링 체계를 확보했습니다.