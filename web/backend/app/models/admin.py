"""
어드민 관련 Pydantic 모델 (경량 대시보드용)
- Docker/Kafka 실시간 스트리밍 제거
- Supabase DB 직접 조회 기반으로 교체
"""
from typing import Dict, List, Any, Optional
from pydantic import BaseModel
from datetime import datetime


# ──────────────────────────────────────
# 데이터 수집 현황
# ──────────────────────────────────────
class PipelineRunResponse(BaseModel):
    """파이프라인 실행 이력 단일 항목"""
    run_id: int
    pipeline_name: str
    brand: Optional[str] = None
    status: str
    total_items: int = 0
    new_items: int = 0
    updated_items: int = 0
    error_count: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_sec: Optional[int] = None


class PipelineStatusResponse(BaseModel):
    """데이터 수집 현황 대시보드"""
    recent_runs: List[PipelineRunResponse]
    total_runs_today: int = 0
    total_errors_today: int = 0
    last_successful_run: Optional[PipelineRunResponse] = None


# ──────────────────────────────────────
# 에러 로그
# ──────────────────────────────────────
class PipelineErrorResponse(BaseModel):
    """파이프라인 에러 단일 항목"""
    error_id: int
    run_id: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    product_id: Optional[str] = None
    source_url: Optional[str] = None
    created_at: Optional[datetime] = None


class ErrorLogListResponse(BaseModel):
    """에러 로그 목록"""
    errors: List[PipelineErrorResponse]
    total: int = 0
    page: int = 1


# ──────────────────────────────────────
# 시스템 상태
# ──────────────────────────────────────
class DataSummaryResponse(BaseModel):
    """데이터 요약 통계"""
    total_products: int = 0
    total_embeddings: int = 0
    total_users: int = 0
    total_searches: int = 0
    db_size_mb: float = 0.0
    brands: List[Dict[str, Any]] = []       # 브랜드별 상품 수


class SystemHealthResponse(BaseModel):
    """시스템 상태 (FastAPI 서버 + Supabase DB + Cloudinary + HF)"""
    server_status: str = "healthy"
    db_status: str = "unknown"
    db_active_connections: int = 0
    db_size_mb: float = 0.0
    app_version: str = ""
    environment: str = ""
    
    # Cloudinary 이미지 저장소 정보
    cloudinary_status: str = "unknown"
    cloudinary_usage_bytes: int = 0
    cloudinary_resources_count: int = 0
    
    # HuggingFace Space 정보
    hf_status: str = "unknown"
    hf_model_status: str = "unknown"
    hf_latency_ms: float = 0.0


# ──────────────────────────────────────
# 통합 대시보드
# ──────────────────────────────────────
class AdminDashboardResponse(BaseModel):
    """어드민 대시보드 통합 응답"""
    system: SystemHealthResponse
    data_summary: DataSummaryResponse
    pipeline: PipelineStatusResponse
