import os
import sys
import asyncio
import re
import json
from datetime import datetime
from playwright.async_api import async_playwright

# --- [설정] ---
# 한국어 주석: 폴햄 브랜드의 고유 정보로 설정합니다.
BRAND_NAME = "polham"

if len(sys.argv) > 1:
    TODAY_STR = sys.argv[1]
else:
    TODAY_STR = datetime.now().strftime('%Y%m%d')

LOCAL_SAVE_DIR = f"data/{BRAND_NAME}/{TODAY_STR}"

# 크롤링 대상 카테고리 URL 맵 (폴햄 브랜드관 실제 카테고리 코드 사용)
TARGET_MAP = {
    "Men": {
        "Outer": [
            "https://www.goodwearmall.com/polhamshop/category/SSMA02A05",
            "https://www.goodwearmall.com/polhamshop/category/SSMA02A06"
        ],
        "Top": [
            "https://www.goodwearmall.com/polhamshop/category/SSMA02A01",
            "https://www.goodwearmall.com/polhamshop/category/SSMA02A02",
            "https://www.goodwearmall.com/polhamshop/category/SSMA02A04"
        ],
        "Bottom": [
            "https://www.goodwearmall.com/polhamshop/category/SSMA02A07",
            "https://www.goodwearmall.com/polhamshop/category/SSMA02A13"
        ]
    },
    "Women": {
        "Outer": [
            "https://www.goodwearmall.com/polhamshop/category/SSMA01A04"
        ],
        "Top": [
            "https://www.goodwearmall.com/polhamshop/category/SSMA01A01",
            "https://www.goodwearmall.com/polhamshop/category/SSMA01A02",
            "https://www.goodwearmall.com/polhamshop/category/SSMA01A03"
        ],
        "Bottom": [
            "https://www.goodwearmall.com/polhamshop/category/SSMA01A06",
            "https://www.goodwearmall.com/polhamshop/category/SSMA01A13",
            "https://www.goodwearmall.com/polhamshop/category/SSMA01A05"
        ]
    }
}

visited_products = set()
sem = asyncio.Semaphore(5)


async def extract_product_data_from_dom(page):
    """
    폴햄 상품 상세 페이지 DOM에서 상품 데이터를 추출합니다.
    굿웨어몰 표준 구조를 사용하여 데이터 파싱을 수행합니다.
    """
    try:
        # 로딩 안정성을 위해 주요 엘리먼트 로드 대기
        try:
            await page.wait_for_selector('.price, .goods-name, script[type="application/ld+json"]', timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(2)  # 리다이렉트 후 JS 렌더링 완료 대기
        data = await page.evaluate("""() => {
            const ogTitle = document.querySelector('meta[property="og:title"]')?.content || document.title;
            // 만약 og:title에 굿웨어몰 메인 슬로건이 들어있다면 아직 상품 페이지가 덜 로드된 것이므로 None 처리 유도
            if (ogTitle.includes("좋은 옷을 모두에게")) {
                return null;
            }

            const result = {};
            result.goodsNo = location.href.match(/\/product\/([A-Z0-9]+)\/detail/)?.[1] || "";

            // 상품명: og:title에서 브랜드/쇼핑몰명 제거 ("POLHAM 여성 20수..." → "여성 20수...")
            // " | 굿 셀렉션..." 이후 제거, 앞에 브랜드명 있으면 제거
            let goodsNm = ogTitle.split('|')[0].trim();
            // "POLHAM " 또는 "폴햄 " 접두어 제거
            goodsNm = goodsNm.replace(/^(POLHAM|폴햄)\s*/i, '').trim();
            result.goodsNm = goodsNm || document.title;
            result.brandName = "POLHAM";

            // 이미지 유효성 검증
            const logoKeywords = [
                'og_goodwearmall', 'og_toptenclub', 'NoImage', 'logo',
                'topten10_mall', 'static.goodwearmall', 'banner', 'header',
                'layout', 'common', 'brand', 'btn', 'icon'
            ];
            const isValidImg = (url) => {
                if (!url) return false;
                const lower = url.toLowerCase();
                return !logoKeywords.some(kwd => lower.includes(kwd));
            };

            let thumb = "";

            // 1. ld+json 구조에서 이미지 추출 (가장 신뢰도 높음)
            try {
                const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (const script of ldJsonScripts) {
                    let jsonData;
                    try {
                        jsonData = JSON.parse(script.innerText || script.textContent);
                    } catch (e) { continue; }
                    if (jsonData && jsonData['@type'] === 'Product' && jsonData.image) {
                        const imgUrl = Array.isArray(jsonData.image) ? jsonData.image[0] : jsonData.image;
                        if (isValidImg(imgUrl)) {
                            thumb = imgUrl;
                            break;
                        }
                    }
                }
            } catch (e) {}

            // 2. carousel-img 클래스 이미지 (실제 상품 이미지 셀렉터)
            if (!thumb) {
                const carouselImg = document.querySelector('.carousel-img');
                if (carouselImg) {
                    const src = carouselImg.getAttribute('data-src') || carouselImg.getAttribute('src') || "";
                    if (isValidImg(src) && src.includes('img.goodwearmall.com/goods/')) {
                        thumb = src;
                    }
                }
            }

            // 3. DOM 엘리먼트에서 lazy loading 이미지 우선 탐색
            if (!thumb) {
                const mainImg = document.querySelector('.goods-img img, .img-zoom img, .prd-detail-img img, #goodsImg img');
                if (mainImg) {
                    const src = mainImg.getAttribute('data-src') || mainImg.getAttribute('src') || "";
                    if (isValidImg(src)) {
                        thumb = src;
                    }
                }
            }

            // 4. og:image 메타 태그 차선책
            if (!thumb) {
                const og = document.querySelector('meta[property="og:image"]')?.content || "";
                if (isValidImg(og)) {
                    thumb = og;
                }
            }

            // 5. 최후 수단: img.goodwearmall.com/goods/ 경로 이미지 직접 탐색
            if (!thumb) {
                const allImgs = document.querySelectorAll('img[src], img[data-src]');
                for (const img of allImgs) {
                    const src = img.getAttribute('data-src') || img.getAttribute('src') || "";
                    if (src.includes('img.goodwearmall.com/goods/') && isValidImg(src)) {
                        thumb = src;
                        break;
                    }
                }
            }

            if (thumb && thumb.startsWith('//')) thumb = 'https:' + thumb;
            result.thumbnailImageUrl = thumb || "";

            // 가격 추출
            let price = 0;
            try {
                const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (const script of ldJsonScripts) {
                    let jsonData;
                    try {
                        jsonData = JSON.parse(script.innerText || script.textContent);
                    } catch (e) { continue; }
                    if (jsonData && jsonData['@type'] === 'Product' && jsonData.offers && jsonData.offers.price) {
                        price = parseInt(jsonData.offers.price);
                        break;
                    }
                }
            } catch (e) {}

            if (price === 0) {
                const metaPrice = document.querySelector('meta[property="product:price:amount"]')?.content;
                if (metaPrice) price = parseInt(metaPrice);
            }
            if (price === 0) {
                const priceElement = document.querySelector('.price strong, .item-price, .sale-price');
                if (priceElement) price = parseInt(priceElement.innerText.replace(/[^0-9]/g, ''));
            }
            result.price = price;

            // 품절 여부
            let isSoldOut = false;
            const buyBtn = document.querySelector('.btn-buy, .btn-order, .btn-cart');
            if (buyBtn && (buyBtn.innerText.includes('품절') || buyBtn.disabled)) isSoldOut = true;
            if (price === 0) isSoldOut = true;
            result.is_sold_out = isSoldOut;

            // 사이즈/재고 정보
            const sizeStockInfo = [];
            document.querySelectorAll('.option-list.size button, .size-area button').forEach(btn => {
                const name = btn.innerText.trim();
                if (name && !name.includes('삭제')) {
                    const isItemSoldOut = btn.classList.contains('soldout') || btn.disabled || btn.innerText.includes('품절');
                    sizeStockInfo.push({
                        size: name.replace(/\(.*\)/, '').trim(),
                        is_sold_out: isItemSoldOut,
                        stock_qty: isItemSoldOut ? 0 : 999
                    });
                }
            });
            result.size_stock = sizeStockInfo;

            // 다른 색상 상품 ID
            const otherColorIds = [];
            document.querySelectorAll('.tooltip-box button, .color-chip button, .option-list.color button').forEach(btn => {
                const onclick = btn.getAttribute('onclick') || "";
                const match = onclick.match(/goGodDetail\(['"]([A-Z0-9]+)['"]/);
                if (match && match[1] && match[1] !== result.goodsNo) otherColorIds.push(match[1]);
            });
            result.other_color_ids = [...new Set(otherColorIds)];

            // 추가 이미지 목록: carousel-img(실제 상품 이미지) 우선 수집
            const images = [];
            // carousel-img 클래스에서 img.goodwearmall.com/goods/ 경로 이미지 우선 수집
            document.querySelectorAll('.carousel-img').forEach(img => {
                const src = img.getAttribute('data-src') || img.getAttribute('src');
                if (src && src.includes('img.goodwearmall.com/goods/') && isValidImg(src)) {
                    const cleanSrc = src.split('?')[0];  // 쿼리스트링 제거
                    images.push(cleanSrc.startsWith('//') ? 'https:' + cleanSrc : cleanSrc);
                }
            });
            // carousel-img에서 못 찾으면 전체 img 탐색
            if (images.length === 0) {
                document.querySelectorAll('img').forEach(img => {
                    const src = img.getAttribute('src') || img.getAttribute('data-src');
                    if (src && src.includes('img.goodwearmall.com/goods/') && isValidImg(src)) {
                        images.push(src.startsWith('//') ? 'https:' + src : src);
                    }
                });
            }
            result.goodsImages = [...new Set(images)];

            // 소재/스펙 정보
            const specInfo = {};
            document.querySelectorAll('table tbody tr').forEach(row => {
                const key = row.querySelector('th')?.innerText.trim();
                const val = row.querySelector('td')?.innerText.trim().replace(/\\n/g, ' ');
                if (key && val) specInfo[key] = val;
            });
            result.goodsMaterial = specInfo;

            return result;
        }""")

        if not data or not data.get('goodsNm') or data.get('goodsNm').strip() == "" or data.get('price', 0) == 0:
            return None
        data['url'] = page.url
        data['scraped_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return data
    except Exception as e:
        print(f"   ⚠️ DOM 추출 오류: {str(e)[:80]}")
        return None


async def process_product(product_id, gender, category, context):
    """상품 ID별 수집 처리 함수"""
    if product_id in visited_products:
        return
    visited_products.add(product_id)

    filename = f"{BRAND_NAME}_{gender.lower()}_{category.lower()}_{product_id}.json"
    filepath = os.path.join(LOCAL_SAVE_DIR, filename)

    if os.path.exists(filepath):
        print(f"   ⏩ {product_id} 이미 수집됨 (스킵)")
        return

    new_ids = []

    async with sem:
        # 리다이렉트를 방지하기 위해 www.goodwearmall.com 도메인 직접 사용
        url = f"https://www.goodwearmall.com/product/{product_id}/detail"
        max_retries = 3

        for attempt in range(max_retries):
            p_page = await context.new_page()
            try:
                await p_page.goto(url, timeout=60000, wait_until="domcontentloaded")
                product_dict = await extract_product_data_from_dom(p_page)

                if product_dict:
                    os.makedirs(LOCAL_SAVE_DIR, exist_ok=True)
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(product_dict, f, ensure_ascii=False, indent=4)
                    break
            except Exception as e:
                await asyncio.sleep(2)
            finally:
                if not p_page.is_closed():
                    await p_page.close()


async def crawl_category(gender, category_name, base_url, context):
    """카테고리 목록 페이지 순회 및 수집 함수"""
    page = await context.new_page()
    all_product_ids = set()

    try:
        await page.goto(base_url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        for page_num in range(1, 51):
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                await asyncio.sleep(0.5)

            pids_on_page = await page.evaluate("""() => {
                const ids = new Set();
                document.querySelectorAll('a[href*="/product/"]').forEach(a => {
                    const m = a.href.match(/\/product\/([A-Z0-9]{8,20})\/detail/);
                    if (m) ids.add(m[1]);
                });
                document.querySelectorAll('[data-goods-no], [data-goodsno]').forEach(el => {
                    const id = el.getAttribute('data-goods-no') || el.getAttribute('data-goodsno');
                    if (id && id.length >= 8 && id.length <= 20) ids.add(id.toUpperCase());
                });
                return Array.from(ids);
            }""")

            all_product_ids.update(pids_on_page)

            if not pids_on_page and page_num > 1:
                break

            try:
                next_btn = await page.query_selector(f".pagination a:has-text('{page_num + 1}')")
                if not next_btn:
                    next_btn = await page.query_selector(".pagination .next, .pagination .btn-next")
                if next_btn:
                    await next_btn.click(timeout=2000)
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await asyncio.sleep(1)
                else:
                    break
            except Exception:
                break

        await page.close()

        if all_product_ids:
            tasks = [process_product(pid, gender, category_name, context) for pid in list(all_product_ids)]
            await asyncio.gather(*tasks)
    except Exception as e:
        if not page.is_closed():
            await page.close()


async def run():
    print(f"--- [START] {BRAND_NAME} 크롤링 시작 ---")
    os.makedirs(LOCAL_SAVE_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 1024},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )

        for gender, categories in TARGET_MAP.items():
            for category, urls in categories.items():
                for url in urls:
                    await crawl_category(gender, category, url, context)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
