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

# musinsa (주석 처리)
# try:
#     import scraper_musinsa as s_ms
#     BRAND_CRAWLER_MODELS["musinsa"] = s_ms
# except ImportError as e:
#     logger.warning(f"musinsa scraper 임포트 에러: {e}")

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

# zara (주석 처리)
# try:
#     import scraper_zara as s_zr
#     BRAND_CRAWLER_MODELS["zara"] = s_zr
# except ImportError as e:
#     logger.warning(f"zara scraper 임포트 에러: {e}")

# spao (추가)
try:
    import scraper_spao as s_sp
    BRAND_CRAWLER_MODELS["spao"] = s_sp
except ImportError as e:
    logger.warning(f"spao scraper 임포트 에러: {e}")

# giordano (추가)
try:
    import scraper_giordano as s_gd
    BRAND_CRAWLER_MODELS["giordano"] = s_gd
except ImportError as e:
    logger.warning(f"giordano scraper 임포트 에러: {e}")

# polham (추가)
try:
    import scraper_polham as s_ph
    BRAND_CRAWLER_MODELS["polham"] = s_ph
except ImportError as e:
    logger.warning(f"polham scraper 임포트 에러: {e}")

# spao (추가)
try:
    import scraper_spao as s_sp
    BRAND_CRAWLER_MODELS["spao"] = s_sp
except ImportError as e:
    logger.warning(f"spao scraper 임포트 에러: {e}")

# giordano (추가)
try:
    import scraper_giordano as s_gd
    BRAND_CRAWLER_MODELS["giordano"] = s_gd
except ImportError as e:
    logger.warning(f"giordano scraper 임포트 에러: {e}")

# polham (추가)
try:
    import scraper_polham as s_ph
    BRAND_CRAWLER_MODELS["polham"] = s_ph
except ImportError as e:
    logger.warning(f"polham scraper 임포트 에러: {e}")


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
    target_counts: dict = None,
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
        "target_counts": target_counts or {"Outer": 0, "Top": 0, "Bottom": 0},
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
    force_download: bool = False, product_id: str = None
):
    """
    특정 브랜드의 Playwright 크롤링 실행 및 
    Cloudinary 실시간 staging 업로드 -> Neon DB 스테이징 테이블 적재 -> 원자적 교체 프로세스.
    """
    import time as _time
    _started_at = _time.strftime("%Y-%m-%dT%H:%M:%S")
    _all_phases = ["카테고리 스캔", "상품 크롤링", "이미지 업로드", "임베딩 생성", "DB 저장"]

    logger.info(f"🚀 [{brand}] 크롤링 파이프라인 기동 (action={action}, limit={limit}, dry_run={dry_run}, force={force}, gender={gender}, category={category}, force_download={force_download}, product_id={product_id})")
    
    # [시작 시각 보존] 어드민 백엔드가 버튼 클릭 즉시 기록한 started_at이 있다면,
    # 파이썬 기동 대기 시간(프로세스 로딩 시간 등)을 전체 경과 시간에 누락 없이 반영하기 위해 덮어쓰지 않고 계승합니다.
    progress_path = os.path.join(_PROGRESS_LOG_DIR, f"progress_{brand.lower()}.json")
    if os.path.exists(progress_path):
        try:
            # 윈도우 찌꺼기 파일 식별 방어: 파일의 마지막 수정 시각이 60초 이내인 경우에만 방금 어드민이 기록한 시작 시간으로 인정
            file_mtime = os.path.getmtime(progress_path)
            if _time.time() - file_mtime < 60:
                with open(progress_path, "r", encoding="utf-8") as pf:
                    existing_data = _json.load(pf)
                    if existing_data.get("started_at"):
                        _started_at = existing_data.get("started_at")
                        logger.info(f"💾 어드민 백엔드가 기록한 최초 시작 시각을 계승합니다: {_started_at}")
            else:
                logger.info("⏳ 기존 진행률 파일이 오래되어(60초 초과) 시작 시각을 계승하지 않고 현재 시각을 사용합니다.")
        except Exception as read_err:
            logger.warning(f"기존 시작 시간 조회 실패: {read_err}")

    # 윈도우 OS의 파일 ctime 보존 현상 방지를 위해 기존 진행률 파일 물리적 선 삭제
    try:
        if os.path.exists(progress_path):
            os.remove(progress_path)
    except Exception as e:
        logger.warning(f"기존 진행 파일 삭제 실패: {e}")

    # 홈페이지 실제 총 상품 수 카운팅 딕셔너리
    target_counts = {"Outer": 0, "Top": 0, "Bottom": 0}

    write_progress(brand, step="파이프라인 시작", current=0, total=limit,
                   phases_done=[], phases_remaining=_all_phases,
                   status="running", run_id=run_id, started_at=_started_at,
                   target_counts=target_counts)
    
    # [Phase 2] 24시간 검증 후 프로덕션 스위칭만 진행 (크롤링 스킵)
    if action == "swap":
        # 스위칭 시작 단계 진행 상태 업데이트
        write_progress(brand, step="스위칭 진행 중", current=0, total=1,
                       phases_done=[], phases_remaining=["이관 및 스위칭"],
                       status="running", run_id=run_id, started_at=_started_at,
                       target_counts=target_counts)
        if not dry_run:
            swap_success = await swap_staging_to_production(brand, force=force)
            if not swap_success:
                write_progress(brand, step="스위칭 실패", current=0, total=1,
                               phases_done=[], phases_remaining=[],
                               status="error", error=f"[{brand}] 데이터 스위칭 실패",
                               run_id=run_id, started_at=_started_at,
                               target_counts=target_counts)
                raise RuntimeError(f"[{brand}] 데이터 프로덕션 스위칭 중 오류가 발생했습니다. 차단 이력을 점검하세요.")
        else:
            logger.info("ℹ️ [DRY RUN] 스위칭 및 이미지 이관 로직을 실행하지 않고 패스합니다.")
        
        # 스위칭 성공적으로 종료 시 progress를 완료 상태로 마킹
        write_progress(brand, step="완료", current=1, total=1,
                       phases_done=["이관 및 스위칭"], phases_remaining=[],
                       current_item="이관 및 스위칭 완료",
                       status="done", run_id=run_id, started_at=_started_at,
                       target_counts=target_counts)
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
                   status="running", run_id=run_id, started_at=_started_at,
                   target_counts=target_counts)

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
            # elif brand == "musinsa":
            #     url = f"https://www.musinsa.com/products/{product_id}"
            elif brand == "topten":
                url = f"https://topten10.goodwearmall.com/product/{product_id}/detail"
            elif brand == "uniqlo":
                url = f"https://www.uniqlo.com/kr/ko/products/{product_id.split('?')[0]}"
            # elif brand == "zara":
            #     url = f"https://www.zara.com/kr/ko/man-outerwear-l715.html"
            #     if product_id.startswith("http"): 
            #         url = product_id
            elif brand == "spao":
                url = f"https://www.spao.com/i/item?itemNo={product_id}"
            elif brand == "giordano":
                url = f"https://www.giordano.co.kr/shop/detail.php?pno={product_id}"
            elif brand == "polham":
                url = f"https://polham.goodwearmall.com/product/{product_id}/detail"
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
                # elif brand == "musinsa":
                #     product_dict = await scraper.extract_attribute_focus_data(p_page)
                elif brand == "topten":
                    product_dict = await scraper.extract_product_data_from_dom(p_page)
                elif brand == "uniqlo":
                    product_dict = await scraper.extract_product_base_data(p_page, product_id)
                    if product_dict:
                        product_dict["goodsImages"] = await scraper.extract_current_images(p_page)
                # elif brand == "zara":
                #     product_dict = await scraper.extract_product_data_from_dom(p_page)
                elif brand in ["spao", "giordano", "polham"]:
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
        nonlocal limit
        async with async_playwright() as p:
            # 봇 감지 솔루션(Akamai, Cloudflare) 우회를 위해 로컬 Chrome/Edge 채널을 우선 사용하고 실패 시 Firefox 및 일반 Chromium 순으로 폴백합니다.
            # 불필요한 대기를 방지하기 위해 각 launch 시도에 5초(5000ms)의 타임아웃을 명시합니다.
            browser = None
            launch_args = ["--disable-blink-features=AutomationControlled"]
            try:
                logger.info("🌐 봇 감지 우회를 위해 로컬 Chrome 채널을 사용하여 브라우저를 시작합니다.")
                browser = await p.chromium.launch(headless=True, channel="chrome", args=launch_args, timeout=5000)
            except Exception as e1:
                logger.warning(f"⚠️ Chrome 채널 실행 실패 ({e1}). Edge 채널로 시도합니다...")
                try:
                    browser = await p.chromium.launch(headless=True, channel="msedge", args=launch_args, timeout=5000)
                except Exception as e2:
                    logger.warning(f"⚠️ Edge 채널 실행 실패 ({e2}). Firefox 브라우저로 시도합니다...")
                    try:
                        browser = await p.firefox.launch(headless=True, timeout=5000)
                    except Exception as e3:
                        logger.error(f"⚠️ Firefox 실행 실패 ({e3}). 일반 Chromium 헤드리스로 최종 폴백합니다.")
                        browser = await p.chromium.launch(headless=True, args=launch_args, timeout=5000)
                
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="ko-KR",
                timezone_id="Asia/Seoul"
            )
            
            # 자라와 무신사 모두 봇 탐지 우회를 위해 stealth 스크립트 주입
            if brand.lower() in ["zara", "musinsa"]:
                stealth_js = """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
                """
                await context.add_init_script(stealth_js)

            target_map = scraper.TARGET_MAP
            all_target_products = [] # (product_code, gender_key, category_key) 튜플 리스트

            # 수동 vs 자동 모드 판단 선언
            # limit > 0 이고 GITHUB_ACTIONS가 아니면 수동/어드민 입력 모드로 간주
            is_manual_mode = (limit > 0) and (os.getenv("GITHUB_ACTIONS") != "true")
            _mode_desc = f"수동 (limit={limit}개 조기종료 적용)" if is_manual_mode else "자동 (전체 사이트 스캔)"
            logger.info(f"🔄 [수동/자동 모드] {_mode_desc}")

            # 1단계: 전체 지정 카테고리 목록 스캔 및 통계 산출
            logger.info("🔍 [Step 1] 전체 카테고리 목록 스캔 및 홈페이지 수량 분석 개시...")
            scan_finished = False
            for gender_key, categories in target_map.items():
                if scan_finished:
                    break
                # 성별 분할 필터링
                if gender and gender_key.lower() != gender.lower():
                    continue
                for category_key, urls in categories.items():
                    if scan_finished:
                        break
                    # 카테고리 분할 필터링
                    if category and category_key.lower() != category.lower():
                        continue
                    
                    if isinstance(urls, str):
                        urls = [urls]
                    for target_url in urls:
                        if scan_finished:
                            break
                        logger.info(f"🔍 목록 스캔 중: {gender_key} - {category_key} - {target_url}")
                        
                        product_codes = []
                        page_num = 1
                        max_pages = 50  # 수집 안전 상한값
                        if brand.lower() in ["uniqlo", "zara"]:
                            max_pages = 1  # 무한 스크롤 사이트는 단일 페이지 내에서 스크롤을 길게 뺌
                            
                        has_detected_count = False
                        
                        while page_num <= max_pages:
                            # 페이지별 URL 파라미터 분기
                            current_url = target_url
                            if brand.lower() == "musinsa":
                                sep = "&" if "?" in target_url else "?"
                                current_url = f"{target_url}{sep}page={page_num}"
                            elif brand.lower() == "8seconds":
                                sep = "&" if "?" in target_url else "?"
                                current_url = f"{target_url}{sep}pageNo={page_num}"
                            elif brand.lower() == "topten":
                                sep = "&" if "?" in target_url else "?"
                                current_url = f"{target_url}{sep}pageIdx={page_num}"
                                
                            logger.info(f"   📄 목록 페이지 {page_num} 스캔 시도: {current_url}")
                            page = await context.new_page()
                            # ★ 자라 전용 Playwright API 인터셉터 등록
                            if brand.lower() == "zara":
                                page._zara_intercepted_products = []
                                async def _zara_response_handler(response):
                                    if "itxrest" in response.url and response.status == 200:
                                        try:
                                            body = await response.json()
                                            products_found = []
                                            if isinstance(body, dict):
                                                if "products" in body and isinstance(body["products"], list):
                                                    products_found = body["products"]
                                                elif "productGroups" in body:
                                                    for group in body.get("productGroups", []):
                                                        for elem in group.get("elements", []):
                                                            if "commercialComponents" in elem:
                                                                for comp in elem["commercialComponents"]:
                                                                    if comp.get("type") == "Product":
                                                                        products_found.append(comp)
                                            for prod in products_found:
                                                parsed = scraper.parse_product_from_api(prod)
                                                if parsed:
                                                    page._zara_intercepted_products.append(parsed)
                                        except Exception:
                                            pass
                                page.on("response", _zara_response_handler)
                            try:
                                await page.set_viewport_size({"width": 1280, "height": 800})
                                await page.goto(current_url, timeout=60000, wait_until="domcontentloaded")
                                
                                # 봇 탐지 우회 및 JS 렌더링 완료 대기 (수동 모드일 때는 대기 시간을 2초로 대폭 단축하여 극적인 속도 향상)
                                if is_manual_mode:
                                    await asyncio.sleep(2.0)
                                elif brand.lower() in ["zara", "musinsa", "uniqlo", "topten"]:
                                    await asyncio.sleep(8.0)
                                else:
                                    await asyncio.sleep(2.0)
                                    
                                # 지연 렌더링 대응 스크롤
                                scroll_limit = 3
                                if brand.lower() in ["uniqlo", "zara"]:
                                    # 수동 모드일 때는 스크롤 다운 횟수를 최대 2회로 제한하여 빠른 결과를 도출
                                    scroll_limit = 2 if is_manual_mode else 25  # 무한 스크롤
                                    
                                last_height = await page.evaluate("document.body.scrollHeight")
                                for _ in range(scroll_limit):
                                    await page.evaluate("window.scrollBy(0, 2500)")
                                    await asyncio.sleep(0.8)
                                    if brand.lower() in ["uniqlo", "zara"]:
                                        new_height = await page.evaluate("document.body.scrollHeight")
                                        if new_height == last_height:
                                            break
                                        last_height = new_height
                                        
                                p_codes = []
                                if brand == "8seconds":
                                    p_codes = await page.evaluate("""() => 
                                        Array.from(document.querySelectorAll('li.god-item')).map(item => item.getAttribute('view-godno')).filter(c => c !== null)
                                    """)
                                # elif brand == "musinsa":
                                #     import re
                                #     hrefs = await page.evaluate("""() => Array.from(document.querySelectorAll('a')).map(a => a.href)""")
                                #     for h in hrefs:
                                #         m = re.search(r'(?:goods|products)/(\d+)', h)
                                #         if m: p_codes.append(m.group(1))
                                elif brand in ["topten", "polham"]:
                                    p_codes = await page.evaluate("""() => {
                                        const ids = new Set();
                                        document.querySelectorAll('a[href*="/product/"]').forEach(a => {
                                            const m = a.href.match(/\/product\/([A-Z0-9]{8,20})\/detail/);
                                            if (m) ids.add(m[1]);
                                        });
                                        document.querySelectorAll('[data-goods-no], [data-goodsno]').forEach(el => {
                                            const id = el.getAttribute('data-goods-no') || el.getAttribute('data-goodsno');
                                            if (id) ids.add(id.toUpperCase());
                                        });
                                        return Array.from(ids);
                                    }""")
                                    if not p_codes:
                                        import re
                                        content = await page.content()
                                        matches = re.findall(r"[A-Z]{3}\d[A-Z]{2}\d{4}[A-Z0-9]+", content)
                                        p_codes = [pid for pid in matches if 10 <= len(pid) <= 15]
                                elif brand == "spao":
                                    p_codes = await page.evaluate("""() => {
                                        const ids = new Set();
                                        document.querySelectorAll('a[href*="itemNo="]').forEach(a => {
                                            const m = a.href.match(/itemNo=([0-9]+)/i);
                                            if (m) ids.add(m[1]);
                                        });
                                        return Array.from(ids);
                                    }""")
                                elif brand == "giordano":
                                    p_codes = await page.evaluate("""() => {
                                        const ids = new Set();
                                        document.querySelectorAll('a[href*="pno="]').forEach(a => {
                                            const m = a.href.match(/pno=([A-Z0-9]+)/i);
                                            if (m) ids.add(m[1]);
                                        });
                                        return Array.from(ids);
                                    }""")
                                elif brand == "uniqlo":
                                    import re
                                    content = await page.content()
                                    matches = re.findall(r"/products/([A-Z0-9-]+)", content)
                                    p_codes = [pid for pid in matches if len(pid) >= 5 and "review" not in pid]
                                # elif brand == "zara":
                                #     import re
                                #     # Playwright API Response 인터셉터로 캡처된 데이터를 collected_products에 즉시 추가
                                #     _zara_intercepted = getattr(page, "_zara_intercepted_products", [])
                                #     logger.info(f"    Zara API Interceptor: {len(_zara_intercepted)} items captured.")
                                #     for item in _zara_intercepted:
                                #         if item and item.get("goodsNo"):
                                #             # 이미 수집된 목록에 중복 방지하며 추가
                                #             pid = item["goodsNo"]
                                #             item["gender"] = gender_key
                                #             item["category"] = category_key
                                #             if not any(x.get("goodsNo") == pid for x in collected_products) and len(collected_products) < limit:
                                #                 collected_products.append(item)
                                #                 logger.info(f"   ➕ [ZARA API] 수집 성공 ({len(collected_products)}/{limit}): {item['goodsNm']}")
                                # 
                                #     # 기존 링크 파싱도 폴백으로 실행하여 crawling_pipeline_cli의 flow를 해치지 않음
                                #     links = await page.evaluate("""() => Array.from(document.querySelectorAll('a[href*="-p"][href*=".html"]')).map(a => a.href)""")
                                #     for link in links:
                                #         match = re.search(r'-p([0-9]+)\.html', link)
                                #         if match:
                                #             p_codes.append(link.split('?')[0])
                                p_codes = list(set(p_codes))
                                if not p_codes:
                                    # WAF/지연 등으로 일시적으로 비어있을 수 있으므로 3페이지 연속 비어있는 경우에만 break
                                    if page_num > 3:
                                        logger.info("   ⏹ 더 이상 추출된 상품 코드가 없습니다. 페이지 순회를 종료합니다.")
                                        await page.close()
                                        break
                                    else:
                                        logger.warning(f"   ⚠️ {page_num}페이지 상품 코드 미검출. 다음 페이지로 계속 진행합니다.")
                                        await page.close()
                                        page_num += 1
                                        continue
                                    
                                # 수집된 신규 코드들을 all_target_products에 실시간으로 중복 없이 추가
                                added_count = 0
                                for code in p_codes:
                                    if not any(x[0] == code for x in all_target_products):
                                        all_target_products.append((code, gender_key, category_key))
                                        added_count += 1

                                product_codes.extend(p_codes)
                                product_codes = list(set(product_codes))
                                
                                logger.info(f"   🔗 {page_num}페이지 스캔 결과: 신규 식별 {added_count}개 (전체 누적 {len(all_target_products)}개)")
                                
                                # [즉각 조기종료 반영] 수동 모드에서 특정 카테고리가 지정되었고 목표 상품수(limit)를 충분히 확보했다면 즉시 스캔 종료
                                # 전체(None) 수집 시에는 Outer만 수집되는 것을 방지하기 위해 목록 스캔의 조기종료를 건너뜁니다.
                                if is_manual_mode and category and len(all_target_products) >= limit:
                                    logger.info(f"   ⏸️ [수동 단일카테고리 조기종료] 목표 수량({limit}개)을 확보하여 목록 스캔을 즉시 중단합니다. (누적: {len(all_target_products)}개)")
                                    scan_finished = True
                                    await page.close()
                                    break

                                # 신규 식별이 0개이더라도 중복 배치 섞임이 있을 수 있으므로 조기 break를 완화
                                # 연속 5페이지 이상 신규 식별이 없거나 전체 limit을 넘길 때 탈출하도록 안전장치 변경
                                if added_count == 0 and page_num > 10:
                                    logger.info("   ⏹ 10페이지 이후 신규 상품 식별 0개 도달. 페이지 순회를 종료합니다.")
                                    await page.close()
                                    break
                                    
                                # 홈페이지 전체 상품 수량 파싱 (최초 1페이지에서만 실행)
                                if not has_detected_count:
                                    cat_target_count = 0
                                    try:
                                        # WAF 차단 체크
                                        page_content = await page.content()
                                        if "Access Denied" in page_content or "don't have permission to access" in page_content:
                                            logger.warning(f"⚠️ [{brand.upper()}] WAF 차단 감지 (Access Denied)")
                                            cat_target_count = -1
                                        else:
                                            cat_target_count = await page.evaluate("""(brnd) => {
                                                let selectors = [];
                                                if (brnd === '8seconds') {
                                                    selectors = ['#godTotalCount', '.gods-total span', '.sub-category-title span'];
                                                } else if (brnd === 'musinsa') {
                                                    selectors = ['[class*="Header__Count"]', '.Header__Count-sc-', '.Header__Count'];
                                                } else if (brnd === 'topten' || brnd === 'polham') {
                                                    selectors = ['.utility-number', '.st-goods-total', '.st-total', 'div.total-count', '.utility-bar .total', '[class*="total"]'];
                                                } else if (brnd === 'uniqlo') {
                                                    selectors = ['.fr-ec-header-overlay__item-count', '.fr-ec-product-count', '.product-count', '.fr-ec-search-result-number'];
                                                } else if (brnd === 'spao') {
                                                    selectors = ['.goods_total_count', '.total', '.count', '.total-count'];
                                                } else if (brnd === 'giordano') {
                                                    selectors = ['.total', '.prd_total', '.total-goods', '.count'];
                                                } else {
                                                    selectors = [
                                                        '.total-count', '.goods-list-total', '.goods-list .total',
                                                        '.gods-total span', '.gods-total strong', '.num',
                                                        'span.count', '.total-goods', '.prod-count', '.prodCount',
                                                        '.product-count', '.num-items',
                                                        '.product-grid-header__product-count', 'span.product-count', '.result-count'
                                                    ];
                                                }
                                                for (const selector of selectors) {
                                                    const el = document.querySelector(selector);
                                                    if (el) {
                                                        const text = el.innerText.trim();
                                                        const num = text.replace(/[^0-9]/g, '');
                                                        if (num) {
                                                            const parsed = parseInt(num, 10);
                                                            if (parsed > 0) return parsed;
                                                        }
                                                    }
                                                }
                                                // ZARA는 수량이 텍스트로 존재하지 않으므로, 상품 링크 개수를 센다.
                                                if (brnd === 'zara') {
                                                    const links = Array.from(document.querySelectorAll('a[href*="-p"][href*=".html"]')).map(a => a.href.split('?')[0]);
                                                    const uniqueLinks = Array.from(new Set(links));
                                                    if (uniqueLinks.length > 0) return uniqueLinks.length;
                                                }
                                                return 0;
                                            }""", brand.lower())
                                    except Exception as parse_err:
                                        logger.warning(f"⚠️ [{brand.upper()}] 전체 상품 수 엘리먼트 파싱 에러: {parse_err}")
                                        
                                    if cat_target_count > 0:
                                        target_counts[category_key] += cat_target_count
                                        logger.info(f"   📊 [{brand.upper()}] {gender_key} - {category_key} 홈페이지 총 수량: {cat_target_count}개 감지")
                                    elif cat_target_count == -1:
                                        # WAF 차단 시 기본값 설정 스킵
                                        pass
                                    has_detected_count = True
                                    
                                await page.close()
                                page_num += 1
                            except Exception as scan_err:
                                logger.error(f"❌ 카테고리 목록 스캔 실패 (페이지 {page_num}): {scan_err}")
                                if not page.is_closed():
                                    await page.close()
                                break
                                
                        # 최종 폴백 카운트 누적
                        if target_counts[category_key] <= 0:
                            fallback_cnt = len(product_codes)
                            target_counts[category_key] = fallback_cnt
                            logger.info(f"   📊 [{brand.upper()}] {gender_key} - {category_key} 홈페이지 최종 식별된 갯수({fallback_cnt}개)로 총 수량 누적 적용")
            
            # [수동 모드 "전체" 수집 시] 카테고리 편중 방지를 위해 라운드 로빈(교대) 재배열 알고리즘 적용
            if is_manual_mode and not category: # category 인자가 없을 때가 "전체" 수집인 경우
                cat_buckets = {} # category_key -> list
                for code, gender_key, category_key in all_target_products:
                    cat_buckets.setdefault(category_key, []).append((code, gender_key, category_key))
                
                # 라운드 로빈 재배열 실행
                shuffled_targets = []
                has_items = True
                while has_items:
                    has_items = False
                    for c_key in sorted(cat_buckets.keys()):
                        if cat_buckets[c_key]:
                            shuffled_targets.append(cat_buckets[c_key].pop(0))
                            has_items = True
                
                all_target_products = shuffled_targets
                logger.info(f"🔀 [라운드 로빈 믹싱] 전체 수집 편중 방지를 위해 카테고리별(Outer/Top/Bottom) 교대 재배열 완료.")

            # 1단계 완료 후, 수집 제한량(limit) 및 진행률 total 동적 보완
            total_scanned_count = sum(target_counts.values())
            logger.info(f"📊 [스캔 완료] 홈페이지 기준 전체 타겟 수: {total_scanned_count}개, 수집된 식별 코드 수: {len(all_target_products)}개")
            
            if total_scanned_count > 0:
                if not is_manual_mode:
                    # 자동 모드: 스캔한 식별 코드 전체를 limit으로 설정
                    limit = max(limit, len(all_target_products))
                    logger.info(f"⚙️ 수집 제한량(limit)을 실제 식별된 상품 총 개수({limit}개)로 자동 상향 갱신합니다.")
                else:
                    # [수동 모드 정합성 개선] 홈페이지 실시간 스캔 감지 수량을 목표 카운트에서 지우고, 사용자가 세운 limit 분배치로 정합화
                    # 3개 카테고리에 limit을 라운드 로빈으로 배분
                    for k in list(target_counts.keys()):
                        target_counts[k] = 0
                    
                    if category: # 단일 카테고리 지정 구동 시
                        target_counts[category] = limit
                    else: # 전체(all) 카테고리 구동 시
                        # 라운드 로빈 배분 시뮬레이션으로 정확한 몫 계산
                        categories_list = ["Outer", "Top", "Bottom"]
                        temp_limit = limit
                        idx = 0
                        while temp_limit > 0:
                            cat_key = categories_list[idx % 3]
                            target_counts[cat_key] += 1
                            temp_limit -= 1
                            idx += 1
                            
                    logger.info(f"📊 [수동 모드 목표 정합화 완료] 수집 제한: {limit}개, 카테고리별 목표: {dict(target_counts)}")
            
            # 스캔 결과를 반영하여 진행률 명시
            write_progress(brand, step="상품 상세 수집 시작", current=0, total=limit,
                           phases_done=["카테고리 스캔"], phases_remaining=["상품 크롤링", "이미지 업로드", "임베딩 생성", "DB 저장"],
                           status="running", run_id=run_id, started_at=_started_at,
                           target_counts=target_counts)

            # 2단계: 상품 수집 및 상세 크롤링 실행
            logger.info("📦 [Step 2] 상품 상세 페이지 수집 및 크롤링 개시...")
            while all_target_products and len(collected_products) < limit:
                code, gender_key, category_key = all_target_products.pop(0)
                res = await process_product_link(code, gender_key, category_key, context)
                if res:
                    # 연관 컬러 등이 추가 검출되면 큐 앞단에 삽입
                    for new_code in res:
                        if new_code not in visited_products:
                            all_target_products.insert(0, (new_code, gender_key, category_key))
            
            await browser.close()

    if product_id:
        logger.info(f"🎯 [단일 상품 수집 모드] brand={brand}, product_id={product_id}")
        # 기존 성별/카테고리 복원 시도
        try:
            prod_conn = get_prod_db_connection()
            prod_cur = prod_conn.cursor()
            prod_cur.execute("SELECT gender, category_code FROM products WHERE product_id = %s", (product_id,))
            row = prod_cur.fetchone()
            if row:
                if not gender: gender = row[0]
                if not category: category = row[1]
            prod_cur.close()
            prod_conn.close()
        except Exception as e:
            logger.warning(f"기존 상품 정보 복원 실패: {e}")
            
        gender = gender or "Men"
        category = category or "Top"
        
        # 단일 상품 Playwright 수집 기동
        async with async_playwright() as p:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            browser = None
            try:
                browser = await p.chromium.launch(headless=True, channel="chrome", args=launch_args, timeout=5000)
            except Exception:
                try:
                    browser = await p.chromium.launch(headless=True, channel="msedge", args=launch_args, timeout=5000)
                except Exception:
                    browser = await p.chromium.launch(headless=True, args=launch_args, timeout=5000)
                    
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="ko-KR",
                timezone_id="Asia/Seoul"
            )
            # 자라/무신사 stealth 주입
            if brand.lower() in ["zara", "musinsa"]:
                stealth_js = """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
                """
                await context.add_init_script(stealth_js)
                
            await process_product_link(product_id, gender, category, context)
            await browser.close()
    else:
        await crawl_brand_categories()
    
    total_collected = len(collected_products)
    logger.info(f"📦 [{brand}] Playwright 크롤링 완료. 수집 데이터 개수: {total_collected} 건")
    write_progress(brand, step="상품 크롤링 완료", current=total_collected, total=limit,
                   phases_done=["카테고리 스캔", "상품 크롤링"],
                   phases_remaining=["이미지 업로드", "임베딩 생성", "DB 저장"],
                   current_item=f"총 {total_collected}개 수집 완료",
                   status="running", run_id=run_id, started_at=_started_at)
    
    if not collected_products:
        err_msg = f"❌ [{brand.upper()}] 크롤링된 데이터가 전혀 없습니다. WAF 차단 또는 목록 레이아웃 변경 여부를 점검해 주세요."
        logger.error(err_msg)
        raise ValueError(err_msg)

    # 4. Cloudinary Staging 업로드 및 DB Staging 적재
    logger.info(f"🔄 [{brand}] Cloudinary Staging 업로드 및 Neon DB 스테이징 적재 시작...")
    
    async with aiohttp.ClientSession() as session:
        success_db_count = 0
        new_items_count = 0
        updated_items_count = 0
        valid_embed_count = 0
        
        # PROD DB 연결 (캐시 조회용)
        prod_conn = get_prod_db_connection()
        prod_cur = prod_conn.cursor()
        
        # DW DB 연결 (스테이징 테이블 적재용)
        dw_conn = get_dw_db_connection()
        dw_conn.autocommit = False
        dw_cur = dw_conn.cursor()

        def ensure_connections():
            nonlocal prod_conn, prod_cur, dw_conn, dw_cur
            try:
                prod_cur.execute("SELECT 1")
                dw_cur.execute("SELECT 1")
            except Exception:
                logger.info("🔄 Neon DB 연결 끊김 감지: 재연결을 시도합니다.")
                for attempt in range(3):
                    try:
                        try:
                            prod_cur.close()
                            prod_conn.close()
                        except: pass
                        try:
                            dw_cur.close()
                            dw_conn.close()
                        except: pass
                        
                        prod_conn = get_prod_db_connection()
                        prod_cur = prod_conn.cursor()
                        dw_conn = get_dw_db_connection()
                        dw_conn.autocommit = False
                        dw_cur = dw_conn.cursor()
                        
                        logger.info("✅ Neon DB 재연결 성공!")
                        return
                    except Exception as conn_err:
                        logger.warning(f"⚠️ Neon DB 재연결 시도 {attempt+1}/3 실패: {conn_err}")
                        if attempt < 2:
                            import time
                            time.sleep(3)
                raise RuntimeError("Neon DB 재연결에 최종 실패하였습니다.")

        pending_errors = []  # 비동기 Neon DB 커넥션 폭주를 막기 위해 에러 로그를 일시 수집

        try:
            for item in collected_products:
                ensure_connections()
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
                        # integer out of range 방지 안전장치 (1,000만원 이상 비현실 가격 보정)
                        if base_price > 10000000 or base_price < 0:
                            base_price = 0
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
                    
                    # img.goodwearmall.com 도메인 상품 및 8세컨즈 브랜드 배너 이미지 필터링
                    _LOGO_KWDS = ["static.goodwearmall", "topten10_mall", "og_goodwearmall", "noimage", "logo", "icon", "banner", "display/category", "brandshop"]
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
                    query_term = re.sub(r'(TOPTEN10|topten10|8SECONDS|8seconds|탑텐10|탑텐|에잇세컨즈|UNIQLO|uniqlo|유니클로|굿웨어몰|goodwearmall|SPAO|spao|스파오|GIORDANO|giordano|지오다노|POLHAM|polham|폴햄)', '', query_term)
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
                            
                            # 2. HuggingFace CLIP 텍스트 임베딩 추출 (내부적으로 자동으로 768차원 패딩 완료)
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
                            def parse_vec(v):
                                if isinstance(v, str):
                                    cleaned = v.strip('[]{}')
                                    return [float(x) for x in cleaned.split(',') if x.strip()]
                                return list(v) if v is not None else None
                            
                            try:
                                p_img = parse_vec(img_vec)
                                p_txt = parse_vec(txt_vec)
                                if p_img and p_txt:
                                    embedding_cache[p_id] = (p_img, p_txt)
                            except Exception as parse_err:
                                logger.warning(f"⚠️ 임베딩 캐시 벡터 파싱 에러 (상품 {p_id}): {parse_err}")
                    prod_cur.close()
                    prod_conn.close()
                    logger.info(f"💾 Core DB로부터 기존 임베딩 캐시 {len(embedding_cache)}건을 로드했습니다. (연산 생략 예정)")
                except Exception as cache_err:
                    logger.warning(f"⚠️ 기존 임베딩 캐시 로드 중 예외 발생 (새로 생성함): {cache_err}")

                # 개별 상품 임베딩이 완료될 때마다 진행률을 기록하고 DB에 즉시 실시간 적재
                # (임베딩 시작 전 닫았던 DW DB 커넥션을 다시 열어 공유 준비)
                dw_conn = get_dw_db_connection()
                dw_conn.autocommit = False
                dw_cur = dw_conn.cursor()

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
                    
                    # 1. 캐시 히트된 데이터 먼저 실시간 DB 적재 (기존 dw_cur 연결 공유 활용)
                    for res in cached_results:
                        completed_count += 1
                        try:
                            dw_cur.execute("""
                                INSERT INTO staging_product_embeddings (
                                    product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                                ON CONFLICT (product_id) DO UPDATE
                                SET image_vector = EXCLUDED.image_vector,
                                    text_vector = EXCLUDED.text_vector,
                                    create_dt = CURRENT_TIMESTAMP
                            """, res)
                            valid_embed_count += 1
                        except Exception as save_err:
                            logger.error(f"❌ 임베딩 캐시 실시간 DB 적재 실패 (상품 {res[0]}): {save_err}")
                            
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
                            
                            # 1. DB 실시간 적재 (기존 dw_cur 연결 공유 활용)
                            if res:
                                try:
                                    dw_cur.execute("""
                                        INSERT INTO staging_product_embeddings (
                                            product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                                        )
                                        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                                        ON CONFLICT (product_id) DO UPDATE
                                        SET image_vector = EXCLUDED.image_vector,
                                            text_vector = EXCLUDED.text_vector,
                                            create_dt = CURRENT_TIMESTAMP
                                    """, res)
                                    valid_embed_count += 1
                                except Exception as save_err:
                                    logger.error(f"❌ 임베딩 연산 실시간 DB 적재 실패 (상품 {res[0]}): {save_err}")
                            
                            # 2. 진행 정보 파일 업데이트 (실시간)
                            current_item_str = f"임베딩 처리 중... ({completed_count}/{total_collected})"
                            if res and len(res) > 0 and res[0]:
                                current_item_str = f"상품 {res[0]} 임베딩 완료 및 DB 저장"
                                
                            write_progress(brand, step="임베딩 생성 중", current=completed_count, total=total_collected,
                                           phases_done=["카테고리 스캔", "상품 크롤링", "이미지 업로드"],
                                           phases_remaining=[f"임베딩 생성 ({total_collected}개)", "DB 저장"],
                                           current_item=current_item_str,
                                           status="running", run_id=run_id, started_at=_started_at,
                                           target_counts=target_counts)
                
                logger.info(f"✨ staging_product_embeddings 테이블에 실시간으로 {valid_embed_count}건 임베딩 벡터 적재 완료.")
            
            # 최종 DB 연결 수립 후 트랜잭션 정상 종료 처리 (이전 단계에서 다 끝났으므로 닫아줄 커넥션만 마무리)
            logger.info("🔌 최종 스테이징 적재 정리 완료. (Phase 1 완료)")
            dw_conn.commit()
            logger.info(f"✅ 스테이징 테이블 {success_db_count}건 임시 적재 완료. (Phase 1 완료)")
            write_progress(brand, step="완료", current=success_db_count, total=total_collected,
                           phases_done=["카테고리 스캔", "상품 크롤링", "이미지 업로드", "임베딩 생성", "DB 저장"],
                           phases_remaining=[],
                           current_item=f"전체 {success_db_count}건 완료",
                           status="done", run_id=run_id, started_at=_started_at,
                           target_counts=target_counts)
            
            # 수집된 pending_errors 일괄 DB 적재 처리 (Neon DB 동시 커넥션 수 초과 방지)
            if pending_errors and not dry_run:
                logger.info(f"📝 펜딩된 에러 로그 {len(pending_errors)}건을 단일 세션으로 일괄 적재 진행 중...")
                try:
                    non_warn_count = 0
                    for err_type, err_msg, p_id, src_url in pending_errors:
                        is_warn = err_type.endswith("_WARN")
                        if not is_warn:
                            non_warn_count += 1
                        
                        dw_cur.execute("""
                            INSERT INTO pipeline_errors (
                                run_id, error_type, error_message, stack_trace, product_id, source_url, created_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        """, (run_id, err_type, err_msg, "", p_id or "", src_url or ""))
                    
                    if run_id and non_warn_count > 0:
                        dw_cur.execute("""
                            UPDATE pipeline_runs 
                            SET error_count = error_count + %s 
                            WHERE run_id = %s
                        """, (non_warn_count, run_id))
                    
                    dw_conn.commit()
                except Exception as log_err:
                    logger.error(f"❌ 펜딩 에러 일괄 적재 중 예외: {log_err}")

            return {"total_items": success_db_count, "new_items": new_items_count, "updated_items": updated_items_count, "embed_count": valid_embed_count, "target_counts": target_counts}
            
        except Exception as db_err:
            write_progress(brand, step="오류 발생", current=0, total=0,
                           phases_done=[], phases_remaining=[],
                           status="error", error=str(db_err)[:200],
                           run_id=run_id, started_at=_started_at,
                           target_counts=target_counts)
            dw_conn.rollback()
            raise RuntimeError(f"스테이징 적재 중 DB 에러: {db_err}")
        finally:
            prod_cur.close()
            prod_conn.close()
            dw_cur.close()
            dw_conn.close()


def main():
    parser = argparse.ArgumentParser(description="Lookalike 안심 모니터링 크롤러 CLI")
    parser.add_argument("--brand", required=True, choices=["8seconds", "topten", "uniqlo", "polham", "spao", "giordano"], help="크롤링할 타겟 패션 브랜드")
    parser.add_argument("--limit", type=int, default=50, help="수집할 최대 상품 개수")
    parser.add_argument("--dry-run", action="store_true", help="실제 DB 반영을 생략하고 디버깅 출력만 활성화")
    parser.add_argument("--action", default="crawl", choices=["crawl", "swap"], help="실행할 파이프라인 액션 (crawl: 수집/임시적재, swap: 지연 이관)")
    parser.add_argument("--force", action="store_true", help="swap 액션 시 24시간 대기 조건 무시하고 즉시 강제 스위칭")
    parser.add_argument("--gender", choices=["Men", "Women"], help="수집할 타겟 성별 (분할 크롤링용)")
    parser.add_argument("--category", choices=["Outer", "Top", "Bottom"], help="수집할 카테고리 (분할 크롤링용)")
    parser.add_argument("--force-download", action="store_true", help="캐시를 강제 스킵하고 이미지를 새로 다운로드하며 네이버 최저가 API 구동")
    parser.add_argument("--product-id", help="단일 재수집을 실행할 상품 ID")
    
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
            force_download=args.force_download,
            product_id=args.product_id
        ))
        logger.info(f"🎉 [{args.brand}] 배치 파이프라인이 성공적으로 완수되었습니다.")
        if run_id:
            total = metrics.get("total_items", 0) if metrics else 0
            new_val = metrics.get("new_items", 0) if metrics else 0
            upd_val = metrics.get("updated_items", 0) if metrics else 0
            embed_val = metrics.get("embed_count", 0) if metrics else 0
            t_counts = metrics.get("target_counts", {}) if metrics else {}
            metadata = {
                "target_counts": t_counts,
                "target_total": sum(t_counts.values()) if t_counts else 0
            }
            log_pipeline_end(run_id, "SUCCESS", total_items=total, new_items=new_val, updated_items=upd_val, embed_count=embed_val, metadata_dict=metadata)
        # 백그라운드 스레드 고정으로 인한 지연 없는 강제 프로세스 정상 종료
        sys.exit(0)
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