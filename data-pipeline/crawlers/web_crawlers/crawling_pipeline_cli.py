import os
import sys
import argparse
import asyncio
import json
import logging
import aiohttp
from datetime import datetime
import psycopg2
from psycopg2.extras import execute_values
import traceback
import re
import difflib

def get_similarity(target_name: str, naver_title: str) -> float:
    if not target_name or not naver_title: 
        return 0.0
    target_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', target_name).lower()
    naver_title_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', naver_title).lower()
    return difflib.SequenceMatcher(None, target_clean, naver_title_clean).ratio()

async def search_naver_shopping_api(query: str, session) -> list:
    client_id = os.getenv("X_NAVER_CLIENT_ID")
    client_secret = os.getenv("X_NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return []
    url = "https://openapi.naver.com/v1/search/shop.json"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }
    params = {"query": query, "display": 5}
    try:
        async with session.get(url, headers=headers, params=params, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("items", [])
    except Exception as e:
        logger.warning(f"네이버 API 호출 에러: {e}")
    return []

# 로컬 개발 환경에서 .env 파일 자동 로드 (GitHub Actions에서는 환경변수로 주입)
# .env 파일의 DATABASE_URL 변수명에 공백이 있는 경우 (예: DATABASE_URL = ...)를 처리하기 위해
# python-dotenv 대신 직접 파싱 방식을 사용합니다.
try:
    import re as _re
    _env_path = r"D:\dev\lookalike-lightweight\.env"
    # GitHub Actions 환경이거나 이미 환경변수에 Neon URL이 주입된 경우 스킵
    if not os.getenv("DATABASE_URL") and os.path.isfile(_env_path):
        _db_url_found = None
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#"):
                    continue
                _m = _re.match(r'^DATABASE_URL\s*=\s*(.+)$', _line)
                if _m:
                    _db_url_found = _m.group(1).strip().strip('"').strip("'")
        # ${...} 변수 치환이 없는 실제 URL만 환경변수로 설정
        if _db_url_found and "${" not in _db_url_found:
            os.environ["DATABASE_URL"] = _db_url_found
            logging.getLogger("crawling_pipeline").info(f"[ENV] DATABASE_URL set from .env (Neon DB)")
        elif _db_url_found and "${" in _db_url_found:
            # 첫 번째 선언(로컬 DB)은 무시 — 하지만 다른 필요한 변수들은 로드
            try:
                from dotenv import load_dotenv
                load_dotenv(_env_path, override=False)
            except ImportError:
                pass
except Exception as _e:
    logging.getLogger("crawling_pipeline").warning(f"[ENV] .env 로드 중 예외: {_e}")

# base_utils 가져오기
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_utils import (
    send_alert, configure_cloudinary, upload_image_to_cloudinary, 
    swap_staging_to_production, clear_staging_data, get_prod_db_connection, get_dw_db_connection,
    get_next_product_id, log_pipeline_start, log_pipeline_end, log_pipeline_error,
    get_yolo_clip_image_embedding, get_gemini_text_embedding
)

logger = logging.getLogger("crawling_pipeline")

# 브랜드별 스크래퍼 모듈 임포트
BRAND_CRAWLER_MODELS = {}

# 8seconds
try:
    import scraper_8seconds as s_8s
    BRAND_CRAWLER_MODELS["8seconds"] = s_8s
except ImportError as e:
    logger.warning(f"8seconds scraper 임포트 에러: {e}")

# musinsa
try:
    import scraper_musinsa as s_ms
    BRAND_CRAWLER_MODELS["musinsa"] = s_ms
except ImportError as e:
    logger.warning(f"musinsa scraper 임포트 에러: {e}")

# topten
try:
    import scraper_topten as s_tt
    BRAND_CRAWLER_MODELS["topten"] = s_tt
except ImportError as e:
    logger.warning(f"topten scraper 임포트 에러: {e}")

# uniqlo
try:
    import scraper_uniqlo as s_uq
    BRAND_CRAWLER_MODELS["uniqlo"] = s_uq
except ImportError as e:
    logger.warning(f"uniqlo scraper 임포트 에러: {e}")

# zara
try:
    import scraper_zara as s_zr
    BRAND_CRAWLER_MODELS["zara"] = s_zr
except ImportError as e:
    logger.warning(f"zara scraper 임포트 에러: {e}")


async def run_pipeline(
    brand: str, limit: int = 50, dry_run: bool = False, 
    action: str = "crawl", force: bool = False,
    gender: str = None, category: str = None, run_id: int = None,
    force_download: bool = False
):
    """
    특정 브랜드의 Playwright 크롤링 실행 및 
    Cloudinary 실시간 staging 업로드 -> Neon DB 스테이징 테이블 적재 -> 원자적 교체 프로세스.
    """
    logger.info(f"🚀 [{brand}] 크롤링 파이프라인 기동 (action={action}, limit={limit}, dry_run={dry_run}, force={force}, gender={gender}, category={category}, force_download={force_download})")
    
    # [Phase 2] 24시간 검증 후 프로덕션 스위칭만 진행 (크롤링 스킵)
    if action == "swap":
        if not dry_run:
            swap_success = await swap_staging_to_production(brand, force=force)
            if not swap_success:
                raise RuntimeError(f"[{brand}] 데이터 프로덕션 스위칭 중 오류가 발생했습니다. 차단 이력을 점검하세요.")
        else:
            logger.info("ℹ️ [DRY RUN] 스위칭 및 이미지 이관 로직을 실행하지 않고 패스합니다.")
        return {"total_items": 0, "new_items": 0, "updated_items": 0}

    # [Phase 1] 크롤링 및 스테이징 적재 실행
    scraper = BRAND_CRAWLER_MODELS.get(brand.lower())
    if not scraper:
        raise ValueError(f"지원하지 않거나 로드되지 않은 브랜드 스크래퍼입니다: {brand}")

    # Cloudinary 초기화
    configure_cloudinary()

    # 스테이징 기존 찌꺼기 청소 (분할 수집 누적을 위해, 카테고리 한정 수집 시에는 청소 스킵)
    if not dry_run and not gender and not category:
        clear_staging_data(brand)

    # Playwright 크롤러 구동
    from playwright.async_api import async_playwright
    
    collected_products = []
    visited_products = set()
    sem = asyncio.Semaphore(3)

    async def process_product_link(product_id, gender_val, category_val, context):
        if product_id in visited_products:
            return []
        visited_products.add(product_id)

        if len(collected_products) >= limit:
            return []

        async with sem:
            if brand == "8seconds":
                url = f"https://www.ssfshop.com/8-seconds/{product_id}/good?brandShopNo=BDMA07A01&brndShopId=8SBSS"
            elif brand == "musinsa":
                url = f"https://www.musinsa.com/products/{product_id}"
            elif brand == "topten":
                url = f"https://topten10.goodwearmall.com/product/{product_id}/detail"
            elif brand == "uniqlo":
                url = f"https://www.uniqlo.com/kr/ko/products/{product_id.split('?')[0]}"
            elif brand == "zara":
                url = f"https://www.zara.com/kr/ko/man-outerwear-l715.html"
                if product_id.startswith("http"):
                    url = product_id

            p_page = await context.new_page()
            try:
                # 불필요 리소스 차단 (속도 향상 및 레이트 리밋 예방)
                await p_page.route("**/*review*", lambda route: route.abort())
                await p_page.route("**/*recommend*", lambda route: route.abort())
                
                await p_page.goto(url, timeout=45000, wait_until="domcontentloaded")
                
                # 봇 방지 우회형 지능적 미세 지연 추가 (Anti-Scraping)
                import random
                await asyncio.sleep(random.uniform(0.5, 1.5))
                
                product_dict = None
                if brand == "8seconds":
                    product_dict = await scraper.extract_product_data_from_dom(p_page)
                elif brand == "musinsa":
                    product_dict = await scraper.extract_attribute_focus_data(p_page)
                elif brand == "topten":
                    product_dict = await scraper.extract_product_data_from_dom(p_page)
                elif brand == "uniqlo":
                    product_dict = await scraper.extract_product_base_data(p_page, product_id)
                    if product_dict:
                        product_dict["goodsImages"] = await scraper.extract_current_images(p_page)
                elif brand == "zara":
                    product_dict = await scraper.extract_product_data_from_dom(p_page)

                if product_dict and product_dict.get("goodsNm"):
                    pid = product_dict.get("goodsNo") or product_id
                    product_dict["product_id"] = pid
                    product_dict["gender"] = gender_val
                    product_dict["category"] = category_val
                    collected_products.append(product_dict)
                    logger.info(f"   ➕ [{brand.upper()}] 수집 성공 ({len(collected_products)}/{limit}): {product_dict['goodsNm']}")
                    
                    return product_dict.get("other_color_ids", [])
            except Exception as e:
                logger.warning(f"   ⚠️ [{brand.upper()}] {product_id} 수집 에러: {e}")
            finally:
                await p_page.close()
        return []

    async def crawl_brand_categories():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            if brand == "zara":
                stealth_js = """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
                """
                await context.add_init_script(stealth_js)

            target_map = scraper.TARGET_MAP
            for gender_key, categories in target_map.items():
                # 성별 분할 필터링
                if gender and gender_key.lower() != gender.lower():
                    continue
                for category_key, urls in categories.items():
                    # 카테고리 분할 필터링
                    if category and category_key.lower() != category.lower():
                        continue
                    
                    if isinstance(urls, str):
                        urls = [urls]
                    for target_url in urls:
                        if len(collected_products) >= limit:
                            break
                        
                        logger.info(f"🔍 목록 스캔 중: {gender_key} - {category_key} - {target_url}")
                        
                        page = await context.new_page()
                        try:
                            await page.goto(target_url, timeout=45000, wait_until="domcontentloaded")
                            for _ in range(2):
                                await page.evaluate("window.scrollBy(0, 2000)")
                                await asyncio.sleep(1.0)
                            
                            product_codes = []
                            if brand == "8seconds":
                                product_codes = await page.evaluate("""() => 
                                    Array.from(document.querySelectorAll('li.god-item')).map(item => item.getAttribute('view-godno')).filter(c => c !== null)
                                """)
                            elif brand == "musinsa":
                                import re
                                hrefs = await page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href)")
                                product_codes = []
                                for h in hrefs:
                                    m = re.search(r'(?:goods|products)\/(\d+)', h)
                                    if m: product_codes.append(m.group(1))
                            elif brand == "topten":
                                import re
                                content = await page.content()
                                matches = re.findall(r"[A-Z]{3}\d[A-Z]{2}\d{4}[A-Z0-9]+", content)
                                product_codes = [pid for pid in matches if 10 <= len(pid) <= 15]
                            elif brand == "uniqlo":
                                import re
                                content = await page.content()
                                matches = re.findall(r"/products/([A-Z0-9-]+)", content)
                                product_codes = [pid for pid in matches if len(pid) >= 5 and "review" not in pid]
                            elif brand == "zara":
                                import re
                                links = await page.evaluate("() => Array.from(document.querySelectorAll('a[href*=\"-p\"][href*=\".html\"]')).map(a => a.href)")
                                product_codes = []
                                for link in links:
                                    match = re.search(r'-p([0-9]+)\.html', link)
                                    if match:
                                        product_codes.append(link.split('?')[0])

                            product_codes = list(set(product_codes))
                            logger.info(f"   🔗 카테고리 목록에서 상품 {len(product_codes)}개 식별 완료")
                            await page.close()

                            pending_codes = product_codes
                            while pending_codes and len(collected_products) < limit:
                                code = pending_codes.pop(0)
                                res = await process_product_link(code, gender_key, category_key, context)
                                if res:
                                    pending_codes = [c for c in res if c not in visited_products] + pending_codes

                        except Exception as e:
                            logger.error(f"❌ 카테고리 수집 실패: {e}")
                            if not page.is_closed():
                                await page.close()
            
            await browser.close()

    await crawl_brand_categories()
    
    logger.info(f"📦 [{brand}] Playwright 크롤링 완료. 수집 데이터 개수: {len(collected_products)} 건")
    
    if not collected_products:
        raise ValueError(f"[{brand}] 크롤링된 데이터가 전혀 없습니다. 차단 또는 목록 레이아웃 변경 여부를 점검해 주세요.")

    # 4. Cloudinary Staging 업로드 및 DB Staging 적재
    logger.info(f"🔄 [{brand}] Cloudinary Staging 업로드 및 Neon DB 스테이징 적재 시작...")
    
    async with aiohttp.ClientSession() as session:
        success_db_count = 0
        new_items_count = 0
        updated_items_count = 0
        
        # PROD DB 연결 (캐시 조회용)
        prod_conn = get_prod_db_connection()
        prod_cur = prod_conn.cursor()
        
        # DW DB 연결 (스테이징 테이블 적재용)
        dw_conn = get_dw_db_connection()
        dw_conn.autocommit = False
        dw_cur = dw_conn.cursor()

        try:
            for item in collected_products:
                # 1. model_code 공식 품번 포맷 수립
                orig_no = item.get("product_id")
                model_code = orig_no
                if brand == "8seconds":
                    mat = item.get("goodsMaterial", {})
                    official = mat.get("제조사 상품번호") or mat.get("제조업체 상품코드") or mat.get("품번") or mat.get("제조사상품번호")
                    if official:
                        model_code = official
                elif brand == "musinsa":
                    model_code = item.get("product_code") or orig_no
                elif brand == "uniqlo":
                    model_code = item.get("display_goods_no") or orig_no

                # model_code 50자 제한
                model_code = model_code[:50]

                base_price = item.get("price", 0)
                if base_price is None:
                    base_price = 0
                else:
                    try:
                        base_price = int(base_price)
                    except:
                        base_price = 0

                # 2. PROD DB 캐시 비교 (CDC 필터링) - model_code 기반 고유 검사
                prod_cur.execute("SELECT product_id, base_price, img_url FROM products WHERE model_code = %s AND brand_name = %s", (model_code, brand.upper()))
                cache_row = prod_cur.fetchone()
                
                prod_id = ""
                cloudinary_url = ""
                skip_cloudinary = False
                
                if cache_row and not force_download:
                    prod_id, db_price, db_img_path = cache_row
                    if db_price == base_price and db_img_path and db_img_path.startswith("http"):
                        cloudinary_url = db_img_path
                        skip_cloudinary = True
                        logger.info(f"   Skip [{brand.upper()}] {model_code} cache hit -> Cloudinary upload skip")
                else:
                    if cache_row:
                        prod_id = cache_row[0]
                        updated_items_count += 1
                    # 동일 배치 내 중복 등록 방지 체크 (DW DB 조회)
                    dw_cur.execute("SELECT product_id FROM staging_products WHERE model_code = %s AND brand_name = %s", (model_code, brand.upper()))
                    stg_row = dw_cur.fetchone()
                    if stg_row:
                        prod_id = stg_row[0]
                    elif not prod_id:
                        # 신규 상품일 경우 DW DB의 시퀀스를 1 올리며 sequential ID 신규 발급!
                        prod_id = get_next_product_id(dw_cur, brand)
                        new_items_count += 1

                # 3. 캐시 불일치 시에만 Cloudinary staging/ 폴더로 스트리밍 업로드 실행
                if not skip_cloudinary:
                    images = item.get("goodsImages", [])
                    primary_url = images[0] if images else item.get("thumbnailImageUrl")
                    
                    if primary_url:
                        try:
                             # 파일명 규격화: 브랜드_성별_카테고리_모델코드 (한글 배제하여 Cloudinary 인코딩 및 리네임 이관 정합성 에러 해결)
                             public_id = f"{brand.upper()}_{item.get('gender')}_{item.get('category')}_{model_code}"
                             
                             # ENV_MODE에 따라 DEV/PROD 폴더 분리하여 staging/{brand} 폴더에 업로드
                             _env_prefix = "PROD" if os.getenv("ENV_MODE", "local").lower() in ["prod", "production"] else "DEV"
                             cloudinary_url = await upload_image_to_cloudinary(
                                 session, primary_url, folder=f"{_env_prefix}/staging/{brand}", public_id=public_id
                             )
                        except Exception as upload_err:
                            logger.warning(f"⚠️ 이미지 업로드 스킵 (URL {primary_url}): {upload_err}")
                            log_pipeline_error(run_id, "IMAGE_UPLOAD_WARN", f"이미지 업로드 스킵 (기존 URL 유지): {upload_err}", product_id=prod_id, source_url=primary_url)
                            cloudinary_url = primary_url

                # 4. 스테이징 DB 적재 (DW DB)
                if not dry_run:
                    dw_cur.execute("""
                        INSERT INTO staging_products (
                            product_id, model_code, brand_name, prod_name, base_price, gender, 
                            category_code, img_url, origin_url, create_dt, update_dt
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT (product_id) DO UPDATE 
                        SET prod_name = EXCLUDED.prod_name,
                            base_price = EXCLUDED.base_price,
                            img_url = EXCLUDED.img_url,
                            update_dt = CURRENT_TIMESTAMP
                    """, (
                        prod_id,
                        model_code,
                        brand.upper(),
                        item.get("goodsNm", "")[:512],
                        base_price,
                        item.get("gender", "Men"),
                        item.get("category", "Top"),
                        cloudinary_url,
                        item.get("url")
                    ))
                    success_db_count += 1

                    # 네이버 최저가 API 병행 실행 (항상 실행하여 실시간 가격 변동 추적)
                    logger.info(f"   🔍 네이버 쇼핑 API 검색 진행 중: {item.get('goodsNm')}")
                    original_name = item.get('goodsNm')
                    # 네이버 검색을 위해 브랜드명, 성별 괄호 등 정제
                    query_term = re.sub(r'\([^\)]*\)|[남여]\)\s*', '', original_name)
                    query_term = re.sub(r'(TOPTEN10|topten10|8SECONDS|8seconds|탑텐10|탑텐|에잇세컨즈|UNIQLO|uniqlo|유니클로)', '', query_term)
                    query_term = query_term.strip()
                    if len(query_term) < 2:
                        query_term = original_name
                    
                    nv_items = await search_naver_shopping_api(query_term, session)
                    nv_count = len(nv_items) if nv_items else 0
                    if nv_count < 5:
                        msg = f"네이버 최저가 매칭 수량 부족: {original_name} (검색어: {query_term}) - 매칭 건수: {nv_count}/5개"
                        log_pipeline_error(run_id, "NAVER_API_WARN", msg, product_id=prod_id)
                    
                    # 기존 네이버 최저가 찌꺼기 삭제
                    dw_cur.execute("DELETE FROM staging_naver_prices WHERE product_id = %s", (prod_id,))
                    
                    rank = 1
                    for nv_item in (nv_items or [])[:5]:
                        nv_title = nv_item.get("title", "").replace("<b>", "").replace("</b>", "")
                        nv_price = int(nv_item.get("lprice") or 0)
                        mall_name = nv_item.get("mallName", "")
                        mall_url = nv_item.get("link", "")
                        image_url = nv_item.get("image", "")
                        sim_score = get_similarity(item.get("goodsNm"), nv_title)
                        
                        dw_cur.execute("""
                            INSERT INTO staging_naver_prices (
                                product_id, brand, model_code, original_name, original_price,
                                rank, naver_title, naver_price, mall_name, mall_url, image_url, similarity_score,
                                create_dt, update_dt
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """, (
                            prod_id, brand.upper(), model_code, item.get("goodsNm"), base_price,
                            rank, nv_title, nv_price, mall_name, mall_url, image_url, sim_score
                        ))
                        rank += 1
            
            # === [임베딩 생성 및 staging_product_embeddings 적재] ===
            if success_db_count > 0:
                logger.info(f"🔄 수집된 {success_db_count}개 상품에 대해 이미지/텍스트 벡터 임베딩 생성 진행 중 (DW DB 적재)...")
                
                # 방금 적재된 상품 목록 조회
                dw_cur.execute("""
                    SELECT product_id, img_url, prod_name, gender, category_code 
                    FROM staging_products 
                    WHERE brand_name = %s
                """, (brand.upper(),))
                inserted_products = dw_cur.fetchall()
                
                # 기존 해당 브랜드의 staging 임베딩 삭제
                dw_cur.execute("DELETE FROM staging_product_embeddings WHERE brand = %s", (brand.upper(),))
                
                gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY") or os.getenv("GEMINI_KEY")
                
                sem = asyncio.Semaphore(5)  # API Rate limit 방지 동시성 제약
                
                async def generate_and_save_embed(session, prod_id, img_url, prod_name, gender, category):
                    async with sem:
                        image_vector = None
                        text_vector = None
                        
                        try:
                            # 1. YOLO-CLIP 통합 이미지 임베딩 추출
                            if img_url:
                                image_vector = await get_yolo_clip_image_embedding(session, img_url)
                            
                            # 2. Gemini 텍스트 임베딩 추출
                            if prod_name and gemini_key:
                                text_vector = await get_gemini_text_embedding(session, prod_name, gemini_key)
                        except Exception as embed_err:
                            logger.warning(f"⚠️ 상품 {prod_id} 임베딩 생성 중 오류 (스킵): {embed_err}")
                            log_pipeline_error(run_id, "EMBEDDING_WARN", f"임베딩 생성 오류 (스킵): {embed_err}", product_id=prod_id, source_url=img_url)
                            
                        if image_vector or text_vector:
                            return (prod_id, image_vector, text_vector, brand.upper(), category, gender, img_url)
                        return None

                async with aiohttp.ClientSession() as session:
                    tasks = [generate_and_save_embed(session, row[0], row[1], row[2], row[3], row[4]) for row in inserted_products]
                    embed_results = await asyncio.gather(*tasks)
                
                # 결과물 일괄 INSERT
                valid_embeds = [r for r in embed_results if r is not None]
                for embed_row in valid_embeds:
                    dw_cur.execute("""
                        INSERT INTO staging_product_embeddings (
                            product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (product_id) DO UPDATE
                        SET image_vector = EXCLUDED.image_vector,
                            text_vector = EXCLUDED.text_vector,
                            create_dt = CURRENT_TIMESTAMP
                    """, embed_row)
                
                logger.info(f"✨ staging_product_embeddings 테이블에 {len(valid_embeds)}건 임베딩 벡터 생성 및 적재 완료.")
            
            dw_conn.commit()
            logger.info(f"✅ 스테이징 테이블 {success_db_count}건 임시 적재 완료. (Phase 1 완료)")
            return {"total_items": success_db_count, "new_items": new_items_count, "updated_items": updated_items_count}
            
        except Exception as db_err:
            dw_conn.rollback()
            raise RuntimeError(f"스테이징 적재 중 DB 에러: {db_err}")
        finally:
            prod_cur.close()
            prod_conn.close()
            dw_cur.close()
            dw_conn.close()


def main():
    parser = argparse.ArgumentParser(description="Lookalike 안심 모니터링 크롤러 CLI")
    parser.add_argument("--brand", required=True, choices=["8seconds", "musinsa", "topten", "uniqlo", "zara"], help="크롤링할 타겟 패션 브랜드")
    parser.add_argument("--limit", type=int, default=50, help="수집할 최대 상품 개수")
    parser.add_argument("--dry-run", action="store_true", help="실제 DB 반영을 생략하고 디버깅 출력만 활성화")
    parser.add_argument("--action", default="crawl", choices=["crawl", "swap"], help="실행할 파이프라인 액션 (crawl: 수집/임시적재, swap: 지연 이관)")
    parser.add_argument("--force", action="store_true", help="swap 액션 시 24시간 대기 조건 무시하고 즉시 강제 스위칭")
    parser.add_argument("--gender", choices=["Men", "Women"], help="수집할 타겟 성별 (분할 크롤링용)")
    parser.add_argument("--category", choices=["Outer", "Top", "Bottom"], help="수집할 카테고리 (분할 크롤링용)")
    parser.add_argument("--force-download", action="store_true", help="캐시를 강제 스킵하고 이미지를 새로 다운로드하며 네이버 최저가 API 구동")
    
    args = parser.parse_args()
    
    # pipeline_runs 로깅용 시작 레코드 등록 (GitHub Actions 여부에 따라 분기)
    run_id = None
    if not args.dry_run:
        pipeline_name = "auto_crawling_pipeline" if os.getenv("GITHUB_ACTIONS") == "true" else "manual_crawling_pipeline"
        run_id = log_pipeline_start(args.brand, pipeline_name=pipeline_name)

    try:
        metrics = asyncio.run(run_pipeline(
            brand=args.brand, 
            limit=args.limit, 
            dry_run=args.dry_run, 
            action=args.action, 
            force=args.force,
            gender=args.gender,
            category=args.category,
            run_id=run_id,
            force_download=args.force_download
        ))
        logger.info(f"🎉 [{args.brand}] 배치 파이프라인이 성공적으로 완수되었습니다.")
        if run_id:
            total = metrics.get("total_items", 0) if metrics else 0
            new_val = metrics.get("new_items", 0) if metrics else 0
            upd_val = metrics.get("updated_items", 0) if metrics else 0
            log_pipeline_end(run_id, "SUCCESS", total_items=total, new_items=new_val, updated_items=upd_val)
    except Exception as e:
        err_msg = f"❌ [{args.brand}] 크롤러 전체 프로세스 기동 실패!\n에러 원인: {e}"
        logger.critical(err_msg, exc_info=True)
        send_alert(err_msg, level="CRITICAL")
        if run_id:
            tb = traceback.format_exc()
            log_pipeline_error(run_id, type(e).__name__, str(e), tb)
            log_pipeline_end(run_id, "FAILED", error_count=1)
        sys.exit(1)


if __name__ == "__main__":
    main()
