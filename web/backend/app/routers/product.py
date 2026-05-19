"""
상품 정보 라우터 (Render 배포용)
- MongoDB 제거 → product_features.detail_desc로 통합
- Redis 세션 → DB 세션
- img_hdfs_path → img_url
"""
from fastapi import APIRouter, HTTPException, Query, status, Request
import math
import logging
from typing import Optional

from ..database import get_pg_cursor, get_session
from ..models.product import (
    ProductCreateRequest, ProductResponse, ProductDetailResponse,
    ProductListResponse, NaverPriceResponse,
)
from ..services.search_service import SearchService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/products", tags=["상품"])
search_service = SearchService()


def _get_user_from_session(request: Request) -> Optional[dict]:
    token = request.cookies.get("session_token")
    return get_session(token, is_admin=False) if token else None


@router.get("/recent-views")
async def get_recent_views(request: Request, limit: int = 20):
    """최근 본 상품 조회"""
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    try:
        with get_pg_cursor() as cur:
            cur.execute("""
                SELECT p.product_id, p.prod_name, p.brand_name, p.base_price,
                       p.img_url, p.category_code,
                       COALESCE(np.naver_price, p.base_price) as lowest_price,
                       np.mall_name, rv.view_dt
                FROM recent_views rv
                JOIN products p ON rv.product_id = p.product_id
                LEFT JOIN naver_prices np ON p.product_id = np.product_id AND np.rank = 1
                WHERE rv.user_id = %s ORDER BY rv.view_dt DESC LIMIT %s
            """, (user_id, limit))
            rows = cur.fetchall()
        products = []
        for r in rows:
            p_dict = dict(r)
            img_url = p_dict.get("img_url")
            p_dict["local_url"] = search_service.get_local_fallback_url(img_url)
            logger.info(f"[get_recent_views] item: {p_dict.get('product_id')}, img: {img_url}, local: {p_dict['local_url']}")
            p_dict["product_id"] = str(p_dict["product_id"])
            if p_dict.get("view_dt"):
                p_dict["view_dt"] = p_dict["view_dt"].isoformat()
            products.append(p_dict)
            
        return {"success": True, "products": products}
    except Exception as e:
        logger.error(f"최근 본 상품 조회 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")


@router.get("/likes")
async def get_likes(request: Request, limit: int = 20):
    """좋아요 목록 조회"""
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    try:
        with get_pg_cursor() as cur:
            cur.execute("""
                SELECT p.product_id, p.prod_name, p.brand_name, p.base_price,
                       p.img_url, p.category_code,
                       COALESCE(np.naver_price, p.base_price) as lowest_price,
                       np.mall_name, l.create_dt
                FROM likes l
                JOIN products p ON l.product_id = p.product_id
                LEFT JOIN naver_prices np ON p.product_id = np.product_id AND np.rank = 1
                WHERE l.user_id = %s ORDER BY l.create_dt DESC LIMIT %s
            """, (user_id, limit))
            rows = cur.fetchall()
        products = []
        for r in rows:
            p_dict = dict(r)
            img_url = p_dict.get("img_url")
            p_dict["local_url"] = search_service.get_local_fallback_url(img_url)
            logger.info(f"[get_likes] item: {p_dict['product_id']}, img: {img_url}, local: {p_dict['local_url']}")
            p_dict["product_id"] = str(p_dict["product_id"])
            if p_dict.get("create_dt"):
                p_dict["create_dt"] = p_dict["create_dt"].isoformat()
            products.append(p_dict)
            
        return {"success": True, "products": products}
    except Exception as e:
        logger.error(f"좋아요 목록 조회 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")


@router.get("", response_model=ProductListResponse)
async def list_products(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    category: str = Query(None), keyword: str = Query(None),
):
    """상품 목록 (페이징)"""
    try:
        offset = (page - 1) * page_size
        cond, params = [], []
        if category:
            cond.append("category_code = %s"); params.append(category)
        if keyword:
            cond.append("prod_name ILIKE %s"); params.append(f"%{keyword}%")
        where = f"WHERE {' AND '.join(cond)}" if cond else ""
        with get_pg_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as cnt FROM products {where}", params)
            total = cur.fetchone()["cnt"]
            cur.execute(f"""SELECT product_id, model_code, prod_name, base_price,
                           category_code, img_url, create_dt, update_dt
                           FROM products {where} ORDER BY product_id DESC LIMIT %s OFFSET %s""",
                        params + [page_size, offset])
            rows = cur.fetchall()
            
        items = []
        for r in rows:
            p_dict = dict(r)
            p_dict["local_url"] = search_service.get_local_fallback_url(p_dict.get("img_url"))
            items.append(ProductResponse(**p_dict))

        return ProductListResponse(
            items=items, total=total,
            page=page, page_size=page_size,
            total_pages=math.ceil(total / page_size) if total > 0 else 0)
    except Exception as e:
        logger.error(f"상품 목록 조회 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")


@router.get("/{product_id}", response_model=ProductDetailResponse)
async def get_product_detail(product_id: str):
    """상품 상세 조회 (PostgreSQL 통합 - MongoDB 제거)"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("""SELECT product_id, model_code, prod_name, base_price,
                           category_code, img_url, create_dt, update_dt
                           FROM products WHERE product_id = %s""", (product_id,))
            product_row = cur.fetchone()
            if not product_row:
                raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")

            cur.execute("""SELECT nprice_id, product_id, rank, naver_price, mall_name, mall_url, create_dt
                           FROM naver_prices WHERE product_id = %s ORDER BY rank ASC""", (product_id,))
            price_rows = cur.fetchall()

            # product_features에서 detected_desc + detail_desc 모두 조회 (MongoDB 통합)
            cur.execute("SELECT detected_desc, detail_desc FROM product_features WHERE product_id = %s", (product_id,))
            feature_row = cur.fetchone()

        product_data = dict(product_row)
        product_data["local_url"] = search_service.get_local_fallback_url(product_data.get("img_url"))

        return ProductDetailResponse(
            product=ProductResponse(**product_data),
            detail_desc=feature_row["detail_desc"] if feature_row else None,
            detected_desc=feature_row["detected_desc"] if feature_row else None,
            naver_prices=[NaverPriceResponse(**r) for r in price_rows],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"상품 상세 조회 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(req: ProductCreateRequest):
    """상품 등록"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("""INSERT INTO products (model_code, prod_name, base_price, category_code, img_url)
                           VALUES (%s,%s,%s,%s,%s)
                           RETURNING product_id, model_code, prod_name, base_price, category_code, img_url, create_dt, update_dt""",
                        (req.model_code, req.prod_name, req.base_price, req.category_code, getattr(req, 'img_url', None)))
            row = cur.fetchone()
        return ProductResponse(**row)
    except Exception as e:
        logger.error(f"상품 등록 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: str):
    """상품 삭제"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("DELETE FROM naver_prices WHERE product_id = %s", (product_id,))
            cur.execute("DELETE FROM product_features WHERE product_id = %s", (product_id,))
            cur.execute("DELETE FROM products WHERE product_id = %s RETURNING product_id", (product_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"상품 삭제 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")


@router.post("/{product_id}/view")
async def record_product_view(product_id: str, request: Request):
    """최근 본 상품 기록"""
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        return {"success": False, "message": "로그인이 필요합니다"}
    try:
        with get_pg_cursor() as cur:
            cur.execute("""INSERT INTO recent_views (user_id, product_id, view_dt) VALUES (%s,%s,NOW())
                           ON CONFLICT (user_id, product_id) DO UPDATE SET view_dt = NOW()""", (user_id, product_id))
        return {"success": True}
    except Exception as e:
        logger.error(f"조회 기록 실패: {e}")
        return {"success": False}


@router.post("/{product_id}/like")
async def add_like(product_id: str, request: Request):
    """좋아요 추가"""
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        return {"success": False, "message": "로그인이 필요합니다"}
    try:
        with get_pg_cursor() as cur:
            cur.execute("INSERT INTO likes (user_id, product_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (user_id, product_id))
        return {"success": True, "liked": True}
    except Exception as e:
        logger.error(f"좋아요 실패: {e}")
        return {"success": False}


@router.delete("/{product_id}/like")
async def remove_like(product_id: str, request: Request):
    """좋아요 취소"""
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        return {"success": False, "message": "로그인이 필요합니다"}
    try:
        with get_pg_cursor() as cur:
            cur.execute("DELETE FROM likes WHERE user_id=%s AND product_id=%s", (user_id, product_id))
        return {"success": True, "liked": False}
    except Exception as e:
        logger.error(f"좋아요 취소 실패: {e}")
        return {"success": False}


@router.get("/{product_id}/like-status")
async def get_like_status(product_id: str, request: Request):
    """좋아요 상태 확인"""
    session = _get_user_from_session(request)
    user_id = session.get("user_id") if session else None
    if not user_id:
        return {"success": True, "liked": False}
    try:
        with get_pg_cursor() as cur:
            cur.execute("SELECT 1 FROM likes WHERE user_id=%s AND product_id=%s", (user_id, product_id))
            return {"success": True, "liked": cur.fetchone() is not None}
    except Exception as e:
        logger.error(f"좋아요 상태 확인 실패: {e}")
        return {"success": False, "liked": False}
