import os
import sys
import asyncio
import re
import json
from datetime import datetime
from playwright.async_api import async_playwright

# --- [설정] ---
# 한국어 주석: 지오다노 브랜드 설정을 정의합니다.
BRAND_NAME = "giordano"

if len(sys.argv) > 1:
    TODAY_STR = sys.argv[1]
else:
    TODAY_STR = datetime.now().strftime('%Y%m%d')

LOCAL_SAVE_DIR = f"data/{BRAND_NAME}/{TODAY_STR}"

# 크롤링 대상 카테고리 URL 맵 (지오다노 cno1 카테고리 매핑 코드 사용)
TARGET_MAP = {
    "Men": {
        "Outer": [
            "https://www.giordano.co.kr/shop/big_section.php?cno1=1019"
        ],
        "Top": [
            "https://www.giordano.co.kr/shop/big_section.php?cno1=1012"
        ],
        "Bottom": [
            "https://www.giordano.co.kr/shop/big_section.php?cno1=1013"
        ]
    },
    "Women": {
        "Outer": [
            "https://www.giordano.co.kr/shop/big_section.php?cno1=1025"
        ],
        "Top": [
            "https://www.giordano.co.kr/shop/big_section.php?cno1=1023"
        ],
        "Bottom": [
            "https://www.giordano.co.kr/shop/big_section.php?cno1=1024"
        ]
    }
}

visited_products = set()
sem = asyncio.Semaphore(5)


async def extract_product_data_from_dom(page):
    """
    지오다노 상품 상세 페이지 DOM 및 전역 변수에서 상품 데이터를 추출합니다.
    WISA 템플릿과 Slick 슬라이더 구조에 맞추어 이미지와 옵션을 파싱합니다.
    """
    try:
        await asyncio.sleep(1)
        data = await page.evaluate(r"""() => {
            const result = {};
            
            // 상품 코드 pno 파싱
            const pnoMatch = location.href.match(/pno=([A-Z0-9]+)/i);
            result.goodsNo = pnoMatch ? pnoMatch[1] : "";
            
            // 상품명 추출 (.info .name 또는 .prdName 구조 대응)
            const titleEl = document.querySelector('.info .name, .prdName, .prd_title, h2.name');
            result.goodsNm = titleEl ? titleEl.innerText.trim() : document.title.split('|')[0].trim();
            result.brandName = "GIORDANO";

            // 이미지 유효성 체크
            const logoKeywords = ['logo', 'icon', 'banner', 'btn', 'common', 'noimage'];
            const isValidImg = (url) => {
                if (!url) return false;
                const lower = url.toLowerCase();
                return !logoKeywords.some(kwd => lower.includes(kwd));
            };

            // 1. Slick Slider 활성 이미지 또는 메인 이미지 탐색
            let thumb = "";
            const mainImg = document.querySelector('.slick-slide.slick-active img, .main_img img, #main_img_id, .prd_detail_img img');
            if (mainImg) {
                thumb = mainImg.getAttribute('data-src') || mainImg.getAttribute('src') || "";
            }
            if (!thumb || !isValidImg(thumb)) {
                // og:image 활용 fallback
                const og = document.querySelector('meta[property="og:image"]')?.content || "";
                if (isValidImg(og)) thumb = og;
            }
            if (!thumb || !isValidImg(thumb)) {
                // img 태그 중 큰 이미지 추출 fallback
                const allImgs = document.querySelectorAll('img');
                for (const img of allImgs) {
                    const src = img.getAttribute('src') || "";
                    if ((src.includes('/shop/') || src.includes('/goods/')) && isValidImg(src)) {
                        thumb = src;
                        break;
                    }
                }
            }

            if (thumb && thumb.startsWith('//')) thumb = 'https:' + thumb;
            result.thumbnailImageUrl = thumb || "";

            // 가격 추출 (bgProduct.price 전역 변수 우선 및 DOM Fallback)
            let price = 0;
            if (typeof bgProduct !== 'undefined' && bgProduct && bgProduct.price) {
                price = parseInt(bgProduct.price);
            }
            if (!price || price === 0) {
                // info data-sell_prc 또는 data-lower_prc 우선 추출
                const infoEl = document.querySelector('.info[data-sell_prc], .info[data-lower_prc]');
                if (infoEl) {
                    const prcText = infoEl.getAttribute('data-sell_prc') || infoEl.getAttribute('data-lower_prc') || "";
                    price = parseInt(prcText.replace(/[^0-9]/g, '') || "0");
                }
            }
            if (!price || price === 0) {
                const priceEl = document.querySelector('.sell-price, .payprice, .prd_price, .price');
                if (priceEl) {
                    let text = priceEl.innerText.trim();
                    const lines = text.split('\n');
                    for (const line of lines) {
                        const num = parseInt(line.replace(/[^0-9]/g, '') || "0");
                        if (num > 0 && num < 10000000) {
                            price = num;
                            break;
                        }
                    }
                    if (!price) {
                        price = parseInt(text.replace(/[^0-9]/g, '') || "0");
                    }
                }
            }
            result.price = price;

            // 품절 여부
            let isSoldOut = false;
            const buyBtn = document.querySelector('.btn-buy, .btn-order, .btn-cart, #btn_buy');
            if (buyBtn && (buyBtn.innerText.includes('품절') || buyBtn.disabled || buyBtn.classList.contains('soldout'))) {
                isSoldOut = true;
            }
            if (price === 0) isSoldOut = true;
            result.is_sold_out = isSoldOut;

            // 사이즈/재고 정보 (WISA 옵션 칩 .optChipSet2 클래스 대상)
            const sizeStockInfo = [];
            document.querySelectorAll('.optChipSet2 a, .size_list a, ul.size a').forEach(chip => {
                const name = chip.innerText.trim();
                if (name) {
                    const isItemSoldOut = chip.classList.contains('disabled') || chip.classList.contains('soldout') || chip.getAttribute('aria-disabled') === 'true';
                    sizeStockInfo.push({
                        size: name,
                        is_sold_out: isItemSoldOut,
                        stock_qty: isItemSoldOut ? 0 : 999
                    });
                }
            });
            
            // 옵션 칩이 없고 드롭다운(select) 방식일 경우의 Fallback
            if (sizeStockInfo.length === 0) {
                document.querySelectorAll('select[name*="option"], select[name*="size"] option').forEach(opt => {
                    const name = opt.innerText.trim();
                    if (name && !name.includes('선택') && !opt.disabled) {
                        const isItemSoldOut = name.includes('품절') || name.includes('품절');
                        sizeStockInfo.push({
                            size: name.replace(/\[.*\]|\(.*\)/g, '').trim(),
                            is_sold_out: isItemSoldOut,
                            stock_qty: isItemSoldOut ? 0 : 999
                        });
                    }
                });
            }
            result.size_stock = sizeStockInfo;

            // 다른 색상 상품 ID
            const otherColorIds = [];
            document.querySelectorAll('.optChipSet1 a, .color_chip a, ul.color a').forEach(chip => {
                const onclick = chip.getAttribute('onclick') || chip.getAttribute('href') || "";
                const match = onclick.match(/pno=([A-Z0-9]+)/i);
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
            document.querySelectorAll('.prd_details table tr, .prd_info_table tr').forEach(row => {
                const key = row.querySelector('th')?.innerText.trim();
                const val = row.querySelector('td')?.innerText.trim().replace(/\n/g, ' ');
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
    """상품 상세 처리 함수"""
    if product_id in visited_products:
        return
    visited_products.add(product_id)

    filename = f"{BRAND_NAME}_{gender.lower()}_{category.lower()}_{product_id}.json"
    filepath = os.path.join(LOCAL_SAVE_DIR, filename)

    if os.path.exists(filepath):
        return

    async with sem:
        url = f"https://www.giordano.co.kr/shop/detail.php?pno={product_id}"
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
