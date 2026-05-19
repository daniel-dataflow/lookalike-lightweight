"""
migration_tool.py - Lookalike 이미지 → Cloudinary 마이그레이션 도구
=======================================================================
동작 흐름:
  1. ./data/raw/<브랜드>/image/** 내 이미지 파일을 모두 탐색
  2. 파일명에서 product_id를 추출 (마지막 _NN 앵글 번호 제거)
       예) uniqlo_men_bottom_E418910-000_00.jpg → E418910-000
  3. DB(products 테이블)에서 product_id 매칭 확인
  4. 이미 img_url이 채워진 상품은 건너뜀
  5. Cloudinary에 업로드 (upload_preset='lookalike_preset')
  6. 반환된 secure_url → DB img_url 업데이트
  7. 50건마다 자동 commit + 진행 요약 출력

실행:
  python migration_tool.py [options]

Options:
  --dry-run       실제 업로드/DB 업데이트 없이 매칭만 시뮬레이션
  --brand BRAND   특정 브랜드만 처리 (예: --brand uniqlo)
  --overwrite     기존 img_url이 있어도 덮어씀
  --limit N       최대 N건만 처리 (테스트용)
  --angle XX      특정 앵글 번호만 처리 (예: --angle 00 → 메인 이미지만)
"""
import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import cloudinary
import cloudinary.uploader
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from tqdm import tqdm

# ──────────────────────────────────────────────────────────────────────────────
# 설정 로드
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "migration.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Cloudinary 설정
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

# DB 설정
DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", "5433")),
    "dbname": os.getenv("POSTGRES_DB", "datadb"),
    "user": os.getenv("POSTGRES_USER", "datauser"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}

# 이미지 루트 경로
IMAGE_ROOT = BASE_DIR / "data" / "raw"

# Cloudinary 프리셋 (Signed 모드)
UPLOAD_PRESET = os.getenv("CLOUDINARY_UPLOAD_PRESET", "lookalike_preset")

# 배치 커밋 단위
COMMIT_BATCH = 50

# 지원 이미지 확장자
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


# ──────────────────────────────────────────────────────────────────────────────
# 파일명 파싱 유틸리티
# ──────────────────────────────────────────────────────────────────────────────
def parse_product_id(filename: str) -> Optional[str]:
    """
    파일명에서 product_id를 추출합니다.

    파일명 패턴: {brand}_{gender}_{category}_{product_id}_{angle}.{ext}
    예) uniqlo_men_bottom_E418910-000_00.jpg → product_id = E418910-000
        uniqlo_women_top_E483877-000_09.jpg  → product_id = E483877-000
        musinsa_men_top_MS1234_01.jpg        → product_id = MS1234

    전략:
      - 확장자 제거 후 stem을 `_`로 분리
      - 마지막 요소가 숫자만으로 구성된 앵글 번호면 제거
      - 나머지에서 앞 3개 세그먼트(브랜드·성별·카테고리)를 제외한 나머지를 product_id로 결합
    """
    stem = Path(filename).stem
    parts = stem.split("_")

    # 최소 5개 파트: brand + gender + category + product_id + angle
    if len(parts) < 4:
        return None

    # 마지막 파트가 앵글 번호(순수 숫자 1~3자리)인지 확인
    last = parts[-1]
    if re.fullmatch(r"\d{1,3}", last):
        parts = parts[:-1]  # 앵글 번호 제거

    # 앞 3개(브랜드·성별·카테고리) 제거 → 남은 것을 product_id로 조합
    if len(parts) <= 3:
        return None

    product_id_parts = parts[3:]  # brand, gender, category 이후
    return "_".join(product_id_parts)


def get_angle(filename: str) -> Optional[str]:
    """파일명에서 앵글 번호 추출 (예: '_00' → '00')"""
    stem = Path(filename).stem
    parts = stem.split("_")
    if len(parts) >= 2 and re.fullmatch(r"\d{1,3}", parts[-1]):
        return parts[-1].zfill(2)
    return None


def get_brand(filepath: Path) -> str:
    """경로에서 브랜드명 추출 (data/raw/<brand>/image/*)"""
    parts = filepath.parts
    try:
        raw_idx = parts.index("raw")
        return parts[raw_idx + 1]
    except (ValueError, IndexError):
        return "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# DB 연결 및 조회
# ──────────────────────────────────────────────────────────────────────────────
def get_db_connection():
    """PostgreSQL 커넥션 반환"""
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def load_product_index(conn, brand_filter: Optional[str] = None) -> dict:
    """
    DB에서 상품 정보를 로드하여 매칭을 위한 인덱스 생성.
    결과: { "raw_id": [product_id1, product_id2, ...], ... }
    """
    query = "SELECT product_id, brand_name, model_code, origin_url, img_url FROM products"
    params = []
    if brand_filter:
        query += " WHERE LOWER(brand_name) = LOWER(%s)"
        params.append(brand_filter)

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    index = {}
    img_url_map = {} # product_id -> img_url (이미 존재 여부 확인용)

    for row in rows:
        pid = str(row["product_id"])
        img_url_map[pid] = row["img_url"]
        
        # 매칭 후보 키들 수집
        keys = []
        if row["model_code"]:
            keys.append(row["model_code"].strip())
        
        if row["origin_url"]:
            # URL에서 ID 추출 시도
            url = row["origin_url"]
            # Musinsa/Uniqlo/etc 공통 패턴: URL 마지막 경로 세그먼트
            match = re.search(r"/products?/([^/?#]+)", url)
            if match:
                keys.append(match.group(1))
            # SSF Shop (8seconds) 패턴
            match_ssf = re.search(r"/8-seconds/([^/]+)/good", url)
            if match_ssf:
                keys.append(match_ssf.group(1))

        for k in set(keys):
            if not k: continue
            if k not in index:
                index[k] = []
            if pid not in index[k]:
                index[k].append(pid)

    logger.info(f"DB에서 {len(rows):,}개 상품 로드, {len(index):,}개 매칭 키 인덱싱 완료")
    return index, img_url_map


# ──────────────────────────────────────────────────────────────────────────────
# 이미지 파일 탐색
# ──────────────────────────────────────────────────────────────────────────────
def collect_image_files(brand_filter: Optional[str] = None, angle_filter: Optional[str] = None) -> list[Path]:
    """
    data/raw/<브랜드>/image/ 하위의 모든 이미지 파일 수집.
    angle_filter: '00'이면 메인 이미지(앵글 00)만 수집
    """
    files = []
    brands = [brand_filter] if brand_filter else [d.name for d in IMAGE_ROOT.iterdir() if d.is_dir()]

    for brand in brands:
        img_dir = IMAGE_ROOT / brand / "image"
        if not img_dir.exists():
            logger.warning(f"이미지 폴더 없음: {img_dir}")
            continue

        for f in img_dir.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in SUPPORTED_EXTS:
                continue
            if angle_filter and get_angle(f.name) != angle_filter.zfill(2):
                continue
            files.append(f)

    files.sort()
    logger.info(f"이미지 파일 {len(files):,}개 탐색 완료")
    return files


# ──────────────────────────────────────────────────────────────────────────────
# Cloudinary 업로드
# ──────────────────────────────────────────────────────────────────────────────
def upload_to_cloudinary(filepath: Path, raw_id: str, brand: str, retries: int = 3) -> Optional[str]:
    """
    Cloudinary 업로드 및 Secure URL 반환.
    - folder 파라미터를 사용하여 실제 계층형 폴더를 생성합니다.
    - 예: 8seconds_men_bottom_abc.jpg -> 폴더: products/8seconds/bottom, 파일명: 8seconds_men_bottom_abc
    """
    try:
        filename = filepath.name
        stem = filepath.stem
        parts = stem.split("_")
        
        # 기본값 설정
        brand_folder = brand
        category_folder = "unclassified"
        
        # 파일명에서 정보 추출 (형식: 브랜드_성별_카테고리_ID_앵글)
        if len(parts) >= 3:
            brand_folder = parts[0]
            category_folder = parts[2]
            
        # 실제 폴더 경로와 파일명 분리 (카테고리 단계 제거)
        target_folder = f"products/{brand_folder}"
        target_public_id = stem

        for attempt in range(1, retries + 1):
            try:
                # debug 로그 추가
                if attempt == 1:
                    tqdm.write(f"  -> 시도 경로: {target_folder}/{target_public_id}")

                result = cloudinary.uploader.upload(
                    str(filepath),
                    asset_folder=target_folder,
                    public_id=target_public_id,
                    overwrite=True,
                    resource_type="image",
                )
                
                # 서버 응답 상세 로그
                tqdm.write(f"  [Server Response] public_id: {result.get('public_id')}, folder: {result.get('folder')}, asset_folder: {result.get('asset_folder')}")
                
                return result.get("secure_url")
            except Exception as e:
                wait = 2 ** attempt
                logger.warning(f"Cloudinary 업로드 실패 [{attempt}/{retries}] {filename}: {e} → {wait}초 후 재시도")
                if attempt < retries:
                    time.sleep(wait)

        logger.error(f"Cloudinary 업로드 최종 실패: {filename}")
        return None
    except Exception as e:
        logger.error(f"Cloudinary 처리 중 오류 ({filepath.name}): {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# DB 업데이트
# ──────────────────────────────────────────────────────────────────────────────
def update_img_url(conn, product_id: str, secure_url: str) -> bool:
    """products.img_url을 secure_url로 업데이트"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE products SET img_url = %s WHERE product_id = %s",
                (secure_url, product_id),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"DB 업데이트 실패 ({product_id}): {e}")
        conn.rollback()
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 메인 마이그레이션 로직
# ──────────────────────────────────────────────────────────────────────────────
def run_migration(args):
    # 1. 이미지 파일 수집
    image_files = collect_image_files(
        brand_filter=args.brand,
        angle_filter=args.angle,
    )
    if args.limit:
        image_files = image_files[: args.limit]
        logger.info(f"--limit {args.limit} 적용 → {len(image_files)}개만 처리")

    if not image_files:
        logger.error("처리할 이미지 파일이 없습니다.")
        return

    # 2. DB 연결 및 상품 맵 로드
    conn = get_db_connection()
    try:
        product_index, img_url_map = load_product_index(conn, brand_filter=args.brand)
    except Exception as e:
        logger.error(f"DB 로드 실패: {e}")
        conn.close()
        return

    # 3. 통계 카운터
    stats = {
        "total": len(image_files),
        "matched_files": 0,
        "skipped_no_match": 0,
        "uploaded": 0,
        "updated_records": 0,
        "failed_upload": 0,
        "failed_update": 0,
    }

    pending_commit = 0
    progress = tqdm(image_files, desc="마이그레이션", unit="파일", ncols=100, colour="cyan")

    for filepath in progress:
        brand = get_brand(filepath)
        raw_id = parse_product_id(filepath.name)

        # ── 파일명 파싱 실패 ──────────────────────────────
        if not raw_id:
            tqdm.write(f"[SKIP] 파싱 실패: {filepath.name}")
            stats["skipped_no_match"] += 1
            continue

        # ── DB 매칭 확인 (Index Lookup) ────────────────────
        target_pids = product_index.get(raw_id, [])
        if not target_pids:
            tqdm.write(f"[SKIP] DB 미매칭: {filepath.name} (raw_id={raw_id})")
            stats["skipped_no_match"] += 1
            continue

        stats["matched_files"] += 1
        
        # 필터링: 업데이트가 필요한 PID만 추출
        pids_to_update = []
        for pid in target_pids:
            existing_url = img_url_map.get(pid)
            if not existing_url or args.overwrite:
                pids_to_update.append(pid)
        
        if not pids_to_update:
            tqdm.write(f"[SKIP] 모두 URL 있음: {raw_id} ({len(target_pids)}개 상품)")
            continue

        # ── Dry-run ───────────────────────────────────────
        if args.dry_run:
            tqdm.write(f"[DRY-RUN] 업로드 예정: {filepath.name} → {len(pids_to_update)}개 상품 ({', '.join(pids_to_update[:3])}...)")
            stats["uploaded"] += 1
            continue

        # ── Cloudinary 업로드 ─────────────────────────────
        progress.set_postfix(raw_id=raw_id, brand=brand)
        secure_url = upload_to_cloudinary(filepath, raw_id, brand)

        if not secure_url:
            stats["failed_upload"] += 1
            continue

        stats["uploaded"] += 1
        tqdm.write(f"[OK] 업로드 완료: {filepath.name} → {secure_url}")

        # ── DB 업데이트 (연관된 모든 상품) ───────────────────
        for pid in pids_to_update:
            success = update_img_url(conn, pid, secure_url)
            if success:
                img_url_map[pid] = secure_url
                stats["updated_records"] += 1
                pending_commit += 1
            else:
                stats["failed_update"] += 1

        # ── 배치 커밋 ─────────────────────────────────────
        if pending_commit >= COMMIT_BATCH:
            conn.commit()
            pending_commit = 0
            tqdm.write(f"[COMMIT] {COMMIT_BATCH}건 이상 커밋 완료 (누계 업데이트: {stats['updated_records']}건)")

    # 4. 잔여 커밋
    if pending_commit > 0 and not args.dry_run:
        conn.commit()
        logger.info(f"잔여 {pending_commit}건 최종 커밋 완료")

    conn.close()

    # 5. 최종 요약 출력
    print("\n" + "=" * 60)
    print("📊  마이그레이션 결과 요약")
    print("=" * 60)
    print(f"  전체 이미지 파일    : {stats['total']:>8,}")
    print(f"  매칭 성공 파일      : {stats['matched_files']:>8,}")
    print(f"  건너뜀 (미매칭 등)  : {stats['skipped_no_match']:>8,}")
    print(f"  Cloudinary 업로드   : {stats['uploaded']:>8,}")
    print(f"  DB 레코드 업데이트  : {stats['updated_records']:>8,}")
    print(f"  업로드 실패         : {stats['failed_upload']:>8,}")
    print(f"  DB 업데이트 실패    : {stats['failed_update']:>8,}")
    if args.dry_run:
        print("\n  ※ --dry-run 모드: 실제 업로드/업데이트 없이 시뮬레이션만 수행했습니다.")
    print("=" * 60)
    logger.info("마이그레이션 완료")


# ──────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Lookalike 이미지 → Cloudinary 마이그레이션 도구",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="시뮬레이션만 수행 (실제 업로드/DB 변경 없음)")
    parser.add_argument("--brand", type=str, default=None, help="특정 브랜드만 처리 (예: uniqlo)")
    parser.add_argument("--overwrite", action="store_true", help="기존 img_url이 있어도 덮어씀")
    parser.add_argument("--limit", type=int, default=None, help="최대 처리 건수 (테스트용)")
    parser.add_argument("--angle", type=str, default=None, help="특정 앵글 번호만 처리 (예: 00 → 메인 이미지만)")
    args = parser.parse_args()

    # 설정 검증
    missing = []
    for key in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        if not os.getenv(key):
            missing.append(key)
    if missing:
        logger.error(f".env에 다음 변수가 누락됐습니다: {', '.join(missing)}")
        sys.exit(1)

    if not IMAGE_ROOT.exists():
        logger.error(f"이미지 루트 경로가 존재하지 않습니다: {IMAGE_ROOT}")
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("Lookalike 이미지 마이그레이션 시작")
    logger.info(f"  이미지 루트: {IMAGE_ROOT}")
    logger.info(f"  DB: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    logger.info(f"  Cloudinary 프리셋: {UPLOAD_PRESET}")
    logger.info(f"  옵션: dry-run={args.dry_run}, brand={args.brand}, overwrite={args.overwrite}, limit={args.limit}, angle={args.angle}")
    logger.info("=" * 50)

    run_migration(args)


if __name__ == "__main__":
    main()
