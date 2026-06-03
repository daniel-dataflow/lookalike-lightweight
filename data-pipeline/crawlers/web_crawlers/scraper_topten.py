import os
import sys
import asyncio
import re
import json
from datetime import datetime
from playwright.async_api import async_playwright

# --- [설정] ---
BRAND_NAME = "topten"

if len(sys.argv) > 1:
    TODAY_STR = sys.argv[1]
else:
    TODAY_STR = datetime.now().strftime('%Y%m%d')

LOCAL_SAVE_DIR = f"data/{BRAND_NAME}/{TODAY_STR}"

HDFS_NAMENODE_URL = "http://namenode-main:9870"
HDFS_USER = "root"
HDFS_ROOT_PATH = f"/raw/{BRAND_NAME}/{TODAY_STR}"

# 크롤링 대상 카테고리 URL 맵
TARGET_MAP = {
    "Men": {
        "Outer": [
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA42A06",
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA42A03"
        ],
        "Top": [
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA42A02",
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA42A01",
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA42A04"
        ],
        "Bottom": [
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA42A07",
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA42A21"
        ]
    },
    "Women": {
        "Outer": [
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA41A04A01",
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA41A02"
        ],
        "Top": [
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA41A01",
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA41A03"
        ],
        "Bottom": [
            "https://topten10.goodwearmall.com/display/category/list?dspCtgryNo=SSMA41A06"
        ]
    }
}

visited_products = set()
sem = asyncio.Semaphore(5)


async def extract_product_data_from_dom(page):
    """
    탑텐 상품 상세 페이지 DOM에서 상품 데이터를 추출합니다.
    thumb 변수 미선언 버그 및 ld+json 파싱 오류 수정 완료.
    """
    try:
        await asyncio.sleep(1)
        data = await page.evaluate("""() => {
            const result = {};
            result.goodsNo = location.href.match(/\/product\/([A-Z0-9]+)\/detail/)?.[1] || "";
            result.goodsNm = document.querySelector('meta[property="og:title"]')?.content || document.title;
            result.brandName = "TOPTEN10";

            // 이미지 유효성 검사: 로고/공통 이미지 키워드 포함 시 제외
            // 주의: 'goodwearmall' 전체를 배제하면 img.goodwearmall.com 상품 이미지도 제외됨!
            // static.goodwearmall.com(CSS/배너)만 제외하고 img.goodwearmall.com은 허용
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

            // thumb 변수를 반드시 let으로 선언 (미선언 시 ReferenceError 발생)
            let thumb = "";

            // 1. ld+json 스키마에서 Product 이미지 추출 (가장 신뢰도 높음)
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

            // 2. DOM 엘리먼트에서 lazy loading 속성 우선 탐색
            if (!thumb) {
                const mainImg = document.querySelector('.goods-img img, .img-zoom img, .prd-detail-img img, #goodsImg img');
                if (mainImg) {
                    const src = mainImg.getAttribute('data-src') || mainImg.getAttribute('src') || "";
                    if (isValidImg(src)) {
                        thumb = src;
                    }
                }
            }

            // 3. og:image 메타 태그 차선책
            if (!thumb) {
                const og = document.querySelector('meta[property="og:image"]')?.content || "";
                if (isValidImg(og)) {
                    thumb = og;
                }
            }

            // 4. 최후 수단: goodwearmall 도메인 이미지 직접 탐색
            if (!thumb) {
                const allImgs = document.querySelectorAll('img[src], img[data-src]');
                for (const img of allImgs) {
                    const src = img.getAttribute('data-src') || img.getAttribute('src') || "";
                    if (src.includes('goodwearmall') && isValidImg(src)) {
                        thumb = src;
                        break;
                    }
                }
            }

            // 프로토콜 없는 URL 보정
            if (thumb && thumb.startsWith('//')) thumb = 'https:' + thumb;
            result.thumbnailImageUrl = thumb || "";

            // 가격 추출: ld+json > meta 태그 > DOM 요소 순서
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

            // 추가 이미지 목록: isValidImg 필터를 통해 로고/배너/정적 이미지 제외
            // img.goodwearmall.com(상품 이미지)만 포함, static.goodwearmall.com(로고/CSS) 제외
            const images = [];
            document.querySelectorAll('img').forEach(img => {
                const src = img.getAttribute('src') || img.getAttribute('data-src');
                if (src && isValidImg(src)) {
                    images.push(src.startsWith('//') ? 'https:' + src : src);
                }
            });
            // 상품 이미지(img.goodwearmall.com)를 앞으로 정렬
            images.sort((a, b) => {
                const aIsProduct = a.includes('img.goodwearmall.com');
                const bIsProduct = b.includes('img.goodwearmall.com');
                return (bIsProduct ? 1 : 0) - (aIsProduct ? 1 : 0);
            });
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

        if not data or not data.get('goodsNm'):
            return None
        data['url'] = page.url
        data['scraped_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return data
    except Exception as e:
        print(f"   ⚠️ DOM 추출 오류: {str(e)[:80]}")
        return None


async def process_product(product_id, gender, category, context):
    """상품 ID를 받아 상세 페이지를 크롤링하고 JSON으로 저장합니다."""
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
        url = f"https://topten10.goodwearmall.com/product/{product_id}/detail"
        max_retries = 3

        for attempt in range(max_retries):
            p_page = await context.new_page()
            try:
                if attempt == 0:
                    print(f"   🔎 {product_id} 분석 중...")

                await p_page.goto(url, timeout=60000, wait_until="domcontentloaded")
                product_dict = await extract_product_data_from_dom(p_page)

                if product_dict:
                    os.makedirs(LOCAL_SAVE_DIR, exist_ok=True)
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(product_dict, f, ensure_ascii=False, indent=4)

                    print(f"   ✅ [저장완료] {filename}")

                    for oid in product_dict.get('other_color_ids', []):
                        if oid not in visited_products:
                            new_ids.append(oid)
                    break
                else:
                    print(f"   ⚠️ {product_id} 수집 불가 (페이지 없음)")
                    break
            except Exception as e:
                if "ERR_NAME_NOT_RESOLVED" in str(e) or "Timeout" in str(e):
                    print(f"   ⏳ {product_id} 재시도 중 ({attempt+1}/3)...")
                    await asyncio.sleep(5)
                else:
                    print(f"   ❌ {product_id} 에러: {str(e)[:60]}")
                    break
            finally:
                if not p_page.is_closed():
                    await p_page.close()

        await asyncio.sleep(0.5)

    # 같은 상품의 다른 색상도 추가 수집
    if new_ids:
        tasks = [process_product(oid, gender, category, context) for oid in new_ids]
        await asyncio.gather(*tasks)


async def crawl_category(gender, category_name, base_url, context):
    """
    카테고리 목록 페이지를 순회하며 상품 ID를 수집합니다.
    핵심 수정: 기존 정규식([A-Z]{3}\\d[A-Z]{2}\\d{4}...)이 실제 탑텐 ID와 불일치하여
    상품 ID를 전혀 수집 못하던 문제를 JS evaluate + href 파싱 방식으로 해결.
    """
    print(f"\n>>> 🎯 카테고리 시작: [{gender}-{category_name}]")
    page = await context.new_page()
    all_product_ids = set()

    try:
        await page.goto(base_url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        for page_num in range(1, 51):
            # 스크롤하여 레이지 로딩 상품도 노출
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                await asyncio.sleep(0.5)

            # /product/ID/detail 패턴의 href에서 상품 ID 직접 추출
            pids_on_page = await page.evaluate("""() => {
                const ids = new Set();
                // a태그 href에서 /product/ID/detail 패턴 파싱
                document.querySelectorAll('a[href*="/product/"]').forEach(a => {
                    const m = a.href.match(/\/product\/([A-Z0-9]{8,20})\/detail/);
                    if (m) ids.add(m[1]);
                });
                // data-goods-no 속성에서도 ID 탐색
                document.querySelectorAll('[data-goods-no], [data-goodsno]').forEach(el => {
                    const id = el.getAttribute('data-goods-no') || el.getAttribute('data-goodsno');
                    if (id && id.length >= 8 && id.length <= 20) ids.add(id.toUpperCase());
                });
                return Array.from(ids);
            }""")

            prev_count = len(all_product_ids)
            all_product_ids.update(pids_on_page)
            new_count = len(all_product_ids) - prev_count
            print(f"   📄 {page_num}페이지: +{new_count}개 → 누적 {len(all_product_ids)}개")

            # 상품이 없으면 마지막 페이지로 간주
            if not pids_on_page and page_num > 1:
                print(f"   ⏹ {page_num}페이지 상품 없음, 스캔 완료")
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
                    print(f"   ⏹ 다음 페이지 버튼 없음, 스캔 완료")
                    break
            except Exception:
                break

        await page.close()

        if all_product_ids:
            print(f"   🎉 총 {len(all_product_ids)}개 상품 ID 수집, 상세 크롤링 시작")
            tasks = [process_product(pid, gender, category_name, context) for pid in list(all_product_ids)]
            await asyncio.gather(*tasks)
        else:
            print(f"   ⚠️ [{gender}-{category_name}] 상품 ID를 하나도 수집하지 못했습니다.")
    except Exception as e:
        print(f"   ❌ 목록 에러: {e}")
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

    local_files = [f for f in os.listdir(LOCAL_SAVE_DIR) if f.endswith('.json')]
    if local_files:
        print(f"\n📦 로컬 수집 완료 ({len(local_files)}건). 저장 경로: {LOCAL_SAVE_DIR}")
    else:
        print("\n❌ 로컬에 수집된 파일이 없습니다.")


if __name__ == "__main__":
    asyncio.run(run())
