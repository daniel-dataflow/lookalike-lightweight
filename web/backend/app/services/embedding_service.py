"""
로컬 경량화 환경을 위한 텍스트 임베딩 서비스
- DB(product_embeddings)의 text_vector에 저장된 openai/clip-vit-base-patch32 (512d) + Zero-padding (256d) 규격과
  100% 동일한 임베딩 파이프라인을 구축하여 텍스트 검색 유사도를 복원합니다.
"""
import logging
import math
import httpx
from typing import Optional

from ..config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """텍스트 임베딩 서비스"""

    def __init__(self):
        self._settings = get_settings()

    async def encode_text(self, text: str) -> Optional[list[float]]:
        """
        HuggingFace Inference API를 활용해 CLIP text-encoder 임베딩 생성 및 Zero-padding 적용 (768d)
        """
        if not text or not text.strip():
            return None

        # 1. HF_TOKEN 설정 확인
        token = self._settings.HF_TOKEN
        if not token:
            logger.warning("⚠️ HF_TOKEN이 설정되지 않아 텍스트 임베딩을 추출할 수 없습니다. (DB Fallback으로 작동)")
            return None

        # DB에 저장된 원래 모델인 openai/clip-vit-base-patch32 지정
        model_id = "openai/clip-vit-base-patch32"
        url = f"https://api-inference.huggingface.co/pipeline/feature-extraction/{model_id}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"inputs": text}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                
                # 모델 로딩 지연 (503) 시 예외 처리
                if resp.status_code == 503:
                    logger.warning("⚠️ HuggingFace 모델 로딩 중 (503) -> DB Fallback 우회")
                    return None
                    
                resp.raise_for_status()
                data = resp.json()
                
                if isinstance(data, list):
                    # 1D/2D 리스트 차원 래핑 해제
                    if len(data) > 0 and isinstance(data[0], list):
                        vec = data[0]
                    else:
                        vec = data
                    
                    # CLIP 모델은 512차원을 뱉습니다. L2 정규화를 먼저 취합니다.
                    vec = self._l2_normalize(vec)
                    
                    # DB 장부(768차원)와 일치시키기 위해 256개의 0.0을 덧붙입니다 (Zero-padding)
                    padded_vec = self._pad(vec)
                    
                    logger.info(f"✅ CLIP 텍스트 임베딩 생성 성공 (512d -> 768d padded, 모델={model_id})")
                    return padded_vec
                        
        except Exception as e:
            logger.error(f"⚠️ CLIP 텍스트 임베딩 생성 중 오류 발생: {e}")
            
        return None

    @staticmethod
    def _pad(v: list[float]) -> Optional[list[float]]:
        """DB에 저장된 base_utils.py의 _pad 로직과 100% 동일하게 768차원 제로 패딩 적용"""
        if not v:
            return None
        if len(v) == 512:
            return v + [0.0] * 256
        elif len(v) < 768:
            return v + [0.0] * (768 - len(v))
        return v[:768]

    @staticmethod
    def _l2_normalize(vec: list[float]) -> list[float]:
        """L2 정규화 (cosine similarity 검색 최적화)"""
        s = sum(x * x for x in vec)
        if s <= 0.0:
            return vec
        norm = math.sqrt(s)
        return [x / norm for x in vec]


# 싱글톤 인스턴스
embedding_service = EmbeddingService()
