"""
상품 검색 비즈니스 로직
- HuggingFace Space (/predict): YOLO 탐지 + Fashion-CLIP 임베딩을 단일 요청으로 수신
- pgvector HNSW 코사인 유사도 검색 (512d image_vector)
- Late Fusion RRF: 이미지 70% + 텍스트 30%
"""
import os
import logging
import asyncio
import httpx
from typing import Optional

from ..database import get_pg_cursor
from ..config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# HuggingFace Space URL (.env의 HF_SPACE_URL 또는 기본값)
# 예: https://daniel0708-lookalike-yolo.hf.space
HF_SPACE_BASE = os.getenv("HF_SPACE_URL", "").rstrip("/")
HF_SPACE_TOKEN = os.getenv("HF_SPACE_TOKEN", "")


class SearchService:
    """
    유사 상품 검색 비즈니스 로직.
    HuggingFace Space → 임베딩 수신 → pgvector HNSW 검색.
    """

    # ──────────────────────────────────────
    # 공개 검색 진입점
    # ──────────────────────────────────────
    async def search_products(
        self,
        image_vector: Optional[list[float]] = None,
        text_vector: Optional[list[float]] = None,
        category: Optional[str] = None,
        gender: Optional[str] = None,
        limit: int = 6,
    ) -> list:
        """
        라우터에서 넘겨받은 임베딩 벡터로 pgvector HNSW 검색 실행.
        벡터가 없으면 DB 랜덤 fallback.
        """
        has_image = image_vector is not None and len(image_vector) > 0
        has_text = text_vector is not None and len(text_vector) > 0

        if has_image or has_text:
            try:
                results = self._vector_search(
                    image_vector=image_vector if has_image else None,
                    text_vector=text_vector if has_text else None,
                    category=category,
                    gender=gender,
                    limit=limit,
                )
                if results:
                    return results
            except Exception as e:
                logger.error(f"벡터 검색 실패: {e}")

        return self._search_by_db(category=category, gender=gender, limit=limit)

    # ──────────────────────────────────────
    # HuggingFace Space 호출
    # ──────────────────────────────────────
    async def call_hf_space_predict(self, image_bytes: bytes) -> dict:
        """
        Space POST /predict → { boxes, embedding(512d), label, category }

        반환 구조:
            {
                "embedding": list[float] | None,   # Fashion-CLIP 512d
                "boxes": list[dict],
                "label": str,
                "category": str | None,
            }
        """
        if not HF_SPACE_BASE:
            logger.warning("HF_SPACE_URL 미설정 → Space 호출 건너뜀")
            return {"embedding": None, "boxes": [], "label": "unknown", "category": None}

        headers = {}
        if HF_SPACE_TOKEN:
            headers["Authorization"] = f"Bearer {HF_SPACE_TOKEN}"

        url = f"{HF_SPACE_BASE}/predict"
        files = {"image": ("image.jpg", image_bytes, "image/jpeg")}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=headers, files=files)
                resp.raise_for_status()
                data = resp.json()
                logger.info(f"Space 응답: label={data.get('label')}, "
                            f"boxes={len(data.get('boxes', []))}, "
                            f"embedding={'있음' if data.get('embedding') else '없음'}")
                return data
        except httpx.TimeoutException:
            logger.warning("HF Space 타임아웃 (콜드스타트 가능) → 재시도 없이 None 반환")
            return {"embedding": None, "boxes": [], "label": "unknown", "category": None}
        except Exception as e:
            logger.error(f"HF Space /predict 호출 실패: {e}")
            return {"embedding": None, "boxes": [], "label": "unknown", "category": None}

    # ──────────────────────────────────────
    # YOLO 전용 탐지 (레거시 호환)
    # ──────────────────────────────────────
    async def detect_objects_hf(self, image_bytes: bytes) -> list[dict]:
        """
        /predict 응답에서 boxes 부분만 추출하여 반환.
        (라우터에서 YOLO 결과만 필요한 경우용)
        """
        result = await self.call_hf_space_predict(image_bytes)
        boxes = result.get("boxes", [])
        if not boxes:
            return [{"label": "full_image", "box": [0, 0, 1000, 1000]}]

        return [
            {"label": b["label"], "box": [b["x1"], b["y1"], b["x2"], b["y2"]]}
            for b in boxes
        ]

    # ──────────────────────────────────────
    # pgvector 코사인 유사도 검색
    # ──────────────────────────────────────
    def _vector_search(
        self,
        image_vector: Optional[list[float]] = None,
        text_vector: Optional[list[float]] = None,
        category: Optional[str] = None,
        gender: Optional[str] = None,
        limit: int = 6,
    ) -> list:
        image_results = {}
        text_results = {}

        if image_vector:
            image_results = self._knn_search_pg(
                vector=image_vector,
                vector_column="image_vector",
                category=category,
                gender=gender,
                limit=limit * 3,
            )

        if text_vector:
            text_results = self._knn_search_pg(
                vector=text_vector,
                vector_column="text_vector",
                category=category,
                gender=gender,
                limit=limit * 3,
            )

        # Late Fusion (RRF)
        if image_results and text_results:
            fused = self._rrf_fusion(image_results, text_results)
        elif image_results:
            fused = image_results
        elif text_results:
            fused = text_results
        else:
            return []

        return self._hydrate_from_db(fused, source="vector", category=category, gender=gender, limit=limit)

    def _knn_search_pg(
        self,
        vector: list[float],
        vector_column: str,
        category: Optional[str] = None,
        gender: Optional[str] = None,
        limit: int = 18,
    ) -> dict:
        """<=> 연산자로 HNSW 인덱스 스캔, 1-거리로 스코어 산출"""
        try:
            with get_pg_cursor() as cur:
                conditions = [f"e.{vector_column} IS NOT NULL"]
                params: list = []

                if gender:
                    conditions.append("e.gender = %s")
                    params.append(gender.lower())
                if category:
                    cat_vals = self._category_filter_values(category)
                    if cat_vals:
                        ph = ",".join(["%s"] * len(cat_vals))
                        conditions.append(f"LOWER(e.category) IN ({ph})")
                        params.extend(cat_vals)

                where = " AND ".join(conditions)
                vec_str = "[" + ",".join(str(v) for v in vector) + "]"

                cur.execute(
                    f"""
                    SELECT
                        e.product_id,
                        1 - (e.{vector_column} <=> %s::vector) AS score
                    FROM product_embeddings e
                    WHERE {where}
                    ORDER BY e.{vector_column} <=> %s::vector ASC
                    LIMIT %s
                    """,
                    [vec_str] + params + [vec_str, limit],
                )

                return {str(row["product_id"]): float(row["score"]) for row in cur.fetchall()}

        except Exception as e:
            logger.error(f"pgvector 검색 실패 ({vector_column}): {e}", exc_info=True)
            return {}

    @staticmethod
    def _rrf_fusion(image_scores: dict, text_scores: dict, k: int = 60) -> dict:
        """RRF 병합 (이미지 70% + 텍스트 30%)"""
        all_ids = set(image_scores.keys()) | set(text_scores.keys())
        img_ranked = {pid: r for r, pid in enumerate(sorted(image_scores, key=image_scores.get, reverse=True), 1)}
        txt_ranked = {pid: r for r, pid in enumerate(sorted(text_scores, key=text_scores.get, reverse=True), 1)}

        fused = {}
        for pid in all_ids:
            img_r = img_ranked.get(pid, len(image_scores) + 50)
            txt_r = txt_ranked.get(pid, len(text_scores) + 50)
            fused[pid] = 0.7 * (1.0 / (k + img_r)) + 0.3 * (1.0 / (k + txt_r))

        return fused

    def _hydrate_from_db(self, product_scores: dict, source: str, category, gender, limit: int) -> list:
        """조회된 상품 ID → DB에서 상세 정보 반환"""
        if not product_scores:
            return []

        product_ids = list(product_scores.keys())

        try:
            with get_pg_cursor() as cur:
                ph = ",".join(["%s"] * len(product_ids))
                cur.execute(
                    f"""
                    SELECT
                        p.product_id, p.prod_name, p.brand_name,
                        p.base_price, p.img_url, p.category_code, p.origin_url,
                        COALESCE(np.naver_price, p.base_price) AS lowest_price,
                        np.mall_name, np.mall_url
                    FROM products p
                    LEFT JOIN naver_prices np ON p.product_id = np.product_id AND np.rank = 1
                    WHERE p.product_id::text IN ({ph})
                    """,
                    tuple(product_ids),
                )
                rows = cur.fetchall()

                products = []
                for row in rows:
                    pid = str(row["product_id"])
                    img_url = row["img_url"] or ""
                    
                    # Fallback URL 생성 (공용 로직 사용)
                    local_url = self.get_local_fallback_url(img_url)

                    products.append({
                        "product_id": pid,
                        "product_name": row["prod_name"] or "상품명 없음",
                        "brand": row["brand_name"] or "브랜드 없음",
                        "price": row["lowest_price"] or 0,
                        "image_url": img_url or "https://placehold.co/300x300?text=No+Image",
                        "local_url": local_url,
                        "mall_name": row["mall_name"] or row["brand_name"] or "공식몰",
                        "mall_url": row["mall_url"] or row["origin_url"] or "#",
                        "similarity_score": round(product_scores.get(pid, 0.0), 4),
                        "search_source": source,
                    })

            products.sort(key=lambda x: x["similarity_score"] or 0.0, reverse=True)
            return products[:limit]

        except Exception as e:
            logger.error(f"DB Hydration 실패: {e}")
            return []

    @staticmethod
    def get_local_fallback_url(img_url: str) -> Optional[str]:
        """Cloudinary URL 또는 원본 경로에서 로컬 Fallback URL (/raw/...) 생성"""
        if not img_url:
            return None
            
        if img_url.startswith("http"):
            # Cloudinary URL에서 정보 추출 (products/brand/image/filename.webp)
            try:
                # 클라우디너리 URL에서 파일명 추출 (마지막 슬래시 이후)
                filename = img_url.split("/")[-1].replace(".webp", ".jpg")
                
                # 파일명에서 브랜드 추출 (예: musinsa_men_top_abc.jpg)
                if "_" in filename:
                    brand = filename.split("_")[0]
                    # 브랜드명 예외 처리
                    if brand == "8세컨즈": brand = "8seconds"
                    return f"/raw/{brand}/image/{filename}"
                
                return f"/raw/{filename}"
            except Exception:
                pass
            return None
        else:
            # 이미 로컬 경로인 경우 (/raw/brand/image/file.jpg)
            if img_url.startswith("/raw/"):
                return img_url
                
            # 파일명만 있는 경우 (예: 8seconds_men_top_abc.jpg)
            clean_path = img_url.lstrip("./").lstrip("/")
            if "_" in clean_path:
                brand = clean_path.split("_")[0]
                return f"/raw/{brand}/image/{clean_path}"
                
            return f"/raw/{clean_path}"

    def _search_by_db(self, category, gender, limit: int) -> list:
        """Fallback: 랜덤 상품 반환"""
        try:
            with get_pg_cursor() as cur:
                conditions, params = [], []
                if gender:
                    conditions.append("p.gender = %s"); params.append(gender.lower())
                if category:
                    cat_vals = self._category_filter_values(category)
                    if cat_vals:
                        conditions.append(f"LOWER(p.category_code) IN ({','.join(['%s']*len(cat_vals))})")
                        params.extend(cat_vals)

                where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
                cur.execute(
                    f"""
                    SELECT p.product_id, p.prod_name, p.brand_name, p.base_price, p.img_url, p.origin_url,
                           COALESCE(np.naver_price, p.base_price) AS lowest_price,
                           np.mall_name, np.mall_url
                    FROM products p
                    LEFT JOIN naver_prices np ON p.product_id = np.product_id AND np.rank = 1
                    {where} ORDER BY RANDOM() LIMIT %s
                    """,
                    params + [limit],
                )
                return [{
                    "product_id": str(r["product_id"]), "product_name": r["prod_name"] or "상품명 없음",
                    "brand": r["brand_name"] or "브랜드 없음", "price": r["lowest_price"] or 0,
                    "image_url": r["img_url"] or "https://placehold.co/300x300?text=No+Image",
                    "local_url": self.get_local_fallback_url(r["img_url"] or ""),
                    "mall_name": r["mall_name"] or r["brand_name"] or "공식몰",
                    "mall_url": r["mall_url"] or r["origin_url"] or "#",
                    "similarity_score": None, "search_source": "db_fallback",
                } for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"DB fallback 검색 실패: {e}")
            return []

    def _category_filter_values(self, category: str) -> list[str]:
        key = (category or "").strip().lower()
        if not key:
            return []
        category_map = {
            "top": ["top", "상의"], "bottom": ["bottom", "하의"],
            "outer": ["outer", "아우터"],
        }
        return category_map.get(key, [key])


search_service = SearchService()
