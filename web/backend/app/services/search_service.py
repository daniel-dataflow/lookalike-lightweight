"""
상품 검색 비즈니스 로직
- HuggingFace Space (/predict): YOLO 탐지 + Fashion-CLIP 임베딩을 단일 요청으로 수신
- pgvector HNSW 코사인 유사도 검색 (512d image_vector)
- Late Fusion RRF: 이미지 70% + 텍스트 30%
"""
import os
import io
import logging
import asyncio
import httpx
from typing import Optional

from ..database import get_pg_cursor
from ..config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# HF Space Client 싱글톤 캐시 (메모리 및 스레드 누수 방지)
_hf_client = None

# 프로젝트 루트 경로 (ml-models/backup/best.pt 절대경로 탐색용)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

# HuggingFace Space URL (config의 settings 사용)
# 예: https://daniel0708-lookalike-yolo.hf.space
HF_SPACE_BASE = settings.HF_SPACE_URL.rstrip("/") if settings.HF_SPACE_URL else ""
HF_SPACE_TOKEN = settings.HF_SPACE_TOKEN or ""


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
        search_text: Optional[str] = None,
        category: Optional[str] = None,
        gender: Optional[str] = None,
        limit: int = 6,
    ) -> list:
        """
        3단계 하이브리드 검색 체인 가동:
        1. 1순위: pgvector 의미론적(Semantic) 임베딩 검색 실행 (추상적 검색어 수용)
        2. 2순위: 임베딩 매칭 실패 또는 결과 미비 시, p.prod_name 키워드(LIKE/ILIKE) 검색으로 보완
        3. 3순위: 최종 Fallback으로 카테고리/성별 기반 무작위 랜덤 매칭
        """
        has_image = image_vector is not None and len(image_vector) > 0
        has_text = text_vector is not None and len(text_vector) > 0

        # ── [1순위] pgvector 의미론적 임베딩 검색 ──
        if has_image or has_text:
            try:
                results = self._vector_search(
                    image_vector=image_vector if has_image else None,
                    text_vector=text_vector if has_text else None,
                    search_text=search_text,
                    category=category,
                    gender=gender,
                    limit=limit,
                )
                if results:
                    logger.info(f"✅ [1순위] pgvector 임베딩 의미 검색 성공: {len(results)}개 상품")
                    return results
            except Exception as e:
                logger.error(f"❌ [1순위] pgvector 임베딩 검색 실패: {e}")

        # ── [2순위] 상품명 직접 키워드(ILIKE) 검색 (임베딩 검색 실패 또는 결과가 없을 때) ──
        if search_text and search_text.strip():
            try:
                results = self._search_products_by_keyword(search_text, category, gender, limit)
                if results:
                    logger.info(f"✅ [2순위] products 직접 키워드(ILIKE) 검색 성공: {len(results)}개 상품")
                    return results
            except Exception as e:
                logger.error(f"❌ [2순위] 키워드 검색 실패: {e}")

        # ── [3순위] 최종 Fallback: 카테고리/성별 기반 랜덤 검색 ──
        logger.info("⚠️ [3순위] 최종 Fallback: DB 카테고리 랜덤 검색 진행")
        return self._search_by_db(category=category, gender=gender, limit=limit)

    # ──────────────────────────────────────
    # HuggingFace Space 호출
    # ──────────────────────────────────────
    async def call_hf_space_predict(self, image_bytes: bytes) -> dict:
        """
        YOLO 탐지 + Fashion-CLIP 임베딩을 수신합니다.
        1. settings.USE_LOCAL_ML이 활성화된 경우 로컬 ML 모델(YOLOv8 + Fashion-CLIP)로 직접 추론을 시도합니다.
        2. 로컬 ML 로딩 실패 혹은 예외가 발생할 경우 기존 HuggingFace Space 외부 API 호출로 자동 Fallback합니다.

        반환 구조:
            {
                "embedding": list[float] | None,  # 512d CLIP 벡터
                "boxes":     list[dict],
                "label":     str,
                "category":  str | None,
                "status":    "success" | "error",
            }
        """
        # ── 원격 HF Space 호출 ───────────────────────────
        if not HF_SPACE_BASE:
            logger.warning("HF_SPACE_URL 미설정 → 이미지 임베딩 불가")
            return {"embedding": None, "boxes": [], "label": "unknown", "category": None}

        import tempfile

        try:
            from gradio_client import Client, handle_file

            # ── 이미지 bytes → 임시 파일 ────────────────────
            # MIME 타입에 따라 확장자 결정
            if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                suffix = ".png"
            elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
                suffix = ".webp"
            else:
                suffix = ".jpg"

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(image_bytes)
                tmp_path = tmp.name

            try:
                # ── gradio_client로 HF Space 호출 ───────────
                # handle_file(): gradio_client v1.x 표준 이미지 전달 방식
                # HF Space 콜드스타트 대비 타임아웃을 넉넉히 설정
                def _predict():
                    global _hf_client
                    if _hf_client is None:
                        logger.info("🔄 [Singleton] HF Space Client 최초 연결 생성 중...")
                        _hf_client = Client(HF_SPACE_BASE)
                    
                    return _hf_client.predict(
                        image=handle_file(tmp_path),
                        api_name="/predict",
                    )

                result = await asyncio.to_thread(_predict)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

            # ── 응답 파싱 ──────────────────────────────────
            if not isinstance(result, dict):
                logger.warning(f"HF Space 응답 타입 이상: {type(result)}")
                return {"embedding": None, "boxes": [], "label": "unknown", "category": None}

            status = result.get("status", "")
            if status == "error":
                logger.warning(
                    f"⚠️ HF Space 내부 오류: {result.get('error_message', 'unknown')}"
                )
                return {"embedding": None, "boxes": [], "label": "unknown", "category": None}

            embedding = result.get("embedding")
            dim = len(embedding) if embedding else 0
            logger.info(f"✅ HF Space 임베딩 수신 성공 (dim={dim})")
            return result

        except Exception as e:
            logger.error(f"⚠️ HF Space 호출 실패: {type(e).__name__}: {e}")

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
        search_text: Optional[str] = None,
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
                search_text=search_text,
                category=category,
                gender=gender,
                limit=limit * 3,
            )

        if text_vector:
            text_results = self._knn_search_pg(
                vector=text_vector,
                vector_column="text_vector",
                search_text=search_text,
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
        search_text: Optional[str] = None,
        category: Optional[str] = None,
        gender: Optional[str] = None,
        limit: int = 18,
    ) -> dict:
        """<=> 연산자로 HNSW 인덱스 스캔, 1-거리로 스코어 산출 + 하이브리드 키워드 매칭"""
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

                # 하이브리드 키워드 매칭 필터 추가
                if search_text and search_text.strip():
                    kw = search_text.strip().lower()
                    
                    # 대표적인 한글 패션 카테고리 동의어 매핑
                    synonyms = {
                        "바지": ["바지", "팬츠", "pants", "슬랙스", "slacks", "데님", "denim", "청바지", "조거"],
                        "팬츠": ["바지", "팬츠", "pants", "슬랙스", "slacks", "데님", "denim", "청바지", "조거"],
                        "청바지": ["데님", "denim", "청바지", "바지", "팬츠", "pants"],
                        "셔츠": ["셔츠", "shirts", "남방", "셔츠"],
                        "티셔츠": ["티셔츠", "t-shirt", "반팔", "긴팔", "맨투맨", "스웨트"],
                        "맨투맨": ["맨투맨", "스웨트", "sweatshirt"],
                        "후드": ["후드", "hoodie"],
                        "자켓": ["재킷", "자켓", "jacket", "바람막이", "블레이저", "blazer"],
                        "재킷": ["재킷", "자켓", "jacket", "바람막이", "블레이저", "blazer"],
                        "바람막이": ["바람막이", "윈드브레이커", "windbreaker"],
                        "블레이저": ["블레이저", "blazer", "재킷", "자켓"],
                        "코트": ["코트", "coat"],
                        "패딩": ["패딩", "다운", "down", "푸퍼", "puffer", "점퍼"],
                        "점퍼": ["점퍼", "jumper", "패딩", "재킷", "자켓"]
                    }
                    
                    words = kw.split()
                    kw_conditions = []
                    
                    for w in words:
                        w_matched = False
                        for syn_key, syn_list in synonyms.items():
                            if syn_key in w or w in syn_key:
                                syn_conds = []
                                for s_val in syn_list:
                                    syn_conds.append("p.prod_name ILIKE %s")
                                    params.append(f"%{s_val}%")
                                kw_conditions.append(f"({' OR '.join(syn_conds)})")
                                w_matched = True
                                break
                        
                        if not w_matched and len(w) >= 1:
                            kw_conditions.append("(p.prod_name ILIKE %s OR p.brand_name ILIKE %s)")
                            params.append(f"%{w}%")
                            params.append(f"%{w}%")
                            
                    if kw_conditions:
                        conditions.append(" AND ".join(kw_conditions))

                where = " AND ".join(conditions)
                vec_str = "[" + ",".join(str(v) for v in vector) + "]"

                cur.execute(
                    f"""
                    SELECT
                        e.product_id,
                        1 - (e.{vector_column} <=> %s::vector) AS score
                    FROM product_embeddings e
                    JOIN products p ON e.product_id = p.product_id
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
        """로컬 우회를 완전히 제거하고 실제 저장된 URL(Cloudinary)을 그대로 반환합니다."""
        return img_url

    def _search_products_by_keyword(self, search_text: str, category: Optional[str], gender: Optional[str], limit: int) -> list:
        """products 테이블 직접 ILIKE 키워드 검색 및 동의어 필터링 적용"""
        try:
            with get_pg_cursor() as cur:
                conditions = []
                params = []

                if gender:
                    conditions.append("p.gender = %s")
                    params.append(gender.lower())
                
                if category:
                    cat_vals = self._category_filter_values(category)
                    if cat_vals:
                        ph = ",".join(["%s"] * len(cat_vals))
                        conditions.append(f"LOWER(p.category_code) IN ({ph})")
                        params.extend(cat_vals)

                kw = search_text.strip().lower()
                
                # 대표적인 한글 패션 카테고리 동의어 매핑
                synonyms = {
                    "바지": ["바지", "팬츠", "pants", "슬랙스", "slacks", "데님", "denim", "청바지", "조거", "하의"],
                    "팬츠": ["바지", "팬츠", "pants", "슬랙스", "slacks", "데님", "denim", "청바지", "조거", "하의"],
                    "청바지": ["데님", "denim", "청바지", "바지", "팬츠", "pants"],
                    "셔츠": ["셔츠", "shirts", "남방", "셔츠", "상의"],
                    "티셔츠": ["티셔츠", "t-shirt", "반팔", "긴팔", "맨투맨", "스웨트", "상의"],
                    "맨투맨": ["맨투맨", "스웨트", "sweatshirt"],
                    "후드": ["후드", "hoodie"],
                    "자켓": ["재킷", "자켓", "jacket", "바람막이", "블레이저", "blazer", "아우터"],
                    "재킷": ["재킷", "자켓", "jacket", "바람막이", "블레이저", "blazer", "아우터"],
                    "바람막이": ["바람막이", "윈드브레이커", "windbreaker"],
                    "블레이저": ["블레이저", "blazer", "재킷", "자켓"],
                    "코트": ["코트", "coat", "아우터"],
                    "패딩": ["패딩", "다운", "down", "푸퍼", "puffer", "점퍼", "아우터"],
                    "점퍼": ["점퍼", "jumper", "패딩", "재킷", "자켓", "아우터"]
                }
                
                words = kw.split()
                kw_conditions = []
                
                for w in words:
                    w_matched = False
                    for syn_key, syn_list in synonyms.items():
                        if syn_key in w or w in syn_key:
                            syn_conds = []
                            for s_val in syn_list:
                                syn_conds.append("p.prod_name ILIKE %s")
                                params.append(f"%{s_val}%")
                            kw_conditions.append(f"({' OR '.join(syn_conds)})")
                            w_matched = True
                            break
                    
                    if not w_matched and len(w) >= 1:
                        kw_conditions.append("(p.prod_name ILIKE %s OR p.brand_name ILIKE %s)")
                        params.append(f"%{w}%")
                        params.append(f"%{w}%")
                        
                if kw_conditions:
                    conditions.append(" AND ".join(kw_conditions))

                where_clause = ""
                if conditions:
                    where_clause = "WHERE " + " AND ".join(conditions)

                cur.execute(
                    f"""
                    SELECT
                        p.product_id, p.prod_name, p.brand_name,
                        p.base_price, p.img_url, p.category_code, p.origin_url,
                        COALESCE(np.naver_price, p.base_price) AS lowest_price,
                        np.mall_name, np.mall_url
                    FROM products p
                    LEFT JOIN naver_prices np ON p.product_id = np.product_id AND np.rank = 1
                    {where_clause}
                    ORDER BY p.update_dt DESC
                    LIMIT %s
                    """,
                    params + [limit],
                )
                
                rows = cur.fetchall()
                products = []
                for row in rows:
                    pid = str(row["product_id"])
                    img_url = row["img_url"] or ""
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
                        "similarity_score": 0.99,  # 정확한 키워드 매칭 스코어 표기
                        "search_source": "db_keyword_match",
                    })
                
                logger.info(f"✅ products 직접 키워드 검색 성공: {len(products)}개 결과 반환")
                return products
                
        except Exception as e:
            logger.error(f"❌ products 직접 키워드 검색 실패: {e}")
            
        return []

    def _search_by_db(self, category, gender, limit: int) -> list:
        """Fallback: 카테고리/성별 기반 검색 + similarity score 할당"""
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
                
                # 카테고리 매칭 점수: 조건 만족 정도에 따라 0.5~0.7 할당
                has_category = category is not None
                has_gender = gender is not None
                base_score = 0.5 + (0.1 if has_category else 0) + (0.1 if has_gender else 0)
                
                return [{
                    "product_id": str(r["product_id"]), 
                    "product_name": r["prod_name"] or "상품명 없음",
                    "brand": r["brand_name"] or "브랜드 없음", 
                    "price": r["lowest_price"] or 0,
                    "image_url": r["img_url"] or "https://placehold.co/300x300?text=No+Image",
                    "local_url": self.get_local_fallback_url(r["img_url"] or ""),
                    "mall_name": r["mall_name"] or r["brand_name"] or "공식몰",
                    "mall_url": r["mall_url"] or r["origin_url"] or "#",
                    "similarity_score": round(base_score, 2),  # 0.5~0.7
                    "search_source": "db_category_match",
                } for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"DB fallback 검색 실패: {e}")
            return []

    def _category_filter_values(self, category: str) -> list[str]:
        key = (category or "").strip().lower()
        if not key:
            return []
        if key in ["top", "상의"]:
            return ["top", "상의"]
        elif key in ["bottom", "하의", "팬츠"]:
            return ["bottom", "하의"]
        elif key in ["outer", "아우터", "아우터(outer)"]:
            return ["outer", "아우터"]
        return [key]


search_service = SearchService()
