# 👗 Lookalike — AI Fashion Image Search Platform

> **"Similar style, better price"** — Upload one image to find visually similar fashion items and compare prices across stores instantly.

**Boot camp team project (top award) → Solo refactor → $0/month, always-on service**

**[🇰🇷 한국어 버전 →](./README.md)**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109.0-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/Neon_PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql)](https://neon.tech)
[![HuggingFace](https://img.shields.io/badge/HuggingFace-Spaces-FFD21E?style=flat-square&logo=huggingface)](https://huggingface.co)
[![Cloudinary](https://img.shields.io/badge/Cloudinary-CDN-3448C5?style=flat-square&logo=cloudinary)](https://cloudinary.com)
[![Render](https://img.shields.io/badge/Render-Free_Plan-46E3B7?style=flat-square&logo=render)](https://render.com)
[![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Automated-2088FF?style=flat-square&logo=githubactions)](https://github.com/features/actions)

---

## 📋 Table of Contents

1. [Project Overview](#-project-overview)
2. [Architecture Evolution (4 Stages)](#-architecture-evolution-4-stages)
3. [Current Architecture (Stage 4)](#-current-architecture-stage-4)
4. [Core Features](#-core-features)
5. [Project Structure](#-project-structure)
6. [Getting Started](#-getting-started)
7. [Technical Decision Rationale](#-technical-decision-rationale)
8. [Admin API Reference](#-admin-api-reference)
9. [Links](#-links)

---

## 🎯 Project Overview

Lookalike lets users upload a fashion photo and instantly find visually similar products with real-time price comparison across major Korean shopping platforms.

```
User uploads image
       │
       ▼
  YOLO object detection (outerwear / top / bottom)
       │
       ▼
  Fashion-CLIP vector embedding
       │
       ▼
  pgvector HNSW similarity search (~0.1s)
       │
       ▼
  Late Fusion RRF (image 70% + text 30%)
       │
       ▼
  Naver Shopping API → price comparison across 5 stores
       │
       ▼
  Results returned to user
```

---

## 🚀 Architecture Evolution (4 Stages)

Each stage was driven by a real constraint. Here's how each one was solved.

```
[Local Docker] ──► [AWS + GPU] ──► [GCP Free] ──► [Render Free · Current]
   Stage 1           Stage 2        Stage 3           Stage 4
  Unified env       Cloud scale    Cost cut          $0/month achieved
```

---

### 📍 Stage 1 — Unified Local Dev Environment

| | |
|---|---|
| **Problem** | Each team member had a different local setup → dependency conflicts, wasted time |
| **Solution** | Single `docker-compose.yml` managing PostgreSQL, Elasticsearch, Redis, Kafka, Hadoop, Spark, Airflow |
| **Outcome** | Eliminated environment errors, maximized team velocity |

---

### 📍 Stage 2 — AWS Cloud Deployment (Jan–Mar 2026)

| | |
|---|---|
| **Context** | After team success, needed to validate at real scale |
| **Outcome** | Proof of concept delivered, boot camp top award 🏆 |

**Stack:**
- YOLO + Fashion-CLIP inference on AWS EC2 + GPU instance
- Spark distributed processing + Kafka message streaming
- Filebeat → Logstash → Elasticsearch log pipeline

---

### 📍 Stage 3 — GCP Free Credits (Mar–Jun 2026)

| | |
|---|---|
| **Context** | Boot camp ended → AWS costs unsustainable → migrated to GCP free credits |
| **Problem** | No GPU available → had to redesign the entire ML pipeline |
| **Outcome** | ~3 months of service on free credits alone |

**Solution:**
- Restructured architecture to delegate ML inference to external APIs
- Optimized VM specs (cost/performance tradeoff analysis)
- Scheduled server downtime outside interview hours to extend credit runway

---

### 📍 Stage 4 — Extreme Lightweight Architecture ⭐ Current (Jun 2026~)

> **Hard constraint**: Render free plan — 512MB RAM, $0/month  
> **Core problem**: YOLO + Fashion-CLIP alone require ~3GB RAM

| Problem | Previous Approach | New Solution | Saved |
|---------|-----------------|-------------|:-----:|
| ML inference (3GB+) | Local GPU server | HuggingFace Space API | **1GB+** |
| Message queue | Kafka broker | PostgreSQL direct INSERT | **~200MB** |
| Log storage | ELK Stack | PostgreSQL Ring Buffer (24h) | **~150MB** |
| Session management | Redis | PostgreSQL table | **~50MB** |
| Orchestration | Airflow server | GitHub Actions | **~100MB** |
| File storage | Local disk | Cloudinary CDN | Ephemeral resolved |
| DB capacity | 0.5GB single | 2-account split (PROD + DW) | 1GB total |
| **Total** | | | **✅ 500MB+** |

**Result**: All core features preserved within 512MB — **$0/month operating cost**

---

## 🏗️ Current Architecture (Stage 4)

### Real-time Search Flow

```
User image upload
       │
       ▼
FastAPI main server (Render, 512MB)
       ├──► HuggingFace Space API ──► YOLO object detection
       └──► HuggingFace Space API ──► Fashion-CLIP embedding
                                             │
                                             ▼
                                  Neon PostgreSQL PROD_DB
                                  (pgvector HNSW cosine search)
                                             │
                                             ▼
                                  Naver Shopping API (real-time price)
                                             │
                                             ▼
                                       Results to user
```

### Crawling Pipeline Flow

```
GitHub Actions (scheduled)
       │
       ├── Phase 1: Parallel crawl across 5 brands
       │        └── Upload images to Cloudinary staging/
       │        └── Store products + embeddings in DW DB
       │
       └── Phase 2: Validate & swap after 24h
                └── Check data integrity + image URLs (HTTP 200)
                └── Move Cloudinary staging/ → products/
                └── Atomic copy DW DB → PROD DB (<1s transaction)
```

### Dual DB Layout

```
┌──────────────────────────────┐   ┌──────────────────────────────────┐
│   PROD_DB  (Neon, 0.5GB)     │   │   PROD_DW_DB  (Neon, 0.5GB)     │
│   Core service data only     │   │   Crawl buffer + log ring        │
│  ────────────────────────    │   │  ──────────────────────────────  │
│  products                    │   │  staging_products                │
│  product_embeddings          │   │  staging_product_embeddings      │
│  user_sessions               │   │  app_logs (24h Ring Buffer)      │
│                              │   │  infra_metrics (1h Ring Buffer)  │
└──────────────────────────────┘   └──────────────────────────────────┘
         ↑ HNSW index performance                ↑ Isolates main DB
```

---

## ✨ Core Features

### 🔍 Search Engine
- YOLO clothing region detection (outerwear / top / bottom)
- Fashion-CLIP embeddings + pgvector HNSW index for ~0.1s similarity search
- Late Fusion RRF: image vector 70% + text semantic 30%

### 🛒 Price Comparison
- Naver Shopping API integrated across 5 Korean shopping platforms

### ⚙️ Crawling Pipeline
- 2-phase Blue-Green deployment: Phase 1 (collect) → 24h validation → Phase 2 (swap)
- CDC filtering: only re-crawl changed products
- Emergency rollback support

### 📊 Admin Monitoring
- cgroups (v1/v2) + psutil for dynamic CPU/RAM/Disk detection
- Real-time PROD/DW dual DB capacity display
- Crawling phase progress + next scheduled swap time
- ERROR/WARN log real-time streaming + download

---

## 📁 Project Structure

```
snap-match/
├── .github/
│   └── workflows/                 # ⚡ GitHub Actions crawling automation
├── docs/
│   └── renewal/                   # 📚 Migration specs (architecture decisions)
├── data-pipeline/
│   └── crawlers/web_crawlers/     # 🕷️ Brand spiders + CLI tool
├── supabase/
│   └── migrations/                # 🗄️ Neon DB schema DDL
├── scripts/
│   └── insert_DB/                 # 🛠️ DB initialization scripts
├── web/
│   ├── backend/
│   │   └── app/
│   │       ├── config/            # Pydantic env var mapping
│   │       ├── database.py        # Neon connection + auto table creation
│   │       ├── routers/           # Search + admin API
│   │       └── services/          # RRF logic, HF Space calls, Cloudinary
│   └── frontend/
│       ├── static/                # CSS, JS assets
│       └── templates/             # Jinja2 HTML templates
└── .env                           # DEV/PROD environment separation
```

---

## 🚀 Getting Started

### 1. Environment Variables

```ini
# .env
ENV_MODE=local  # local | dev | production

# ── PRODUCTION ──────────────────────────────────
PROD_DATABASE_URL=postgresql://...
PROD_DW_DATABASE_URL=postgresql://...
PROD_CLOUDINARY_CLOUD_NAME=...
PROD_CLOUDINARY_API_KEY=...
PROD_CLOUDINARY_API_SECRET=...

# ── DEVELOPMENT (same format, DEV_ prefix) ──────
DEV_DATABASE_URL=...

# ── Shared APIs ──────────────────────────────────
HF_SPACE_URL=...
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
```

### 2. Run Locally

```bash
# Initialize DBs
python scripts/insert_DB/init_dev_db.py
python scripts/insert_DB/init_dw_db.py

# Start backend
cd web/backend
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8900 --reload
```

> 💡 `database.py` auto-creates `infra_metrics` and `app_logs` tables on startup

### 3. Test the Crawling Pipeline

```bash
# Phase 1: collect
python data-pipeline/crawlers/web_crawlers/crawling_pipeline_cli.py \
  --brand musinsa --limit 10 --action crawl

# Phase 2: validate and swap (--force to skip 24h wait)
python data-pipeline/crawlers/web_crawlers/crawling_pipeline_cli.py \
  --brand musinsa --action swap --force

# Supported brands: musinsa | 8seconds | topten | uniqlo | zara
```

### 4. GitHub Actions Deployment

After saving secrets, enable the schedule:

```yaml
schedule:
  - cron: '0 18 * * 0'  # Phase 1 — weekly crawl (Sunday)
  - cron: '0 20 * * *'  # Phase 2 — daily validate & swap
```

---

## 💡 Technical Decision Rationale

> Every Stage 4 decision came from one constraint: **run everything for $0/month.**

**🤗 HuggingFace Space API**
YOLO + Fashion-CLIP combined need ~3GB RAM. Loading them directly on a 512MB server means instant OOM. By delegating inference to HF Space's free 16GB GPU environment, the main server only handles API calls.

**🗄️ Dual DB isolation**
Neon's free plan caps at 0.5GB. By isolating PROD_DB (products + embeddings only) and handling crawl staging and logs in a separate DW_DB account, total usable storage doubles to 1GB while protecting search performance.

**⚡ GitHub Actions over Airflow**
Airflow's own server process consumes ~200MB. GitHub Actions' free 3,000 CI/CD minutes/month handles 5 brands × 2 phases = 10 parallel jobs at no cost.

**📋 PostgreSQL Ring Buffer over ELK**
Elasticsearch requires 512MB minimum on its own. Keeping only the last 24 hours of ERROR/WARN logs in a PostgreSQL table eliminates that entire overhead with ~80% storage reduction.

---

## 📡 Admin API Reference

| Endpoint | Description |
|----------|-------------|
| `GET /api/admin/dashboard` | Unified KPI dashboard |
| `GET /api/admin/system/health` | CPU/RAM/Disk + DB capacity (live) |
| `GET /api/admin/pipeline/status` | Crawl phase progress + next swap time |
| `GET /api/logs/dashboard` | Log stats and 24h trend |
| `GET /api/logs/stream` | ERROR/WARN log real-time stream |

---

## 🔗 Links

| Document | Description |
|----------|-------------|
| [Previous team project repository](https://github.com/daniel-dataflow/main-project-lookalike) | Original boot camp award-winning project |
| [Crawling pipeline spec](docs/renewal/crawling.md) | 2-phase pipeline, CDC filtering, Blue-Green deployment |
| [Infrastructure lightweight spec](docs/renewal/admin_infra_renewal.md) | cgroups(v1/v2) resource detection, Ring Buffer structure |
| [Log system spec](docs/renewal/admin_logs_renewal.md) | 24h log ring buffer, timezone conversion, NeonLogHandler |
| [Full migration guide](docs/renewal/README.md) | Stage 1–4 transition history and decision log |
