"""
자라(Zara) aiohttp 기반 순수 API 스크래퍼
전략: itxrest/4 엔드포인트를 통한 카테고리-상품 데이터 수집
- itxrest/4/catalog/store/11717/category/{categoryId}?languageId=-9 → 카테고리 메타데이터 및 서브카테고리 확인됨
- Playwright page.on('response') 인터셉트로 실제 product grid API URL을 캡처
"""

import asyncio
import re
import json
import os
import aiohttp
from datetime import datetime

# --- 설정 ---
BRAND_NAME = "zara"
STORE_ID = 11717
LANGUAGE_ID = -9

# 수집 대상 카테고리 (categoryId, sectionName, gender, category)
# URL 예: /kr/ko/woman-trousers-l1335.html?v1=2420795
# l{숫자} = sectionId, v1={숫자} = categoryId
TARGET_MAP = {
    "Men": {
        "Outer": [
            {"url": "https://www.zara.com/kr/ko/man-outerwear-l715.html?v1=2606109", "categoryId": 2606109},
            {"url": "https://www.zara.com/kr/ko/man-jackets-l640.html?v1=2536906", "categoryId": 2536906},
        ],
        "Top": [
            {"url": "https://www.zara.com/kr/ko/man-tshirts-l855.html?v1=2432042", "categoryId": 2432042},
            {"url": "https://www.zara.com/kr/ko/man-sweatshirts-l821.html?v1=2432232", "categoryId": 2432232},
        ],
        "Bottom": [
            {"url": "https://www.zara.com/kr/ko/man-trousers-l838.html?v1=2432096", "categoryId": 2432096},
            {"url": "https://www.zara.com/kr/ko/man-jeans-l659.html?v1=2432131", "categoryId": 2432131},
        ],
    },
    "Women": {
        "Outer": [
            {"url": "https://www.zara.com/kr/ko/woman-jackets-l1114.html?v1=2417772", "categoryId": 2417772},
            {"url": "https://www.zara.com/kr/ko/woman-outerwear-l1184.html?v1=2419032", "categoryId": 2419032},
        ],
        "Top": [
            {"url": "https://www.zara.com/kr/ko/woman-shirts-l1217.html?v1=2420369", "categoryId": 2420369},
            {"url": "https://www.zara.com/kr/ko/woman-tshirts-l1362.html?v1=2420417", "categoryId": 2420417},
        ],
        "Bottom": [
            {"url": "https://www.zara.com/kr/ko/woman-trousers-l1335.html?v1=2420795", "categoryId": 2420795},
        ],
    },
}

# 기본 HTTP 헤더 (브라우저처럼 위장)
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.zara.com/kr/ko/",
    "Origin": "https://www.zara.com",
    "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


async def fetch_category_meta(session: aiohttp.ClientSession, category_id: int) -> dict:
    """itxrest/4를 통해 카테고리 메타데이터 수집 (200 OK 확인된 엔드포인트)"""
    url = f"https://www.zara.com/itxrest/4/catalog/store/{STORE_ID}/category/{category_id}?languageId={LANGUAGE_ID}"
    try:
        async with session.get(url, headers=BASE_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                return data
            else:
                print(f"  ⚠️ 카테고리 메타 {category_id} → HTTP {resp.status}")
                return {}
    except Exception as e:
        print(f"  ❌ 카테고리 메타 요청 실패 {category_id}: {e}")
        return {}


async def fetch_product_detail(session: aiohttp.ClientSession, product_id: str) -> dict:
    """개별 상품 상세 정보 수집 (itxrest API 기반)"""
    # 자라 상품 상세 API 탐색 중 - 현재는 HTML 파싱 방식 사용
    # 나중에 올바른 product API가 발견되면 교체 예정
    return {}


async def intercept_with_playwright(target_url, category_id: int) -> list:
    """
    Playwright를 사용해 자라 카테고리 페이지 로드 시 발생하는
    내부 API 요청을 실시간으로 인터셉트하여 상품 목록을 수집
    
    이 방식은:
    1. WAF(Akamai) 챌린지를 브라우저 렌더링으로 통과
    2. 내부적으로 발생하는 itxrest API 응답을 실시간 캡처
    3. 브라우저 없이는 얻을 수 없는 세션 쿠키/토큰 없이도 동작
    """
    if isinstance(target_url, dict):
        target_url = target_url.get("url", "")
        
    from playwright.async_api import async_playwright
    
    captured_products = []
    captured_api_urls = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        
        # 봇 탐지 우회용 stealth 스크립트
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
        """)
        
        page = await context.new_page()
        
        # *** 핵심: 응답 인터셉터 설정 ***
        # 자라 페이지 로드 중 발생하는 API 호출을 실시간으로 캡처
        async def handle_response(response):
            url = response.url
            # itxrest API나 product 관련 API 응답만 캡처
            if ("itxrest" in url or "zara.com" in url) and response.status == 200:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type:
                    try:
                        body = await response.json()
                        
                        # product 목록이 포함된 응답 찾기
                        products_found = []
                        
                        # 응답 구조 분석 - 여러 형태의 product 목록 패턴 탐색
                        if isinstance(body, dict):
                            # 'products' 키로 직접 상품 목록
                            if "products" in body and isinstance(body["products"], list):
                                products_found = body["products"]
                                print(f"  📦 상품 목록 API 발견! URL: {url[:100]}")
                                print(f"     상품 수: {len(products_found)}개")
                            
                            # 'productGroups' 키
                            elif "productGroups" in body:
                                for group in body.get("productGroups", []):
                                    for elem in group.get("elements", []):
                                        if "commercialComponents" in elem:
                                            for comp in elem["commercialComponents"]:
                                                if comp.get("type") == "Product":
                                                    products_found.append(comp)
                                if products_found:
                                    print(f"  📦 productGroups API 발견! URL: {url[:100]}")
                                    print(f"     상품 수: {len(products_found)}개")
                            
                            # 그 외 구조 로깅 (새 패턴 발견용)
                            elif len(str(body)) > 1000 and "itxrest" in url:
                                print(f"  🔍 API 응답 발견: {url[:80]}")
                                print(f"     키 구조: {list(body.keys())[:8]}")
                                captured_api_urls.append({"url": url, "keys": list(body.keys())})
                        
                        # 발견된 상품 데이터 파싱
                        for prod in products_found:
                            parsed = parse_product_from_api(prod)
                            if parsed:
                                captured_products.append(parsed)
                    
                    except Exception as e:
                        pass  # JSON 파싱 실패 무시
        
        page.on("response", handle_response)
        
        # 불필요한 리소스 차단 (속도 향상)
        await page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,css}", lambda r: r.abort())
        await page.route("**/*analytics*", lambda r: r.abort())
        await page.route("**/*tracking*", lambda r: r.abort())
        await page.route("**/*ads*", lambda r: r.abort())
        
        print(f"  🌐 자라 카테고리 페이지 로드 중: {target_url[:60]}")
        
        try:
            await page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3)  # API 응답 대기
            
            # 스크롤 다운하여 더 많은 상품 로드 유도
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5)
            
            # DOM에서 직접 상품 링크 수집 (API 인터셉트 실패 시 폴백)
            if not captured_products:
                print(f"  ⚠️ API 인터셉트 실패. DOM 파싱으로 폴백...")
                links = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href*="-p"][href*=".html"]'))
                              .map(a => a.href)
                """)
                print(f"  🔗 DOM에서 상품 링크 {len(links)}개 발견")
                
                for link in set(links):
                    match = re.search(r'-p([0-9]+)\.html', link)
                    if match:
                        pid = match.group(1)
                        captured_products.append({
                            "product_id": pid,
                            "url": link.split("?")[0],
                            "from_dom": True,
                        })
        
        except Exception as e:
            print(f"  ❌ 페이지 로드 실패: {e}")
        
        finally:
            if captured_api_urls:
                print(f"  📋 발견된 새 API 엔드포인트 ({len(captured_api_urls)}개):")
                for item in captured_api_urls[:5]:
                    print(f"     {item['url'][:80]} → {item['keys']}")
            
            await browser.close()
    
    return captured_products


def parse_product_from_api(prod: dict) -> dict:
    """API 응답에서 상품 정보 파싱"""
    try:
        product_id = str(prod.get("id", "") or prod.get("productId", ""))
        name = prod.get("name", "") or prod.get("displayName", "")
        
        if not product_id or not name:
            return None
        
        # 가격 추출
        price = 0
        price_info = prod.get("price", {})
        if isinstance(price_info, dict):
            price = price_info.get("value", 0) or price_info.get("current", {}).get("value", 0)
        elif isinstance(price_info, (int, float)):
            price = int(price_info)
        
        # 이미지 추출
        images = []
        for media_group in prod.get("detail", {}).get("colors", []):
            for media in media_group.get("xmedia", []):
                path = media.get("path", "")
                name_img = media.get("name", "")
                if path and name_img:
                    img_url = f"https://static.zara.net/assets/public/4b79/f8af/{name_img}{path}?format=auto"
                    images.append(img_url)
        
        # 단순 xmedia 구조
        if not images:
            for media in prod.get("xmedia", []):
                path = media.get("path", "")
                img_name = media.get("name", "")
                if path:
                    img_url = f"https://static.zara.net{path}"
                    images.append(img_url)
        
        # 색상 정보
        colors = [c.get("name", "") for c in prod.get("detail", {}).get("colors", []) if c.get("name")]
        
        # 사이즈 정보
        size_stock = []
        for color_data in prod.get("detail", {}).get("colors", []):
            for size in color_data.get("sizes", []):
                size_stock.append({
                    "size": size.get("name", ""),
                    "is_sold_out": not size.get("availability", "in_stock").startswith("in"),
                    "stock_qty": 999 if size.get("availability", "").startswith("in") else 0,
                })
            break  # 첫 번째 색상 기준
        
        # 품번 (색상코드)
        goods_no = prod.get("displayReference", product_id)
        
        return {
            "goodsNo": goods_no,
            "goodsNm": name,
            "brandName": "ZARA",
            "price": price,
            "thumbnailImageUrl": images[0] if images else "",
            "goodsImages": images,
            "color_name": colors[0] if colors else "",
            "colors": colors,
            "size_stock": size_stock,
            "is_sold_out": all(s["is_sold_out"] for s in size_stock) if size_stock else False,
            "goodsMaterial": {},
            "url": f"https://www.zara.com/kr/ko/-p{product_id}.html",
            "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "product_id": product_id,
        }
    except Exception:
        return None


async def get_product_list_via_playwright(gender: str, category: str, cat_info: dict) -> list:
    """Playwright 인터셉트를 통한 카테고리별 상품 목록 수집"""
    target_url = cat_info["url"]
    category_id = cat_info["categoryId"]
    
    print(f"\n  🎯 [{gender}/{category}] 카테고리 {category_id} 수집 시작")
    
    # Playwright로 상품 목록 수집
    products = await intercept_with_playwright(target_url, category_id)
    
    print(f"  ✅ [{gender}/{category}] 총 {len(products)}개 상품 수집")
    return products


async def get_product_detail_from_api(session: aiohttp.ClientSession, product_id: str, product_url: str) -> dict:
    """
    aiohttp로 상품 상세 정보 수집
    Playwright 인터셉트로 발견된 product API URL을 활용하거나
    알려진 API 패턴으로 상세 정보 수집
    """
    # 아직 정확한 상품 상세 API가 발견되지 않아 Playwright 결과를 그대로 사용
    return {}


# crawling_pipeline_cli.py에서 호출하는 공개 인터페이스
# 이 스크래퍼는 crawling_pipeline_cli.py의 brand="zara" 처리에서 활용

# TARGET_MAP은 crawling_pipeline_cli.py가 참조하는 표준 형식
# 기존 Playwright 기반 인터페이스와의 호환성을 유지하되
# API 인터셉트를 통해 더 안정적으로 데이터 수집

async def extract_product_data_from_dom(page) -> dict:
    """
    DOM에서 상품 데이터 추출 (Playwright page 객체 사용)
    crawling_pipeline_cli.py의 process_product_link에서 호출하는 인터페이스
    """
    try:
        # 스크롤로 레이아웃 로딩 유도
        await page.mouse.wheel(0, 500)
        await asyncio.sleep(1.0)

        # 소재 정보 패널 클릭 시도
        try:
            await page.evaluate("""
                () => {
                    const btns = document.querySelectorAll('button');
                    for (const btn of btns) {
                        if (btn.innerText.includes('소재') || btn.innerText.includes('혼용률')) {
                            btn.click();
                        }
                    }
                }
            """)
            await asyncio.sleep(1.5)
        except Exception:
            pass

        data = await page.evaluate("""
            () => {
                const result = {};
                const urlMatch = location.href.match(/-p([0-9]+)\\.html/);
                
                // 상품 ID 추출 (URL 패턴에서)
                result.goodsNo = urlMatch ? urlMatch[1] : location.href.split('?')[0].split('-').pop();
                
                // 상품명
                result.goodsNm = document.querySelector('h1')?.innerText.trim() || document.title;
                result.brandName = "ZARA";
                
                // OG 이미지 (썸네일)
                result.thumbnailImageUrl = document.querySelector('meta[property="og:image"]')?.content || "";
                
                // 색상
                const colorEl = document.querySelector('.product-detail-info__color');
                result.color_name = colorEl ? colorEl.innerText.split('|')[0].trim() : "";
                result.colors = [];
                
                // 가격
                const priceEl = document.querySelector('.price__amount, .money-amount');
                result.price = priceEl ? parseInt(priceEl.innerText.replace(/[^0-9]/g, '')) : 0;
                
                // 사이즈/재고 정보
                const sizeStockInfo = [];
                document.querySelectorAll('li.product-detail-size-selector__size-list-item').forEach(li => {
                    const nameEl = li.querySelector('[data-qa-qualifier="product-detail-size-selector-size-list-item-name"]');
                    const name = nameEl ? nameEl.innerText.trim() : li.innerText.split('\\n')[0].trim();
                    if (name) {
                        const isSoldOut = li.getAttribute('aria-disabled') === 'true' 
                            || li.classList.contains('disabled') 
                            || li.innerText.includes('품절');
                        sizeStockInfo.push({ size: name, is_sold_out: isSoldOut, stock_qty: isSoldOut ? 0 : 999 });
                    }
                });
                result.size_stock = sizeStockInfo;
                
                // 품절 여부
                result.is_sold_out = sizeStockInfo.length > 0 ? sizeStockInfo.every(s => s.is_sold_out) : false;
                
                // 이미지 목록
                const images = [];
                document.querySelectorAll('img.media-image__image, .media-wrap__image').forEach(img => {
                    let src = img.src;
                    if (src && src.includes('static.zara.net')) images.push(src.split('?')[0]);
                });
                result.goodsImages = [...new Set(images)];
                
                // 소재 정보
                const specInfo = {};
                const descEl = document.querySelector('.product-detail-description div.expandable-text__inner-content');
                if (descEl) specInfo['description'] = descEl.innerText.replace(/\\n+/g, ' ').trim();
                
                const compContainer = document.querySelector('.product-detail-composition');
                if (compContainer) {
                    compContainer.querySelectorAll('.product-detail-composition__item').forEach(item => {
                        const partName = item.querySelector('.product-detail-composition__part-name')?.innerText.trim() || "소재";
                        const ingredients = [];
                        item.querySelectorAll('li').forEach(li => ingredients.push(li.innerText.trim()));
                        if (ingredients.length > 0) specInfo[partName] = ingredients.join(', ');
                    });
                }
                result.goodsMaterial = specInfo;
                return result;
            }
        """)
        
        if not data or not data.get("goodsNm"):
            return None
        
        data["url"] = page.url
        data["scraped_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return data

    except Exception:
        return None
