from fastapi import APIRouter, HTTPException, Query, Body
from typing import Optional
from pydantic import BaseModel
import os
import sys
import math
import httpx
import tempfile
import urllib.request
import psycopg2
import asyncio
from psycopg2.extras import RealDictCursor

# 로컬 경로 설정 호환
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "data-pipeline", "crawlers", "web_crawlers")))
import base_utils
from ..database import get_dw_cursor, get_pg_cursor
from ..config import get_settings

router = APIRouter(prefix="/admin/pipeline", tags=["Admin Errors"])

class ResolveRequest(BaseModel):
    brand: str
    action: str  # "re_embed", "partial_switch", "delete_staging"
    product_ids: list[str]

@router.get("/errors/summary")
async def get_errors_summary():
    """브랜드별 치명 에러 상품 요약 개수 조회"""
    try:
        with get_dw_cursor() as cur:
            cur.execute("""
                SELECT 
                    p.brand_name as brand,
                    COUNT(DISTINCT pe.product_id) as critical_errors
                FROM pipeline_errors pe
                JOIN staging_products p ON pe.product_id = p.product_id
                GROUP BY p.brand_name
            """)
            rows = cur.fetchall()
            return {"success": True, "summary": [{ "brand": r[0], "count": r[1] } for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"에러 요약 조회 실패: {e}")

@router.get("/errors/products")
async def get_error_products(
    brand: str = Query(..., description="브랜드명 (spao, giordano, topten 등)"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100)
):
    """특정 브랜드의 에러 발생 상품 목록 및 세부 정보 상세 조회"""
    try:
        offset = (page - 1) * limit
        brand_upper = brand.upper()

        with get_dw_cursor() as cur:
            # 전체 개수 세기
            cur.execute("""
                SELECT COUNT(DISTINCT pe.product_id)
                FROM pipeline_errors pe
                JOIN staging_products p ON pe.product_id = p.product_id
                WHERE UPPER(p.brand_name) = %s
            """, (brand_upper,))
            total = cur.fetchone()[0] or 0

            # 상세 내역 스캔 (최신 에러 우선)
            cur.execute("""
                SELECT 
                    pe.product_id, 
                    p.prod_name,
                    p.category_code,
                    pe.error_type,
                    pe.error_message,
                    p.img_url,
                    pe.created_at
                FROM pipeline_errors pe
                JOIN staging_products p ON pe.product_id = p.product_id
                WHERE UPPER(p.brand_name) = %s
                ORDER BY pe.created_at DESC
                LIMIT %s OFFSET %s
            """, (brand_upper, limit, offset))
            
            rows = cur.fetchall()
            products = []
            for r in rows:
                products.append({
                    "product_id": r[0],
                    "product_name": r[1],
                    "category": r[2],
                    "error_type": r[3],
                    "error_message": r[4],
                    "image_url": r[5],
                    "created_at": r[6].isoformat() if r[6] else None
                })

            return {
                "success": True,
                "total": total,
                "page": page,
                "limit": limit,
                "products": products
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"에러 상품 조회 실패: {e}")

@router.post("/errors/resolve")
async def resolve_errors(req: ResolveRequest = Body(...)):
    """에러가 발생한 상품들에 대해 운영자의 지시에 따라 조치를 실행"""
    brand_upper = req.brand.upper()
    product_ids = req.product_ids
    action = req.action.lower()

    if not product_ids:
        raise HTTPException(status_code=400, detail="조치할 상품 ID가 지정되지 않았습니다.")

    settings = get_settings()

    if action == "re_embed":
        # ── 1. [임베딩 실시간 재구축] ──
        success_cnt = 0
        failed_cnt = 0
        
        try:
            # 1-1) staging_products에서 상품 정보 조회
            with get_dw_cursor() as cur:
                cur.execute("""
                    SELECT product_id, prod_name, img_url, category_code, gender 
                    FROM staging_products 
                    WHERE product_id = ANY(%s)
                """, (product_ids,))
                products = cur.fetchall()

            async def get_text_vector(text):
                model_id = "openai/clip-vit-base-patch32"
                url = f"https://api-inference.huggingface.co/pipeline/feature-extraction/{model_id}"
                headers = {"Authorization": f"Bearer {settings.HF_TOKEN}"}
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(url, json={"inputs": text}, headers=headers)
                        resp.raise_for_status()
                        data = resp.json()
                        vec = data[0] if isinstance(data, list) and isinstance(data[0], list) else data
                        # L2 Normalization
                        s = sum(x*x for x in vec)
                        if s > 0:
                            norm = math.sqrt(s)
                            vec = [x/norm for x in vec]
                        return vec + [0.0] * 256
                except Exception:
                    return None

            async def get_image_vector(img_url):
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(img_url)
                        resp.raise_for_status()
                        img_bytes = resp.content
                    
                    from ..services.search_service import search_service
                    res = await search_service.call_hf_space_predict(img_bytes)
                    if isinstance(res, dict):
                        return res.get("embedding")
                    return None
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(f"get_image_vector 실패: {e}")
                    return None

            for p in products:
                pid, name, img_url, cat, gen = p
                txt_vec = await get_text_vector(name)
                img_vec = await get_image_vector(img_url)

                if txt_vec and img_vec:
                    # staging_product_embeddings에 재적재 및 해당 상품의 pipeline_errors 로그 소거
                    with get_dw_cursor() as cur:
                        cur.execute("""
                            INSERT INTO staging_product_embeddings (
                                product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (product_id) DO UPDATE SET
                                image_vector = EXCLUDED.image_vector,
                                text_vector = EXCLUDED.text_vector,
                                create_dt = CURRENT_TIMESTAMP
                        """, (pid, img_vec, txt_vec, brand_upper, cat, gen, img_url))
                        
                        # 이관 가능하게 되었으므로 해당 상품의 치명 에러 로그 삭제
                        cur.execute("DELETE FROM pipeline_errors WHERE product_id = %s", (pid,))
                    success_cnt += 1
                else:
                    failed_cnt += 1

            return {
                "success": True, 
                "message": f"임베딩 재연산 조치 결과: 성공 {success_cnt}건, 실패 {failed_cnt}건. 성공한 상품은 에러가 소거되었습니다."
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"임베딩 재연산 실패: {e}")

    elif action == "partial_switch":
        # ── 2. [선택적 이관 강제 처리] ──
        # 에러 상품이라도 강제 스위칭을 수행하여 프로덕션 DB로 바로 옮깁니다. (force=True 모드 연동)
        try:
            # 2-1) 임시 DB 연결 수식
            stage_conn = base_utils.get_dw_db_connection()
            stage_cur = stage_conn.cursor()

            # 선택받은 에러 상품들의 메타, 가격, 임베딩을 강제로 긁어와 이관 진행
            stage_cur.execute("""
                SELECT product_id, model_code, prod_name, base_price, gender, category_code, img_url, origin_url, create_dt
                FROM staging_products 
                WHERE product_id = ANY(%s)
            """, (product_ids,))
            staging_rows = stage_cur.fetchall()

            stage_cur.execute("""
                SELECT product_id, rank, naver_price, mall_name, mall_url, image_url, create_dt
                FROM staging_naver_prices
                WHERE product_id = ANY(%s)
            """, (product_ids,))
            naver_rows = stage_cur.fetchall()

            stage_cur.execute("""
                SELECT product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                FROM staging_product_embeddings
                WHERE product_id = ANY(%s)
            """, (product_ids,))
            embedding_rows = stage_cur.fetchall()

            # PROD DB 트랜잭션 수립 및 일괄 강제 이관
            core_conn = base_utils.get_prod_db_connection()
            core_conn.autocommit = False
            core_cur = core_conn.cursor()

            try:
                # 기존 데이터 제거 후 재적재
                if product_ids:
                    core_cur.execute("DELETE FROM product_embeddings WHERE product_id = ANY(%s)", (product_ids,))
                    core_cur.execute("DELETE FROM naver_prices WHERE product_id = ANY(%s)", (product_ids,))
                    core_cur.execute("DELETE FROM products WHERE product_id = ANY(%s)", (product_ids,))

                # 1) 상품 적재
                for u_row in staging_rows:
                    core_cur.execute("""
                        INSERT INTO products (
                            product_id, model_code, brand_name, prod_name, base_price, gender, 
                            category_code, img_url, origin_url, create_dt, update_dt
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """, u_row)

                # 2) 임베딩 적재
                for e_row in embedding_rows:
                    core_cur.execute("""
                        INSERT INTO product_embeddings (
                            product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, e_row)

                # 3) 가격 적재
                for n_row in naver_rows:
                    core_cur.execute("""
                        INSERT INTO naver_prices (
                            product_id, rank, naver_price, mall_name, mall_url, image_url, create_dt
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, n_row)

                core_conn.commit()
                
                # DW DB 스테이징 청소 및 에러 소거
                stage_cur.execute("DELETE FROM staging_products WHERE product_id = ANY(%s)", (product_ids,))
                stage_cur.execute("DELETE FROM staging_naver_prices WHERE product_id = ANY(%s)", (product_ids,))
                stage_cur.execute("DELETE FROM staging_product_embeddings WHERE product_id = ANY(%s)", (product_ids,))
                stage_cur.execute("DELETE FROM pipeline_errors WHERE product_id = ANY(%s)", (product_ids,))
                stage_conn.commit()

                return {"success": True, "message": f"선택받은 {len(product_ids)}개 에러 상품이 성공적으로 운영 DB로 강제 스위칭 및 이관되었습니다."}

            except Exception as core_err:
                core_conn.rollback()
                stage_conn.rollback()
                raise core_err
            finally:
                core_cur.close()
                core_conn.close()
                stage_cur.close()
                stage_conn.close()

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"선택 스위칭 실행 실패: {e}")

    elif action == "delete_staging":
        # ── 3. [재크롤링을 위한 스테이징 제거] ──
        # 에러 상품의 스테이징을 깨끗이 비워 차후 크롤러가 전체 재크롤링을 정상 수행할 수 있게 문을 엽니다.
        try:
            with get_dw_cursor() as cur:
                cur.execute("DELETE FROM staging_products WHERE product_id = ANY(%s)", (product_ids,))
                cur.execute("DELETE FROM staging_naver_prices WHERE product_id = ANY(%s)", (product_ids,))
                cur.execute("DELETE FROM staging_product_embeddings WHERE product_id = ANY(%s)", (product_ids,))
                cur.execute("DELETE FROM pipeline_errors WHERE product_id = ANY(%s)", (product_ids,))
            return {"success": True, "message": f"선택한 {len(product_ids)}개 에러 상품의 스테이징 데이터를 삭제했습니다. 크롤링 시 재수집이 가능합니다."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"스테이징 데이터 삭제 실패: {e}")

    elif action == "re_crawl":
        # ── 4. [선택 상품 벌크 네이버 가격 재수집] ──
        success_cnt = 0
        failed_cnt = 0
        try:
            with get_dw_cursor() as cur:
                cur.execute("""
                    SELECT product_id, prod_name, brand_name 
                    FROM staging_products 
                    WHERE product_id = ANY(%s)
                """, (product_ids,))
                products = cur.fetchall()

            for p in products:
                pid, prod_name, brand_name = p
                ok = await retry_single_product_collect(brand_name, pid, prod_name)
                if ok:
                    success_cnt += 1
                else:
                    failed_cnt += 1
            return {
                "success": True,
                "message": f"선택 상품 가격 재수집 완료: 성공 {success_cnt}건, 실패 {failed_cnt}건."
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"벌크 재크롤링 실패: {e}")

    else:
        raise HTTPException(status_code=400, detail="유효하지 않은 조치(Action) 지시입니다.")


class RetryRequest(BaseModel):
    brand: str
    product_id: str

async def retry_single_product_collect(brand_name: str, pid: str, prod_name: str) -> bool:
    """단일 상품에 대해 네이버 쇼핑 API를 직접 찔러 최저가 5개를 동적으로 재수집하여 staging_naver_prices에 업서트"""
    import re
    client_id = os.getenv("X_NAVER_CLIENT_ID")
    client_secret = os.getenv("X_NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return False

    brand_en = str(brand_name).lower() if brand_name else "unknown"
    brand_ko = {"zara": "자라", "8seconds": "에잇세컨즈", "musinsa": "무신사", "uniqlo": "유니클로", "topten": "탑텐", "spao": "스파오", "giordano": "지오다노"}.get(brand_en, brand_en)

    core_kw = ""
    if prod_name:
        clean_name = re.sub(r'[^가-힣a-zA-Z0-9\s]', ' ', prod_name)
        words = [w for w in clean_name.split() if w.lower() not in [brand_en, "여성", "남성", "정품", "공용"] and not w.isdigit()]
        core_kw = " ".join(words[:3]) if words else prod_name

    query = f"{brand_ko} {core_kw}".strip()
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }
    params = {
        "query": query, "display": 10, "start": 1, "sort": "sim", "exclude": "used"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://openapi.naver.com/v1/search/shop.json", headers=headers, params=params)
            if resp.status_code == 200:
                naver_items = resp.json().get("items", [])
                
                with get_dw_cursor() as cur:
                    # 1) 가격 정보 리셋
                    cur.execute("DELETE FROM staging_naver_prices WHERE product_id = %s", (pid,))
                    
                    # 2) 수집된 정보가 있을 경우 상위 5위 가격정보 저장
                    valid_count = 0
                    for idx, item in enumerate(naver_items, 1):
                        if valid_count >= 5:
                            break
                        naver_price = int(item.get("lprice", 0))
                        if naver_price > 0:
                            cur.execute("""
                                INSERT INTO staging_naver_prices (
                                    product_id, rank, naver_price, mall_name, mall_url, image_url, brand, create_dt
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                            """, (pid, idx, naver_price, item.get("mallName", "공식몰"), item.get("link", "#"), item.get("image", ""), brand_name.upper()))
                            valid_count += 1
                    
                    # 3) 가격정보가 정상 수집되었을 시 해당 상품의 NAVER_PRICE_MISSING 치명 에러 제거
                    if valid_count > 0:
                        cur.execute("DELETE FROM pipeline_errors WHERE product_id = %s AND error_type = 'NAVER_PRICE_MISSING'", (pid,))
                return True
    except Exception:
        pass
    return False

@router.post("/crawling/retry")
async def retry_single_product_api(req: RetryRequest = Body(...)):
    """[단일 상품 핀포인트 즉시 재수집 API] 가격 실시간 수집 및 임베딩 재연산을 원스톱 진행"""
    brand_upper = req.brand.upper()
    pid = req.product_id

    try:
        # 1. 상품 정보 조회
        with get_dw_cursor() as cur:
            cur.execute("SELECT prod_name, brand_name FROM staging_products WHERE product_id = %s", (pid,))
            row = cur.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="해당 상품이 스테이징에 존재하지 않습니다.")

        prod_name, brand_name = row[0], row[1]

        # 2. 가격 재수집 가동
        ok = await retry_single_product_collect(brand_name, pid, prod_name)
        
        # 3. 임베딩 재생성 가동 (오류 해결 시도)
        # re_embed의 단일 로직 인라인 처리
        resolve_req = ResolveRequest(brand=req.brand, action="re_embed", product_ids=[pid])
        await resolve_errors(resolve_req)

        if ok:
            return {"success": True, "message": f"상품 [{pid}]의 핀포인트 가격 재수집 및 임베딩 연산이 성공적으로 완료되었습니다."}
        else:
            return {"success": False, "detail": "가격 재수집에 실패했거나 네이버 쇼핑 검색 결과가 없습니다."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"단일 상품 재수집 실패: {e}")
