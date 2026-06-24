import os
import sys
import asyncio
import re
import json
from datetime import datetime
from playwright.async_api import async_playwright

# --- [설정] ---
# 한국어 주석: 스파오 브랜드 설정을 정의합니다.
BRAND_NAME = "spao"

if len(sys.argv) > 1:
    TODAY_STR = sys.argv[1]
else:
    TODAY_STR = datetime.now().strftime('%Y%m%d')

LOCAL_SAVE_DIR = f"data/{BRAND_NAME}/{TODAY_STR}"

# 크롤링 대상 카테고리 URL 맵 (이랜드몰 dispCategoryNo 카테고리 매핑 코드 사용)
TARGET_MAP = {
    "Women": {
        "Outer": [
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000006"
        ],
        "Top": [
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000015",
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000122",
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000068",
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000073"
        ],
        "Bottom": [
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000051",
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000064"
        ]
    },
    "Men": {
        "Outer": [
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000141"
        ],
        "Top": [
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000102",
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000127",
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000131",
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000136"
        ],
        "Bottom": [
            "https://www.spao.com/c/ctg?dispCategoryNo=2605000109"
        ]
    }
}

visited_products = set()
sem = asyncio.Semaphore(5)


async def extract_product_data_from_dom(page):
    """
    스파오 상품 상세 페이지 DOM에서 상품 데이터를 추출합니다.
    Next.js PWA 구조와 스파오 상세 레이아웃을 대응하여 파싱합니다.
    """
    try:
        await asyncio.sleep(1)
        # 판매 종료 등으로 메인 페이지로 리다이렉트된 경우 예외 처리
        if "/i/item" not in page.url or "itemNo=" not in page.url:
            import logging
            logging.getLogger("crawling_pipeline").warning(f"   ⚠️ 상세 페이지 리다이렉트 감지 (수집 스킵): {page.url}")
            return None
        data = await page.evaluate("""() => {
            const result = {};
            
            // 상품 코드 itemNo 파싱
            const itemNoMatch = location.href.match(/itemNo=([0-9]+)/i);
            result.goodsNo = itemNoMatch ? itemNoMatch[1] : "";
            
            // 상품명 추출 (.goods_name 또는 .goods_info .goods_tit 등)
            const titleEl = document.querySelector('.goods_name, .goods_info .goods_tit, .goods_title_area .goods_tit, h2.goods_name');
            result.goodsNm = titleEl ? titleEl.innerText.trim() : document.title.split('|')[0].trim();
            result.brandName = "SPAO";

            // 이미지 유효성 체크 (배너, 이벤트성 키워드 추가 차단)
            const logoKeywords = ['logo', 'icon', 'banner', 'btn', 'common', 'noimage', 'naver', 'kakao', 'popup', 'event', 'member', 'benefit'];
            const isValidImg = (url) => {
                if (!url) return false;
                const lower = url.toLowerCase();
                return !logoKeywords.some(kwd => lower.includes(kwd));
            };

            // 1. 썸네일 리스트 또는 스와이퍼 이미지 탐색 (대표 이미지 ID인 #repImg 및 .goods_img img를 최우선으로 조회하여 배너 오파싱 차단)
            let thumb = "";
            const mainImg = document.querySelector('#repImg, .goods_img img, .goods_thumb_list img, .item_img img, .swiper-slide img');
            if (mainImg) {
                thumb = mainImg.getAttribute('data-src') || mainImg.getAttribute('src') || "";
            }
            if (!thumb || !isValidImg(thumb)) {
                // og:image 활용 fallback
                const og = document.querySelector('meta[property="og:image"]')?.content || "";
                if (isValidImg(og)) thumb = og;
            }
            if (!thumb || !isValidImg(thumb)) {
                // img 태그 fallback
                const allImgs = document.querySelectorAll('img');
                for (const img of allImgs) {
                    const src = img.getAttribute('src') || "";
                    if (src.includes('elandrs.com') && isValidImg(src)) {
                        thumb = src;
                        break;
                    }
                }
            }

            if (thumb && thumb.startsWith('//')) thumb = 'https:' + thumb;
            result.thumbnailImageUrl = thumb || "";

            // 가격 추출 (.price_info .sales_price, .price_info .price em 등)
            let price = 0;
            const priceEl = document.querySelector('.price_info .sales_price, .price_info .price em, .sales_price, .price_info em, .goods_price em');
            if (priceEl) {
                price = parseInt(priceEl.innerText.replace(/[^0-9]/g, '') || "0");
            }
            result.price = price;

            // 품절 여부
            let isSoldOut = false;
            const buyBtn = document.querySelector('.btn_buy, .btn-buy, .goods_btn_buy, .buy_btn');
            if (buyBtn && (buyBtn.innerText.includes('품절') || buyBtn.disabled || buyBtn.classList.contains('disabled'))) {
                isSoldOut = true;
            }
            if (price === 0) isSoldOut = true;
            result.is_sold_out = isSoldOut;

            // 사이즈/재고 정보 (.goods_option select 또는 select[name="size_code"] 등)
            const sizeStockInfo = [];
            
            // 옵션 선택 버튼 리스트 탐색 (.size_list li button 등)
            const sizeButtons = document.querySelectorAll('.size_list li button, .option_list button, .option_size button');
            sizeButtons.forEach(btn => {
                const name = btn.innerText.trim();
                if (name) {
                    const isItemSoldOut = btn.classList.contains('disabled') || btn.disabled || btn.innerText.includes('품절');
                    sizeStockInfo.push({
                        size: name.replace(/품절/g, '').trim(),
                        is_sold_out: isItemSoldOut,
                        stock_qty: isItemSoldOut ? 0 : 999
                    });
                }
            });

            // select 박스 형태 Fallback
            if (sizeStockInfo.length === 0) {
                document.querySelectorAll('select[name*="size"], select.goods_option, select.option_select').forEach(sel => {
                    sel.querySelectorAll('option').forEach(opt => {
                        const name = opt.innerText.trim();
                        if (name && !name.includes('선택') && opt.value) {
                            const isItemSoldOut = name.includes('품절') || opt.disabled;
                            sizeStockInfo.push({
                                size: name.replace(/품절/g, '').trim(),
                                is_sold_out: isItemSoldOut,
                                stock_qty: isItemSoldOut ? 0 : 999
                            });
                        }
                    });
                });
            }
            result.size_stock = sizeStockInfo;

            // 다른 색상 상품 ID
            const otherColorIds = [];
            document.querySelectorAll('.goods_option_color a, .option_color a, ul.color a').forEach(chip => {
                const href = chip.getAttribute('href') || "";
                const match = href.match(/itemNo=([0-9]+)/i);
                if (match && match[1] && match[1] !== result.goodsNo) {
                    otherColorIds.push(match[1]);
                }
            });
            result.other_color_ids = [...new Set(otherColorIds)];

            // 추가 이미지 목록
            const images = [];
            document.querySelectorAll('img').forEach(img => {
                const src = img.getAttribute('src') || img.getAttribute('data-src');
                if (src && isValidImg(src)) {
                    images.push(src.startsWith('//') ? 'https:' + src : src);
                }
            });
            result.goodsImages = [...new Set(images)];

            // 상세/소재 정보
            const specInfo = {};
            document.querySelectorAll('.goods_spec table tr, .spec_table tr, table tr').forEach(row => {
                const key = row.querySelector('th')?.innerText.trim();
                const val = row.querySelector('td')?.innerText.trim().replace(/\\n/g, ' ');
                if (key && val && key.length < 20) specInfo[key] = val;
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
    """상품 상세 처리 함수"""
    if product_id in visited_products:
        return
    visited_products.add(product_id)

    filename = f"{BRAND_NAME}_{gender.lower()}_{category.lower()}_{product_id}.json"
    filepath = os.path.join(LOCAL_SAVE_DIR, filename)

    if os.path.exists(filepath):
        return

    async with sem:
        url = f"https://www.spao.com/i/item?itemNo={product_id}"
        p_page = await context.new_page()
        try:
            await p_page.goto(url, timeout=60000, wait_until="domcontentloaded")
            product_dict = await extract_product_data_from_dom(p_page)

            if product_dict:
                os.makedirs(LOCAL_SAVE_DIR, exist_ok=True)
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(product_dict, f, ensure_ascii=False, indent=4)
        except Exception:
            pass
        finally:
            if not p_page.is_closed():
                await p_page.close()


if __name__ == "__main__":
    pass
