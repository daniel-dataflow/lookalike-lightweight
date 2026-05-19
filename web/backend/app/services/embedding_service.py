"""
외부 ML API 기반 임베딩 서비스

전략:
  이미지 임베딩 (512d):
    1. huggingface_hub InferenceClient → CLIP text feature_extraction
       (이미지 → Gemini Vision으로 텍스트 설명 생성 → CLIP 텍스트 임베딩)
    2. Gemini Vision 없을 때: CLIP 텍스트 임베딩 (쿼리 텍스트로 대체)

  텍스트 임베딩 (768d):
    - Gemini text-embedding-004
"""
import logging
import math
import io
import base64
import httpx
from typing import Optional

from ..config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """외부 API를 사용한 벡터 임베딩 생성 서비스"""

    def __init__(self):
        self._settings = get_settings()

    # ──────────────────────────────────────
    # 이미지 임베딩 (512d CLIP)
    # ──────────────────────────────────────
    async def encode_image(self, image_bytes: bytes) -> Optional[list[float]]:
        """
        이미지 → 512차원 CLIP 임베딩

        전략:
        1. Gemini Vision으로 이미지를 패션 텍스트 설명으로 변환
        2. 변환된 텍스트를 CLIP 텍스트 임베딩으로 인코딩 (512d)
        Gemini 없을 경우 None 반환 → DB fallback
        """
        if not self._settings.HF_TOKEN:
            logger.warning("HF_TOKEN이 설정되지 않았습니다")
            return None

        # Step 1. Gemini Vision으로 이미지 설명 생성
        caption = await self._describe_image_gemini(image_bytes)
        if not caption:
            logger.warning("Gemini Vision 실패 → 이미지 임베딩 건너뜀")
            return None

        logger.info(f"Gemini 이미지 설명: {caption[:80]}...")

        # Step 2. CLIP 텍스트 임베딩 (512d)
        return await self._clip_text_embedding(caption)

    async def _describe_image_gemini(self, image_bytes: bytes) -> Optional[str]:
        """Gemini Vision API로 이미지를 패션 설명 텍스트로 변환"""
        if not self._settings.GEMINI_API_KEY:
            logger.warning("GEMINI_API_KEY 없음 → 이미지 설명 생성 불가")
            return None

        img_b64 = base64.b64encode(image_bytes).decode()

        # MIME 타입 추론
        mime = "image/jpeg"
        if image_bytes[:4] == b"\x89PNG":
            mime = "image/png"
        elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            mime = "image/webp"

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-1.5-flash:generateContent"
        )
        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": mime, "data": img_b64}},
                    {"text": (
                        "Describe this fashion item concisely in English for a search query. "
                        "Include: clothing type, color, style, material if visible. "
                        "Max 2 sentences."
                    )}
                ]
            }],
            "generationConfig": {"maxOutputTokens": 100, "temperature": 0.2}
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=payload, params={"key": self._settings.GEMINI_API_KEY})
                resp.raise_for_status()
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
        except httpx.TimeoutException:
            logger.warning("Gemini Vision 타임아웃")
        except Exception as e:
            logger.error(f"Gemini Vision 실패: {e}")

        return None

    async def _clip_text_embedding(self, text: str) -> Optional[list[float]]:
        """huggingface_hub InferenceClient로 CLIP 텍스트 임베딩 (512d)"""
        try:
            import asyncio
            from huggingface_hub import InferenceClient

            def _call():
                hf_client = InferenceClient(token=self._settings.HF_TOKEN)
                result = hf_client.feature_extraction(
                    text,
                    model=self._settings.HF_CLIP_MODEL,
                )
                import numpy as np
                arr = np.array(result)
                if arr.ndim > 1:
                    arr = arr[0]
                return arr.tolist()

            vec = await asyncio.to_thread(_call)
            return self._l2_normalize(vec)

        except Exception as e:
            logger.error(f"CLIP 텍스트 임베딩 실패: {e}")
            return None

    # ──────────────────────────────────────
    # 텍스트 임베딩 (768d Gemini)
    # ──────────────────────────────────────
    async def encode_text(self, text: str) -> Optional[list[float]]:
        """Gemini text-embedding-004 모델로 텍스트 임베딩 벡터(768d) 생성"""
        if not self._settings.GEMINI_API_KEY:
            logger.warning("GEMINI_API_KEY가 설정되지 않았습니다")
            return None

        if not text or not text.strip():
            return None

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._settings.GEMINI_EMBED_MODEL}:embedContent"
        )
        payload = {
            "model": f"models/{self._settings.GEMINI_EMBED_MODEL}",
            "content": {"parts": [{"text": text}]},
            "taskType": "SEMANTIC_SIMILARITY",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, params={"key": self._settings.GEMINI_API_KEY})
                resp.raise_for_status()
                data = resp.json()
                embedding = data.get("embedding", {}).get("values", [])
                if not embedding:
                    logger.warning(f"Gemini 텍스트 임베딩 빈 응답")
                    return None
                return self._l2_normalize(embedding)
        except Exception as e:
            logger.error(f"Gemini 텍스트 임베딩 실패: {e}")
            return None

    # ──────────────────────────────────────
    # 유틸리티
    # ──────────────────────────────────────
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
