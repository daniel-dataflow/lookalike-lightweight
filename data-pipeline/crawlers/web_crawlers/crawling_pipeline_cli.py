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
    if os.path.isfile(_env_path):
        _db_url_found = None
        _hf_tok_found = None
        _hf_sp_found = None
        
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#"):
                    continue
                _m_db = _re.match(r'^DATABASE_URL\s*=\s*(.+)$', _line)
                if _m_db:
                    _db_url_found = _m_db.group(1).strip().strip('"').strip("'")
                _m_hf_tok = _re.match(r'^HF_TOKEN\s*=\s*(.+)$', _line)
                if _m_hf_tok:
                    _hf_tok_found = _m_hf_tok.group(1).strip().strip('"').strip("'")
                _m_hf_sp = _re.match(r'^HF_SPACE_URL\s*=\s*(.+)$', _line)
                if _m_hf_sp:
                    _hf_sp_found = _m_hf_sp.group(1).strip().strip('"').strip("'")
        
        # 환경변수 바인딩
        if _db_url_found and "${" not in _db_url_found:
            os.environ["DATABASE_URL"] = _db_url_found
            logging.getLogger("crawling_pipeline").info(f"[ENV] DATABASE_URL set from .env (Neon DB)")
        if _hf_tok_found:
            os.environ["HF_TOKEN"] = _hf_tok_found
        if _hf_sp_found:
            os.environ["HF_SPACE_URL"] = _hf_sp_found

        try:
            from dotenv import load_dotenv
            load_dotenv(_env_path, override=False)
        except ImportError:
            pass
except Exception as _e:
    logging.getLogger("crawling_pipeline").warning(f"[ENV] .env 로드 중 예외: {_e}")

from base_utils import (
    send_alert, configure_cloudinary, upload_image_to_cloudinary, 
    swap_staging_to_production, clear_staging_data, get_prod_db_connection, get_dw_db_connection,
    get_next_product_id, log_pipeline_start, log_pipeline_end, log_pipeline_error,
    get_yolo_clip_image_embedding, get_clip_text_embedding
)

logger = logging.getLogger("crawling_pipeline")

# ──────────────────────────────────────────────────────────
# [디버깅] Cloudinary 환경변수 검증 로깅
# ──────────────────────────────────────────────────────────
for env_name in ["CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"]:
    val = os.getenv(env_name)
    if val:
        val_stripped = val.strip("'\" \t\r\n")
        masked = f"{val_stripped[:3]}...{val_stripped[-3:]}" if len(val_stripped) > 6 else "***"
        logger.info(f"🔍 [DEBUG ENV] {env_name}: len={len(val)}, stripped_len={len(val_stripped)}, value={masked}")
    else:
        logger.warning(f"🔍 [DEBUG ENV] {env_name} is MISSING!")

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


# ──────────────────────────────────────────────────────────────
# [진행률 추적] 관리 화면에서 실시간 확인을 위한 진행률 파일 기록
# ──────────────────────────────────────────────────────────────
import json as _json

_PROGRESS_LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "logs"))

def write_progress(
    brand: str,
    step: str,
    current: int = 0,
    total: int = 0,
    current_item: str = "",
    phases_done: list = None,
    phases_remaining: list = None,
    status: str = "running",
    run_id: int = None,
    error: str = "",
    started_at: str = "",
):
    """파이프라인 진행 상황을 JSON 파일에 기록합니다. 관리 화면이 폴링으로 읽어 표시합니다."""
    import time as _time
    os.makedirs(_PROGRESS_LOG_DIR, exist_ok=True)
    path = os.path.join(_PROGRESS_LOG_DIR, f"progress_{brand.lower()}.json")
    percent = int((current / total * 100)) if total > 0 else 0
    data = {
        "brand": brand.upper(),
        "run_id": run_id,
        "step": step,
        "current": current,
        "total": total,
        "percent": percent,
        "current_item": current_item,
        "phases_done": phases_done or [],
        "phases_remaining": phases_remaining or [],
        "status": status,
        "error": error,
        "started_at": started_at,
        "updated_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"진행률 파일 쓰기 실패: {e}")


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
    import time as _time
    _started_at = _time.strftime("%Y-%m-%dT%H:%M:%S")
    _all_phases = ["카테고리 스캔", "상품 크롤링", "이미지 업로드", "임베딩 생성", "DB 저장"]

    logger.info(f"🚀 [{brand}] 크롤링 파이프라인 기동 (action={action}, limit={limit}, dry_run={dry_run}, force={force}, gender={gender}, category={category}, force_download={force_download})")
    
    # 윈도우 OS의 파일 ctime 보존 현상 방지를 위해 기존 진행률 파일 물리적 선 삭제
    try:
        progress_path = os.path.join(_PROGRESS_LOG_DIR, f"progress_{brand.lower()}.json")
        if os.path.exists(progress_path):
            os.remove(progress_path)
    except Exception as e:
        logger.warning(f"기존 진행 파일 삭제 실패: {e}")

    write_progress(brand, step="파이프라인 시작", current=0, total=limit,
                   phases_done=[], phases_remaining=_all_phases,
                   status="running", run_id=run_id, started_at=_started_at)
    
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
    
    write_progress(brand, step="카테고리 스캔 중", current=0, total=limit,
                   phases_done=[], phases_remaining=_all_phases,
                   status="running", run_id=run_id, started_at=_started_at)

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
                
                await p_page.goto(url, timeout=15000, wait_until="domcontentloaded")
                
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
            break_outer = False
            for gender_key, categories in target_map.items():
                if break_outer:
                    break
                # 성별 분할 필터링
                if gender and gender_key.lower() != gender.lower():
                    continue
                for category_key, urls in categories.items():
                    if break_outer:
                        break
                    # 카테고리 분할 필터링
                    if category and category_key.lower() != category.lower():
                        continue
                    
                    if isinstance(urls, str):
                        urls = [urls]
                    for target_url in urls:
                        if len(collected_products) >= limit:
                            break_outer = True
                            break
                        
                        logger.info(f"🔍 목록 스캔 중: {gender_key} - {category_key} - {target_url}")
                        
                        page = await context.new_page()
                        try:
                            await page.goto(target_url, timeout=15000, wait_until="domcontentloaded")
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
    
    total_collected = len(collected_products)
    logger.info(f"📦 [{brand}] Playwright 크롤링 완료. 수집 데이터 개수: {total_collected} 건")
    write_progress(brand, step="상품 크롤링 완료", current=total_collected, total=limit,
                   phases_done=["카테고리 스캔", "상품 크롤링"],
                   phases_remaining=["이미지 업로드", "임베딩 생성", "DB 저장"],
                   current_item=f"총 {total_collected}개 수집 완료",
                   status="running", run_id=run_id, started_at=_started_at)
    
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

        pending_errors = []  # 비동기 Neon DB 커넥션 폭주를 막기 위해 에러 로그를 일시 수집

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
                        # 기존 저장된 이미지가 로고로 의심되면 캐시 히트를 스킵하고 강제 재다운로드 유도
                        is_logo = any(kwd in db_img_path.lower() for kwd in ["logo", "topten10_mall", "goodwearmall", "og_goodwearmall", "og_toptenclub", "noimage"])
                        if is_logo:
                            logger.info(f"   ⚠️ [{brand.upper()}] {model_code} 기존 이미지 캐시가 로고로 의심되어 캐시를 우회합니다: {db_img_path}")
                        else:
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
                    # thumbnailImageUrl(ld+json 기반, 가장 정확한 대표 이미지)을 최우선 사용
                    # goodsImages는 필터링됐더라도 첫 번째가 로고일 수 있으므로 보조 수단으로만 사용
                    thumbnail_url = item.get("thumbnailImageUrl", "")
                    images = item.get("goodsImages", [])
                    
                    # img.goodwearmall.com 도메인 상품 이미지만 필터링
                    _LOGO_KWDS = ["static.goodwearmall", "topten10_mall", "og_goodwearmall", "noimage", "logo", "icon", "banner"]
                    valid_images = [
                        img for img in images
                        if img and not any(kw in img.lower() for kw in _LOGO_KWDS)
                    ]
                    
                    # 우선순위: ① thumbnailImageUrl ② img.goodwearmall.com 포함 상품 이미지 ③ 첫 번째 valid 이미지
                    if thumbnail_url and not any(kw in thumbnail_url.lower() for kw in _LOGO_KWDS):
                        primary_url = thumbnail_url
                    elif valid_images:
                        # img.goodwearmall.com 도메인 우선
                        product_imgs = [img for img in valid_images if "img.goodwearmall.com" in img]
                        primary_url = product_imgs[0] if product_imgs else valid_images[0]
                    else:
                        primary_url = None
                    
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
                             pending_errors.append(("IMAGE_UPLOAD_WARN", f"이미지 업로드 스킵 (기존 URL 유지): {upload_err}", prod_id, primary_url))
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

                    # 상품별 진행률 업데이트 (이미지 업로드 단계)
                    write_progress(
                        brand,
                        step=f"이미지 업로드 및 DB 저장",
                        current=success_db_count,
                        total=total_collected,
                        current_item=item.get("goodsNm", "")[:50],
                        phases_done=["카테고리 스캔", "상품 크롤링"],
                        phases_remaining=["임베딩 생성", "DB 커밋"],
                        status="running", run_id=run_id, started_at=_started_at
                    )

                    # 네이버 최저가 API 병행 실행 (항상 실행하여 실시간 가격 변동 추적)
                    logger.info(f"   🔍 네이버 쇼핑 API 검색 진행 중: {item.get('goodsNm')}")
                    original_name = item.get('goodsNm')
                    
                    # 1. 네이버 쇼핑 검색 최적화: | 구분자 및 뒤에 오는 브랜드 슬로건 등 쓰레기 단어 완전 분절
                    query_term = re.sub(r'\|.*$', '', original_name)
                    # 2. 성별 괄호 제거
                    query_term = re.sub(r'\([^\)]*\)|[남여]\)\s*', '', query_term)
                    # 3. 주요 브랜드 노이즈 단어 소거
                    query_term = re.sub(r'(TOPTEN10|topten10|8SECONDS|8seconds|탑텐10|탑텐|에잇세컨즈|UNIQLO|uniqlo|유니클로|굿웨어몰|goodwearmall)', '', query_term)
                    query_term = query_term.strip()
                    if len(query_term) < 2:
                        query_term = original_name
                    
                    nv_items = await search_naver_shopping_api(query_term, session)
                    nv_count = len(nv_items) if nv_items else 0
                    if nv_count < 5:
                        msg = f"네이버 최저가 매칭 수량 부족: {original_name} (검색어: {query_term}) - 매칭 건수: {nv_count}/5개"
                        pending_errors.append(("NAVER_API_WARN", msg, prod_id, None))
                    
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
                
                # ① 임베딩 생성 전 먼저 DB를 커밋하고 연결 목록을 조회해 메모리에 캐시
                # → Neon DB SSL 연결이 임베딩 생성 중 타임아웃으로 끊기는 현상 방지
                dw_conn.commit()
                dw_cur.execute("""
                    SELECT product_id, img_url, prod_name, gender, category_code 
                    FROM staging_products 
                    WHERE brand_name = %s
                """, (brand.upper(),))
                inserted_products = dw_cur.fetchall()
                
                # ② DW DB 연결을 임베딩 생성 전에 명시적으로 닫기 (장시간 유휴 방지)
                dw_cur.close()
                dw_conn.close()
                logger.info("🔌 임베딩 생성 전 DW DB 연결 정리 완료 (재연결 예정)")
                write_progress(brand, step="임베딩 생성 중", current=0, total=total_collected,
                               phases_done=["카테고리 스캔", "상품 크롤링", "이미지 업로드"],
                               phases_remaining=[f"임베딩 생성 ({total_collected}개)", "DB 저장"],
                               current_item=f"0/{total_collected}상품 임베딩 시작...",
                               status="running", run_id=run_id, started_at=_started_at)
                
                hf_token = os.getenv("HF_TOKEN")
                
                sem = asyncio.Semaphore(5)  # API Rate limit 방지 동시성 제약
                
                async def generate_and_save_embed(session, prod_id, img_url, prod_name, gender, category):
                    async with sem:
                        image_vector = None
                        text_vector = None
                        
                        try:
                            # 1. YOLO-CLIP 통합 이미지 임베딩 추출
                            if img_url:
                                image_vector = await get_yolo_clip_image_embedding(session, img_url)
                            
                            # 2. HuggingFace CLIP 텍스트 임베딩 추출 (차단 시 로컬 CLIP 모델 폴백)
                            if prod_name:
                                text_vector = await get_clip_text_embedding(session, prod_name, hf_token)
                        except Exception as embed_err:
                            logger.warning(f"⚠️ 상품 {prod_id} 임베딩 생성 중 오류 (스킵): {embed_err}")
                            pending_errors.append(("EMBEDDING_WARN", f"임베딩 생성 오류 (스킵): {embed_err}", prod_id, img_url))
                            
                        # 예외는 발생하지 않았으나 환경변수 미비/오류 등으로 결과가 None인 스킵 케이스에 대해 명시적 로깅 연동
                        if img_url and not image_vector:
                            pending_errors.append(("EMBEDDING_WARN", f"이미지 임베딩 추출 결과가 None입니다. (HF_SPACE_URL 설정 상태 확인 필요)", prod_id, img_url))
                        if prod_name and not text_vector:
                            pending_errors.append(("EMBEDDING_WARN", f"텍스트 임베딩 결과가 None입니다. (API 호출 실패 또는 유효성 확인 필요)", prod_id, None))

                        if image_vector or text_vector:
                            return (prod_id, image_vector, text_vector, brand.upper(), category, gender, img_url)
                        return None

                # 기존 Core DB에서 이미 생성된 임베딩 캐시가 있는지 일괄 조회하여 API 호출 최소화 (리소스 낭비 방지)
                embedding_cache = {}
                try:
                    prod_conn = get_prod_db_connection()
                    prod_cur = prod_conn.cursor()
                    prod_ids = [row[0] for row in inserted_products]
                    if prod_ids:
                        prod_cur.execute("""
                            SELECT product_id, image_vector, text_vector 
                            FROM product_embeddings 
                            WHERE product_id = ANY(%s) AND image_vector IS NOT NULL AND text_vector IS NOT NULL
                        """, (prod_ids,))
                        for p_id, img_vec, txt_vec in prod_cur.fetchall():
                            embedding_cache[p_id] = (img_vec, txt_vec)
                    prod_cur.close()
                    prod_conn.close()
                    logger.info(f"💾 Core DB로부터 기존 임베딩 캐시 {len(embedding_cache)}건을 로드했습니다. (연산 생략 예정)")
                except Exception as cache_err:
                    logger.warning(f"⚠️ 기존 임베딩 캐시 로드 중 예외 발생 (새로 생성함): {cache_err}")

                # 개별 상품 임베딩이 완료될 때마다 진행률을 기록하고 DB에 즉시 실시간 적재
                async with aiohttp.ClientSession() as session:
                    tasks = []
                    cached_results = []
                    
                    for row in inserted_products:
                        prod_id, img_url, prod_name, gender, category = row
                        
                        # 캐시 히트 조건 체크
                        if not force_download and prod_id in embedding_cache:
                            img_vec, txt_vec = embedding_cache[prod_id]
                            cached_results.append((prod_id, img_vec, txt_vec, brand.upper(), category, gender, img_url))
                            logger.info(f"   💾 상품 {prod_id} ({prod_name[:20]}) -> 기존 임베딩 캐시 히트 (연산 스킵)")
                        else:
                            tasks.append(generate_and_save_embed(session, prod_id, img_url, prod_name, gender, category))
                    
                    completed_count = 0
                    valid_embed_count = 0
                    total_collected = len(inserted_products)
                    
                    # 1. 캐시 히트된 데이터 먼저 실시간 DB 적재
                    for res in cached_results:
                        completed_count += 1
                        try:
                            def _save_to_db(row_data):
                                conn = get_dw_db_connection()
                                cur = conn.cursor()
                                try:
                                    cur.execute("""
                                        INSERT INTO staging_product_embeddings (
                                            product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                                        )
                                        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                                        ON CONFLICT (product_id) DO UPDATE
                                        SET image_vector = EXCLUDED.image_vector,
                                            text_vector = EXCLUDED.text_vector,
                                            create_dt = CURRENT_TIMESTAMP
                                    """, row_data)
                                    conn.commit()
                                finally:
                                    cur.close()
                                    conn.close()
                            
                            await asyncio.to_thread(_save_to_db, res)
                            valid_embed_count += 1
                        except Exception as save_err:
                            logger.error(f"❌ 임베딩 실시간 DB 적재 실패 (상품 {res[0]}): {save_err}")
                            
                        # 진행 정보 파일 업데이트 (실시간)
                        write_progress(brand, step="임베딩 생성 중", current=completed_count, total=total_collected,
                                       phases_done=["카테고리 스캔", "상품 크롤링", "이미지 업로드"],
                                       phases_remaining=[f"임베딩 생성 ({total_collected}개)", "DB 저장"],
                                       current_item=f"상품 {res[0]} 캐시 적용 완료",
                                       status="running", run_id=run_id, started_at=_started_at)
                    
                    # 2. 캐시 미스된 데이터 비동기 연산 및 실시간 DB 적재
                    if tasks:
                        for coro in asyncio.as_completed(tasks):
                            res = await coro  # res: (prod_id, image_vector, text_vector, brand, category, gender, img_url)
                            completed_count += 1
                            
                            # 1. DB 실시간 적재
                            if res:
                                try:
                                    # 동기 DB 커넥션을 열고 한 건 저장 후 즉시 닫음 (Neon DB 안정성 극대화)
                                    def _save_to_db(row_data):
                                        conn = get_dw_db_connection()
                                        cur = conn.cursor()
                                        try:
                                            cur.execute("""
                                                INSERT INTO staging_product_embeddings (
                                                    product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                                                )
                                                VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                                                ON CONFLICT (product_id) DO UPDATE
                                                SET image_vector = EXCLUDED.image_vector,
                                                    text_vector = EXCLUDED.text_vector,
                                                    create_dt = CURRENT_TIMESTAMP
                                            """, row_data)
                                            conn.commit()
                                        finally:
                                            cur.close()
                                            conn.close()
                                    
                                    await asyncio.to_thread(_save_to_db, res)
                                    valid_embed_count += 1
                                except Exception as save_err:
                                    logger.error(f"❌ 임베딩 실시간 DB 적재 실패 (상품 {res[0]}): {save_err}")
                            
                            # 2. 진행 정보 파일 업데이트 (실시간)
                            current_item_str = f"임베딩 처리 중... ({completed_count}/{total_collected})"
                            if res and len(res) > 0 and res[0]:
                                current_item_str = f"상품 {res[0]} 임베딩 완료 및 DB 저장"
                                
                            write_progress(brand, step="임베딩 생성 중", current=completed_count, total=total_collected,
                                           phases_done=["카테고리 스캔", "상품 크롤링", "이미지 업로드"],
                                           phases_remaining=[f"임베딩 생성 ({total_collected}개)", "DB 저장"],
                                           current_item=current_item_str,
                                           status="running", run_id=run_id, started_at=_started_at)
                
                logger.info(f"✨ staging_product_embeddings 테이블에 실시간으로 {valid_embed_count}건 임베딩 벡터 적재 완료.")
            
            # 최종 DB 연결 수립 후 트랜잭션 정상 종료 처리 (이전 단계에서 다 끝났으므로 닫아줄 커넥션만 마무리)
            logger.info("🔌 최종 스테이징 적재 정리 완료. (Phase 1 완료)")
            dw_conn = get_dw_db_connection()
            dw_conn.commit()
            logger.info(f"✅ 스테이징 테이블 {success_db_count}건 임시 적재 완료. (Phase 1 완료)")
            write_progress(brand, step="완료", current=success_db_count, total=total_collected,
                           phases_done=["카테고리 스캔", "상품 크롤링", "이미지 업로드", "임베딩 생성", "DB 저장"],
                           phases_remaining=[],
                           current_item=f"전체 {success_db_count}건 완료",
                           status="done", run_id=run_id, started_at=_started_at)
            
            # 수집된 pending_errors 일괄 DB 적재 처리 (Neon DB 동시 커넥션 수 초과 방지)
            if pending_errors and not dry_run:
                logger.info(f"📝 펜딩된 에러 로그 {len(pending_errors)}건을 단일 세션으로 일괄 적재 진행 중...")
                for err_type, err_msg, p_id, src_url in pending_errors:
                    try:
                        # log_pipeline_error 내부에서 별도로 커넥션을 열어 처리하도록 설계되어 있으므로 순차 실행
                        log_pipeline_error(run_id, err_type, err_msg, product_id=p_id, source_url=src_url)
                    except Exception as log_err:
                        logger.error(f"❌ 펜딩 에러 일괄 적재 중 예외: {log_err}")

            return {"total_items": success_db_count, "new_items": new_items_count, "updated_items": updated_items_count, "embed_count": valid_embed_count}
            
        except Exception as db_err:
            write_progress(brand, step="오류 발생", current=0, total=0,
                           phases_done=[], phases_remaining=[],
                           status="error", error=str(db_err)[:200],
                           run_id=run_id, started_at=_started_at)
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
            embed_val = metrics.get("embed_count", 0) if metrics else 0
            log_pipeline_end(run_id, "SUCCESS", total_items=total, new_items=new_val, updated_items=upd_val, embed_count=embed_val)
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
