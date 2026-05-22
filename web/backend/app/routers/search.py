"""
상품 검색 라우터 (Render 배포용)
- ML Engine → HuggingFace/Gemini 외부 API
- HDFS → Supabase Storage
- Redis 세션 → DB 세션
"""
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form, Request
from typing import Optional
import logging
import traceback

from ..database import get_pg_cursor, get_session
from ..models.search import (
    SearchByTextRequest, SearchResultResponse, SimilarProductResponse,
    SearchLogResponse, ImageSearchResponse, ProductResult,
    SearchHistoryItem, SearchHistoryListResponse,
)
from ..services.embedding_service import embedding_service
from ..services.storage_service import storage_service
from ..services.search_service import search_service
from ..config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/search", tags=["검색"])


def _get_user_from_session(request: Request) -> Optional[dict]:
    token = request.cookies.get("session_token")
    return get_session(token, is_admin=False) if token else None


@router.post("/by-image", response_model=ImageSearchResponse)
async def search_by_image(
    request: Request,
    image: Optional[UploadFile] = File(None),
    search_text: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
):
    """외부 API 기반 이미지+텍스트 복합 검색"""
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    logger.info(f"[search_by_image] user_id: {user_id}, search_text: {search_text}")

    if not image and not search_text:
        raise HTTPException(status_code=400, detail="이미지 또는 검색어를 입력해주세요")

    try:
        thumb_url, file_size, img_w, img_h = None, None, None, None
        image_vector, text_vector = None, None

        if image:
            storage_service.validate_image_file(image)
            image_bytes = await image.read()
            if user_id:
                try:
                    t = await storage_service.create_thumbnail(image_bytes, str(user_id))
                    thumb_url, file_size = t["thumbnail_url"], t["file_size"]
                    img_w, img_h = t["width"], t["height"]
                except Exception as e:
                    logger.warning(f"썸네일 실패: {e}")

            # HF Space /predict: YOLO 탐지 + Fashion-CLIP 임베딩 동시 수신
            space_result = await search_service.call_hf_space_predict(image_bytes)
            image_vector = space_result.get("embedding")  # 512d list[float] or None
            detected_category = space_result.get("category")  # YOLO에서 탐지된 카테고리

            # YOLO가 카테고리를 탐지했는데 사용자가 직접 지정하지 않은 경우 자동 적용
            if not category and detected_category:
                category = detected_category
                logger.info(f"YOLO 카테고리 자동 적용: {category}")

            if image_vector:
                logger.info(f"✅ Fashion-CLIP 임베딩 수신 (dim={len(image_vector)})")
            else:
                logger.warning("⚠️ HF Space 임베딩 없음 → 텍스트 검색만 진행")

        if search_text:
            text_vector = await embedding_service.encode_text(search_text)
            if text_vector:
                logger.info(f"✅ 텍스트 임베딩 생성 (dim={len(text_vector)})")
            else:
                logger.warning("⚠️ 텍스트 임베딩 생성 실패")

        results = await search_service.search_products(
            image_vector=image_vector,
            text_vector=text_vector,
            category=category,
            gender=gender,
            limit=6,
        )

        gender_f, category_f = gender, category
        if category and "_" in category and not gender:
            parts = category.split("_", 1)
            gender_f, category_f = parts[0], parts[1]

        log_id = None
        try:
            with get_pg_cursor() as cur:
                cur.execute(
                    """INSERT INTO search_logs (user_id, thumbnail_url, input_text, applied_category,
                       gender, image_size, image_width, image_height, search_status, result_count)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'completed',%s) RETURNING log_id""",
                    (user_id, thumb_url, search_text, category_f, gender_f,
                     file_size, img_w, img_h, len(results)),
                )
                log_row = cur.fetchone()
                if log_row:
                    log_id = log_row["log_id"]
        except Exception as e:
            logger.error(f"[search_by_image] 검색 로그 저장 실패: {e}")

        if log_id:
            try:
                with get_pg_cursor() as cur:
                    for rank, item in enumerate(results, 1):
                        cur.execute(
                            """INSERT INTO search_results (log_id, product_name, brand, price,
                               image_url, mall_name, mall_url, rank)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (log_id, item["product_name"], item["brand"], item["price"],
                             item["image_url"], item["mall_name"], item["mall_url"], rank),
                        )
            except Exception as e:
                logger.warning(f"결과 저장 실패: {e}")

        src = results[0]["search_source"] if results else "db"
        return ImageSearchResponse(
            success=True, log_id=log_id, thumbnail_url=thumb_url,
            results=[ProductResult(
                product_id=str(r["product_id"]), product_name=r["product_name"],
                brand=r["brand"], price=r["price"], image_url=r["image_url"],
                mall_name=r["mall_name"], mall_url=r["mall_url"],
                similarity_score=r.get("similarity_score"),
                search_source=r.get("search_source", "db"),
            ) for r in results],
            result_count=len(results), search_source=src,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"이미지 검색 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="서버 오류")


@router.get("/history", response_model=SearchHistoryListResponse)
async def get_search_history(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    with get_pg_cursor() as cur:
        cur.execute("SELECT COUNT(*) as cnt FROM search_logs WHERE user_id=%s", (user_id,))
        total = cur.fetchone()["cnt"]
        cur.execute("""SELECT log_id, thumbnail_url, input_text, applied_category, gender, create_dt, result_count
                       FROM search_logs WHERE user_id=%s ORDER BY create_dt DESC LIMIT %s OFFSET %s""",
                    (user_id, limit, offset))
        rows = cur.fetchall()
    history = []
    for r in rows:
        thumb_url = r["thumbnail_url"]
        item = SearchHistoryItem(
            log_id=r["log_id"],
            thumbnail_url=thumb_url,
            local_thumbnail_url=search_service.get_local_fallback_url(thumb_url),
            search_text=r["input_text"],
            category=r["applied_category"],
            gender=r["gender"],
            create_dt=r["create_dt"],
            result_count=r["result_count"] or 0
        )
        history.append(item)
    return SearchHistoryListResponse(success=True, total=total, page=offset//limit+1, limit=limit, history=history)


@router.get("/history/{log_id}")
async def get_search_history_detail(log_id: int, request: Request):
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    with get_pg_cursor() as cur:
        cur.execute("SELECT * FROM search_logs WHERE log_id=%s", (log_id,))
        log_row = cur.fetchone()
        if not log_row:
            raise HTTPException(status_code=404, detail="검색 기록 없음")
        if log_row["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="접근 권한 없음")
        cur.execute("SELECT * FROM search_results WHERE log_id=%s ORDER BY rank ASC", (log_id,))
        result_rows = cur.fetchall()
        
    results = []
    for r in result_rows:
        p_dict = dict(r)
        p_dict["local_url"] = search_service.get_local_fallback_url(p_dict.get("image_url"))
        results.append(p_dict)

    return {
        "success": True, 
        "log_id": log_row["log_id"], 
        "thumbnail_url": log_row.get("thumbnail_url"),
        "local_thumbnail_url": search_service.get_local_fallback_url(log_row.get("thumbnail_url")),
        "search_text": log_row["input_text"], 
        "category": log_row["applied_category"],
        "gender": log_row["gender"], 
        "create_dt": log_row["create_dt"].isoformat() if log_row["create_dt"] else None,
        "results": results
    }


@router.delete("/history")
async def delete_all_search_history(request: Request):
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    with get_pg_cursor() as cur:
        cur.execute("DELETE FROM search_logs WHERE user_id=%s", (user_id,))
        deleted = cur.rowcount
    return {"success": True, "message": f"{deleted}건 삭제됨", "deleted_count": deleted}


@router.post("/by-text", response_model=SearchResultResponse)
async def search_by_text(request: Request, req: SearchByTextRequest):
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    logger.info(f"[search_by_text] user_id: {user_id}, query: {req.query}")

    with get_pg_cursor() as cur:
        cond = ["prod_name ILIKE %s"]
        params = [f"%{req.query}%"]
        if req.gender:
            cond.append("gender=%s"); params.append(req.gender.lower())
        if req.category:
            cv = search_service._category_filter_values(req.category)
            if cv:
                cond.append(f"LOWER(category_code) IN ({','.join(['%s']*len(cv))})")
                params.extend(cv)
        params.append(req.top_k)
        cur.execute(f"SELECT product_id, prod_name, base_price, img_url, brand_name, origin_url FROM products WHERE {' AND '.join(cond)} ORDER BY product_id DESC LIMIT %s", tuple(params))
        rows = cur.fetchall()
        
    results = []
    for r in rows:
        img_url = r["img_url"]
        results.append(SimilarProductResponse(
            product_id=str(r["product_id"]),
            prod_name=r["prod_name"],
            base_price=r["base_price"],
            img_url=img_url,
            local_url=search_service.get_local_fallback_url(img_url)
        ))

    # 검색 로그 저장 (텍스트 검색)
    try:
        with get_pg_cursor() as cur:
            cur.execute(
                """INSERT INTO search_logs (user_id, input_text, applied_category,
                   gender, search_status, result_count)
                   VALUES (%s,%s,%s,%s,'completed',%s) RETURNING log_id""",
                (user_id, req.query, req.category, req.gender, len(results)),
            )
            log_row = cur.fetchone()
            if log_row:
                log_id = log_row["log_id"]
                for rank, r in enumerate(rows[:20], 1):
                    cur.execute(
                        """INSERT INTO search_results (log_id, product_name, brand, price,
                           image_url, mall_name, mall_url, rank)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (log_id, r["prod_name"] or "상품명 없음", r["brand_name"] or "브랜드 없음", r["base_price"] or 0,
                         r["img_url"] or "https://placehold.co/300x300?text=No+Image", r["brand_name"] or "공식몰", r["origin_url"] or "#", rank),
                    )

    except Exception as e:
        logger.error(f"[search_by_text] 텍스트 검색 로그 저장 실패: {e}")

    return SearchResultResponse(results=results, total=len(results), query_type="text")


@router.get("/logs/{user_id}", response_model=list[SearchLogResponse])
async def get_search_logs(user_id: str, limit: int = Query(20, ge=1, le=100)):
    with get_pg_cursor() as cur:
        cur.execute("SELECT log_id, user_id, input_img_url as input_img_path, input_text, applied_category, gender, create_dt FROM search_logs WHERE user_id=%s ORDER BY create_dt DESC LIMIT %s", (user_id, limit))
        rows = cur.fetchall()
    return [SearchLogResponse(**r) for r in rows]
