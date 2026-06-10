import os
import re
import io
import json
import logging
import asyncio
import urllib.request
import urllib.error
import aiohttp
import psycopg2
from psycopg2.extras import execute_values
import cloudinary
import cloudinary.uploader
import traceback
import math
import numpy as np

# 로컬 개발 환경에서 .env 파일의 데이터베이스 및 Cloudinary 연결 정보들을 파싱하여 환경변수로 설정합니다.
try:
    _env_candidates = [
        r"D:\dev\lookalike-lightweight\.env",
        os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env")),
    ]
    for _env_path in _env_candidates:
        if os.path.isfile(_env_path):
            _prod_found = None
            _dw_found = None
            _dev_found = None
            _dev_dw_found = None
            _hf_token = None
            _hf_space_url = None
            _gemini_key = None
            
            with open(_env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#"):
                        continue
                    _m_mode = re.match(r'^ENV_MODE\s*=\s*(.+)$', _line)
                    if _m_mode:
                        _env_mode_found = _m_mode.group(1).strip().strip('"').strip("'").lower()
                    _m_prod = re.match(r'^PROD_DATABASE_URL\s*=\s*(.+)$', _line)
                    if _m_prod:
                        _prod_found = _m_prod.group(1).strip().strip('"').strip("'")
                    _m_dw = re.match(r'^PROD_DW_DATABASE_URL\s*=\s*(.+)$', _line)
                    if _m_dw:
                        _dw_found = _m_dw.group(1).strip().strip('"').strip("'")
                    _m_dev = re.match(r'^DEV_DATABASE_URL\s*=\s*(.+)$', _line)
                    if _m_dev:
                        _dev_found = _m_dev.group(1).strip().strip('"').strip("'")
                    _m_dev_dw = re.match(r'^DEV_DW_DATABASE_URL\s*=\s*(.+)$', _line)
                    if _m_dev_dw:
                        _dev_dw_found = _m_dev_dw.group(1).strip().strip('"').strip("'")
                    
                    # HuggingFace 파싱
                    _m_hf_tok = re.match(r'^HF_TOKEN\s*=\s*(.+)$', _line)
                    if _m_hf_tok:
                        _hf_token = _m_hf_tok.group(1).strip().strip('"').strip("'")
                    _m_hf_sp = re.match(r'^HF_SPACE_URL\s*=\s*(.+)$', _line)
                    if _m_hf_sp:
                        _hf_space_url = _m_hf_sp.group(1).strip().strip('"').strip("'")

                    # Cloudinary 매핑 패턴 파싱
                    _m_p_name = re.match(r'^PROD_CLOUDINARY_CLOUD_NAME\s*=\s*(.+)$', _line)
                    if _m_p_name:
                        _prod_cl_name = _m_p_name.group(1).strip().strip('"').strip("'")
                    _m_p_key = re.match(r'^PROD_CLOUDINARY_API_KEY\s*=\s*(.+)$', _line)
                    if _m_p_key:
                        _prod_cl_key = _m_p_key.group(1).strip().strip('"').strip("'")
                    _m_p_secret = re.match(r'^PROD_CLOUDINARY_API_SECRET\s*=\s*(.+)$', _line)
                    if _m_p_secret:
                        _prod_cl_secret = _m_p_secret.group(1).strip().strip('"').strip("'")
                        
                    _m_d_name = re.match(r'^DEV_CLOUDINARY_CLOUD_NAME\s*=\s*(.+)$', _line)
                    if _m_d_name:
                        _dev_cl_name = _m_d_name.group(1).strip().strip('"').strip("'")
                    _m_d_key = re.match(r'^DEV_CLOUDINARY_API_KEY\s*=\s*(.+)$', _line)
                    if _m_d_key:
                        _dev_cl_key = _m_d_key.group(1).strip().strip('"').strip("'")
                    _m_d_secret = re.match(r'^DEV_CLOUDINARY_API_SECRET\s*=\s*(.+)$', _line)
                    if _m_d_secret:
                        _dev_cl_secret = _m_d_secret.group(1).strip().strip('"').strip("'")
            
            # 환경변수 강제 셋팅
            if _hf_token:
                os.environ["HF_TOKEN"] = _hf_token
            if _hf_space_url:
                os.environ["HF_SPACE_URL"] = _hf_space_url

            # ENV_MODE에 맞게 DEV/PROD DB 및 Cloudinary 세트 매핑 (TEST 단계 제거됨)
            if _env_mode_found in ["local", "dev"]:
                if _dev_found and "${" not in _dev_found:
                    os.environ["DATABASE_URL"] = _dev_found
                    os.environ["PROD_DATABASE_URL"] = _dev_found
                if _dev_dw_found and "${" not in _dev_dw_found:
                    os.environ["DW_DATABASE_URL"] = _dev_dw_found
                
                # Cloudinary DEV 매핑
                if _dev_cl_name:
                    os.environ["CLOUDINARY_CLOUD_NAME"] = _dev_cl_name
                if _dev_cl_key:
                    os.environ["CLOUDINARY_API_KEY"] = _dev_cl_key
                if _dev_cl_secret:
                    os.environ["CLOUDINARY_API_SECRET"] = _dev_cl_secret
            else:
                if _prod_found and "${" not in _prod_found:
                    os.environ["DATABASE_URL"] = _prod_found
                    os.environ["PROD_DATABASE_URL"] = _prod_found
                if _dw_found and "${" not in _dw_found:
                    os.environ["DW_DATABASE_URL"] = _dw_found
                
                # Cloudinary PROD 매핑
                if _prod_cl_name:
                    os.environ["CLOUDINARY_CLOUD_NAME"] = _prod_cl_name
                if _prod_cl_key:
                    os.environ["CLOUDINARY_API_KEY"] = _prod_cl_key
                if _prod_cl_secret:
                    os.environ["CLOUDINARY_API_SECRET"] = _prod_cl_secret
            break
except Exception:
    pass

# Cloudinary 환경변수도 .env에서 로드
if not os.getenv("CLOUDINARY_CLOUD_NAME"):
    try:
        from dotenv import load_dotenv
        _env_path = r"D:\dev\lookalike-lightweight\.env"
        if os.path.isfile(_env_path):
            load_dotenv(_env_path, override=False)
    except ImportError:
        pass

logger = logging.getLogger("crawling_pipeline")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# --- 1. 알림 전송 모듈 (Discord/Slack 웹훅 연동) ---
def send_alert(message: str, level: str = "ERROR") -> None:
    """Slack 및 Discord 웹훅으로 장애 메시지를 다중 발송합니다."""
    slack_url = os.getenv("SLACK_WEBHOOK_URL")
    if slack_url:
        payload = {
            "text": f"🚨 [Lookalike Crawler {level}] {message}"
        }
        try:
            req = urllib.request.Request(
                slack_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    logger.info("✅ Slack 알림 전송 완료")
        except Exception as e:
            logger.error(f"❌ Slack 알림 전송 실패: {e}")

    discord_url = os.getenv("DISCORD_WEBHOOK_URL")
    if discord_url:
        payload = {
            "content": f"🚨 **[Lookalike Crawler {level}]**\n{message}"
        }
        try:
            req = urllib.request.Request(
                discord_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0"
                }
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status in (200, 204):
                    logger.info("✅ Discord 알림 전송 완료")
        except Exception as e:
            logger.error(f"❌ Discord 알림 전송 실패: {e}")

# --- 2. Cloudinary 메모리 스트리밍 업로드 ---
def configure_cloudinary():
    """Cloudinary 인증 정보를 초기화합니다."""
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")

    if cloud_name:
        cloud_name = cloud_name.strip("'\" \t\r\n")
    if api_key:
        api_key = api_key.strip("'\" \t\r\n")
    if api_secret:
        api_secret = api_secret.strip("'\" \t\r\n")

    if not all([cloud_name, api_key, api_secret]):
        raise ValueError("Cloudinary 필수 인증 환경변수가 누락되었습니다.")

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True
    )

async def upload_image_to_cloudinary(session: aiohttp.ClientSession, url: str, folder: str = "products", public_id: str = None) -> str:
    """
    지정된 이미지 URL로부터 바이너리를 임시 다운로드한 후,
    메모리 I/O(BytesIO) 상에서 Cloudinary 스토리지로 다이렉트 업로드합니다.
    """
    if not url or not url.startswith("http"):
        return ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    logger.warning(f"⚠️ 이미지 다운로드 실패 (HTTP {response.status}): {url}")
                    continue
                
                import io
                content = await response.read()
                file_io = io.BytesIO(content)

                def _upload():
                    upload_params = {
                        "folder": folder
                    }
                    if public_id:
                        upload_params["public_id"] = public_id
                    res = cloudinary.uploader.upload(
                        file_io,
                        **upload_params
                    )
                    url = res.get("secure_url") or res.get("url")
                    if url and "image/upload/" in url:
                        url = url.replace("image/upload/", "image/upload/q_auto,f_webp/")
                    return url

                uploaded_url = await asyncio.to_thread(_upload)
                if uploaded_url:
                    return uploaded_url

        except Exception as e:
            logger.warning(f"⚠️ 이미지 Cloudinary 업로드 시도 실패 ({attempt+1}/{max_retries}): {e}")
            await asyncio.sleep(1.0)

    raise RuntimeError(f"❌ {url} 이미지 Cloudinary 업로드 최종 실패")

# --- 2.1 HuggingFace API 연동 임베딩 생성 유틸 ---

# 로컬 CLIP 모델 캐시 (최초 1회 로드 후 재사용)
_LOCAL_CLIP_MODEL = None
_LOCAL_CLIP_TOKENIZER = None

def _get_local_clip_text_embedding(text: str) -> list:
    """
    transformers 라이브러리로 로컬 CLIP 텍스트 임베딩을 생성합니다.
    HF Inference API가 차단된 환경(로컬)에서 폴백으로 사용합니다.
    """
    global _LOCAL_CLIP_MODEL, _LOCAL_CLIP_TOKENIZER
    try:
        import torch
        import torch.nn.functional as F
        from transformers import CLIPTokenizer, CLIPModel

        if _LOCAL_CLIP_MODEL is None:
            logger.info("로컬 CLIP 모델 최초 로드 중 (openai/clip-vit-base-patch32)...")
            _LOCAL_CLIP_TOKENIZER = CLIPTokenizer.from_pretrained("openai/clip-vit-base-patch32")
            _LOCAL_CLIP_MODEL = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
            _LOCAL_CLIP_MODEL.eval()
            logger.info("로컬 CLIP 모델 로드 완료")

        inputs = _LOCAL_CLIP_TOKENIZER(
            text, return_tensors="pt", padding=True,
            truncation=True, max_length=77
        )
        with torch.no_grad():
            result = _LOCAL_CLIP_MODEL.get_text_features(**inputs)

        # transformers 버전에 따라 텐서 또는 ModelOutput 반환 가능 → 안전하게 처리
        if hasattr(result, 'pooler_output'):
            # BaseModelOutputWithPooling 계열 객체인 경우
            feat = result.pooler_output
        elif hasattr(result, 'last_hidden_state'):
            feat = result.last_hidden_state[:, 0, :]
        else:
            # 순수 텐서인 경우 (정상 케이스)
            feat = result

        # L2 정규화 (torch.nn.functional 사용, .norm() 메서드 의존 없음)
        feat = F.normalize(feat, p=2, dim=-1)
        return feat[0].tolist()
    except Exception as e:
        logger.warning(f"로컬 CLIP 텍스트 임베딩 실패: {e}")
        return None


# Inference API 네트워크 차단 상태 플래그 (타임아웃 방지용)
_HF_INFERENCE_API_FAILED = False
_GRADIO_CLIENT = None

async def get_clip_text_embedding(session: aiohttp.ClientSession, text: str, token: str) -> list:
    global _HF_INFERENCE_API_FAILED
    if not text:
        return None
        
    # 이미 API가 한 번 실패한 경우 타임아웃을 기다리지 않고 즉시 로컬 CLIP 모델 사용
    if not _HF_INFERENCE_API_FAILED:
        model_id = "openai/clip-vit-base-patch32"
        url = f"https://api-inference.huggingface.co/pipeline/feature-extraction/{model_id}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # DNS 실패(getaddrinfo) 시 OS 기본 타임아웃(30초)을 기다리는 문제 방지:
        # aiohttp.ClientTimeout으로 connect=3초, total=5초 명시적 지정
        _timeout = aiohttp.ClientTimeout(total=5, connect=3)
        try:
            async with session.post(url, json={"inputs": text}, headers=headers, timeout=_timeout) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    arr = np.array(result)
                    if arr.ndim > 1:
                        arr = arr[0]
                    vec = arr.tolist()
                    s = sum(x * x for x in vec)
                    if s > 0:
                        norm = math.sqrt(s)
                        return [x / norm for x in vec]
        except Exception as e:
            logger.warning(f"HF Inference API 차단/실패: {e}. 로컬 CLIP 모델로 폴백 및 이후 API 호출 스킵 설정.")
            _HF_INFERENCE_API_FAILED = True

    # HF API 실패 시 → 로컬 transformers CLIP 모델로 폴백
    local_vec = _get_local_clip_text_embedding(text)
    if local_vec:
        logger.info(f"로컬 CLIP 텍스트 임베딩 성공 (dim={len(local_vec)})")
        return local_vec

    # 로컬 모델도 실패 시 → mock 벡터 (마지막 수단)
    logger.error("텍스트 임베딩 모든 방법 실패. mock 벡터 반환.")
    fallback_vec = [0.0] * 512
    fallback_vec[0] = 1.0
    return fallback_vec

_GRADIO_CLIENT_LOCK = asyncio.Lock()

async def get_yolo_clip_image_embedding(session: aiohttp.ClientSession, image_url: str) -> list:
    """
    HuggingFace Space의 Gradio API를 호출하여
    YOLOv11 Pre-Cropping 기반의 정확도 높은 512d Fashion-CLIP 이미지 임베딩을 받아옵니다.
    """
    global _GRADIO_CLIENT
    hf_space_url = os.getenv("HF_SPACE_URL")
    if not hf_space_url:
        logger.warning("HF_SPACE_URL 환경변수가 없어 이미지 임베딩을 추출할 수 없습니다.")
        return None

    # 이미지 다운로드
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        _timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(image_url, headers=headers, timeout=_timeout) as response:
            if response.status == 200:
                image_bytes = await response.read()
            else:
                fallback_vec = [0.0] * 512
                fallback_vec[0] = 1.0
                return fallback_vec
    except Exception as e:
        logger.warning(f"임베딩용 이미지 다운로드 실패: {e}")
        fallback_vec = [0.0] * 512
        fallback_vec[0] = 1.0
        return fallback_vec

    # 임시 파일 작성
    import tempfile
    suffix = ".jpg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        suffix = ".png"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        suffix = ".webp"

    try:
        from gradio_client import Client, handle_file
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name

        try:
            async with _GRADIO_CLIENT_LOCK:
                if _GRADIO_CLIENT is None:
                    # httpx_kwargs 에 timeout 30초 명시하여 무한 블록을 방지합니다.
                    _GRADIO_CLIENT = Client(hf_space_url, httpx_kwargs={"timeout": 30.0})
            
            def _predict():
                return _GRADIO_CLIENT.predict(
                    image=handle_file(tmp_path),
                    api_name="/predict",
                )
            result = await asyncio.to_thread(_predict)
            if isinstance(result, dict) and result.get("status") != "error":
                embedding = result.get("embedding")
                if embedding:
                    return embedding
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    except Exception as e:
        logger.warning(f"HF Space YOLO-CLIP API 호출 오류: {e}")
        
    # 오프라인/패키지 미지원을 대비한 L2 정규화된 512차원 이미지 더미 벡터 반환 (첫 번째 차원만 1.0)
    fallback_vec = [0.0] * 512
    fallback_vec[0] = 1.0
    return fallback_vec

def clean_db_url(db_url: str) -> str:
    """DB URL 양 끝의 공백 및 따옴표를 지우고, 잘못된 형식(텍스트 포함 등)에서 postgresql:// 주소를 추출하여 DSN 오류를 방지합니다."""
    if not db_url:
        return db_url
    db_url = db_url.strip()
    # postgres:// 또는 postgresql://로 시작하는 패턴 추출
    import re
    match = re.search(r'(postgres(?:ql)?://\S+)', db_url)
    if match:
        cleaned = match.group(1)
        return cleaned.strip("'\"")
    return db_url.strip("'\"")

def get_prod_db_connection():
    """PROD_DATABASE_URL 환경 변수로부터 PostgreSQL 커넥션을 가져옵니다. (PROD DB)"""
    raw_url = os.getenv("PROD_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not raw_url:
        raise ValueError("PROD_DATABASE_URL 또는 DATABASE_URL 환경 변수가 필요합니다.")
    db_url = clean_db_url(raw_url)
    conn = psycopg2.connect(db_url)
    # 세션 타임존을 서울(KST)로 설정
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Seoul';")
    conn.commit()
    return conn

def get_dw_db_connection():
    """DW_DATABASE_URL 환경 변수로부터 PostgreSQL 커넥션을 가져옵니다. (DW DB)"""
    raw_url = os.getenv("DW_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not raw_url:
        raise ValueError("DW_DATABASE_URL 또는 DATABASE_URL 환경 변수가 필요합니다.")
    db_url = clean_db_url(raw_url)
    conn = psycopg2.connect(db_url)
    # 세션 타임존을 서울(KST)로 설정
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Seoul';")
    conn.commit()
    return conn

# --- 4. 순차 일련번호(product_id) 시퀀스 복원 발급기 ---
def get_next_product_id(cur, brand_name: str) -> str:
    """Neon DW DB의 brand_sequences 테이블을 이용해 Sequential ID를 순차 발급합니다. (예: 8S0001, 8S0002)"""
    brand_upper = brand_name.upper()
    brand_headers = {
        "8SECONDS": "8S",
        "MUSINSA": "MS",
        "TOPTEN": "TT",
        "UNIQLO": "UQ",
        "ZARA": "ZR"
    }
    header = brand_headers.get(brand_upper, brand_upper[:2])
    
    cur.execute("""
        INSERT INTO brand_sequences (brand_name, last_seq)
        VALUES (%s, 0)
        ON CONFLICT (brand_name) DO NOTHING
    """, (brand_upper,))
    
    cur.execute("""
        UPDATE brand_sequences 
        SET last_seq = last_seq + 1 
        WHERE brand_name = %s 
        RETURNING last_seq
    """, (brand_upper,))
    
    last_seq = cur.fetchone()[0]
    return f"{header}{last_seq:04d}"

# --- 5. 모니터링 테이블 연동 로그 유틸리티 ---
def log_pipeline_start(brand_name: str, pipeline_name: str = "crawling_pipeline") -> int:
    """pipeline_runs 테이블에 구동 시작 시각 및 레코드를 생성하여 run_id를 획득합니다. (DW DB 저장)"""
    conn = get_dw_db_connection()
    cur = conn.cursor()
    run_id = None
    try:
        # 동일 브랜드의 기존 멈춰 있는(RUNNING) 파이프라인 찌꺼기를 FAILED 로 정리
        cur.execute("""
            UPDATE pipeline_runs 
            SET status = 'FAILED', 
                finished_at = CURRENT_TIMESTAMP,
                metadata = metadata || '{"system_cleanup": "auto-failed due to new run start"}'::jsonb
            WHERE brand = %s AND status = 'RUNNING';
        """, (brand_name.upper(),))
        
        cur.execute("""
            INSERT INTO pipeline_runs (
                pipeline_name, brand, status, started_at, error_count, total_items, new_items, updated_items, metadata
            )
            VALUES (%s, %s, 'RUNNING', CURRENT_TIMESTAMP, 0, 0, 0, 0, '{}'::jsonb)
            RETURNING run_id
        """, (pipeline_name, brand_name.upper()))
        conn.commit()
        run_id = cur.fetchone()[0]
    except Exception as e:
        logger.error(f"❌ pipeline_runs 시작 로그 생성 오류: {e}")
    finally:
        cur.close()
        conn.close()
    return run_id

def log_pipeline_end(run_id: int, status: str, total_items: int = 0, new_items: int = 0, updated_items: int = 0, embed_count: int = 0, error_count: int = 0, metadata_dict: dict = None) -> None:
    """pipeline_runs 테이블의 상태 및 소요 시간을 갱신 및 종료 처리합니다. (DW DB 저장)"""
    if not run_id:
        return
    conn = get_dw_db_connection()
    cur = conn.cursor()
    try:
        # 실제 등록된 에러 카운트를 쿼리하여 보완
        actual_error_count = error_count
        try:
            cur.execute("SELECT count(*) FROM pipeline_errors WHERE run_id = %s", (run_id,))
            err_row = cur.fetchone()
            if err_row:
                actual_error_count = max(error_count, err_row[0])
        except Exception as query_err:
            logger.warning(f"실제 에러 카운트 조회 실패: {query_err}")

        meta_str = json.dumps(metadata_dict or {})
        cur.execute("""
            UPDATE pipeline_runs 
            SET status = %s,
                finished_at = CURRENT_TIMESTAMP,
                duration_sec = EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at))::integer,
                total_items = %s,
                new_items = %s,
                updated_items = %s,
                embed_count = %s,
                error_count = %s,
                metadata = %s::jsonb
            WHERE run_id = %s
        """, (status, total_items, new_items, updated_items, embed_count, actual_error_count, meta_str, run_id))
        conn.commit()
        logger.info(f"📋 pipeline_runs 상태 업데이트 완료 (run_id: {run_id}, status: {status}, error_count: {actual_error_count})")
    except Exception as e:
        logger.error(f"❌ pipeline_runs 종료 로그 갱신 오류: {e}")
    finally:
        cur.close()
        conn.close()

def log_pipeline_error(run_id: int, error_type: str, message: str, stack_trace: str = None, product_id: str = None, source_url: str = None, is_warning: bool = False) -> None:
    """pipeline_errors 테이블에 에러 원인 및 상세 stack trace를 상세 수집 기록합니다. (DW DB 저장)"""
    conn = get_dw_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO pipeline_errors (
                run_id, error_type, error_message, stack_trace, product_id, source_url, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """, (run_id, error_type, message, stack_trace or "", product_id or "", source_url or ""))
        
        # [중요] 경고(is_warning=True)가 아닌 실제 에러일 경우에만 pipeline_runs의 error_count를 1 증가 처리
        if run_id and not is_warning:
            cur.execute("UPDATE pipeline_runs SET error_count = error_count + 1 WHERE run_id = %s", (run_id,))
            
        conn.commit()
    except Exception as e:
        logger.error(f"❌ pipeline_errors 로그 적재 오류: {e}")
    finally:
        cur.close()
        conn.close()

def is_pipeline_blocked(brand_name: str) -> bool:
    """해당 브랜드의 최종 pipeline_runs 상태가 BLOCKED(정합성 오류 등으로 차단)인지 확인합니다."""
    conn = get_dw_db_connection()
    cur = conn.cursor()
    blocked = False
    try:
        cur.execute("""
            SELECT status FROM pipeline_runs 
            WHERE brand = %s 
            ORDER BY started_at DESC LIMIT 1
        """, (brand_name.upper(),))
        row = cur.fetchone()
        if row and row[0] == "BLOCKED":
            blocked = True
    except Exception as e:
        logger.error(f"❌ pipeline_runs 차단 체크 오류: {e}")
    finally:
        cur.close()
        conn.close()
    return blocked

# --- 6. Cloudinary 이미지 폴더 이관 및 유효성 검사 ---
def extract_public_id(url: str) -> str:
    """Cloudinary URL로부터 public_id를 추출합니다 (URL 인코딩 디코딩 처리 및 transformations/version 스킵 포함)."""
    if not url or "cloudinary.com" not in url:
        return ""
    import urllib.parse
    decoded_url = urllib.parse.unquote(url)
    
    # image/upload/ 이후의 경로를 추출
    marker = "/image/upload/"
    if marker not in decoded_url:
        return ""
    
    path_part = decoded_url.split(marker)[1]
    # 확장자 제거 (예: .jpg, .png 등)
    if "." in path_part:
        path_part = path_part.rsplit(".", 1)[0]
        
    parts = path_part.split("/")
    clean_parts = []
    for part in parts:
        # transformations 혹은 version 정보 스킵
        # 1. version 정보: v12345678
        if part.startswith("v") and part[1:].isdigit():
            continue
        # 2. transformations 파라미터: 쉼표가 들어있거나 c_limit, w_300 등 특정 패턴 검출
        if "," in part or any(part.startswith(prefix) for prefix in ["c_", "w_", "h_", "q_", "f_", "r_", "e_", "bo_", "co_", "bg_"]):
            continue
        # transformations 단일 파라미터 추가 검출 및 스킵
        if part in ["q_auto", "f_auto", "dpr_auto"]:
            continue
            
        clean_parts.append(part)
        
    return "/".join(clean_parts)

def move_cloudinary_image(old_public_id: str, new_public_id: str) -> str:
    """Cloudinary 상에서 이미지를 staging 폴더에서 products(혹은 test) 폴더로 원격 이동시키고 asset_folder 속성도 업데이트합니다."""
    if not old_public_id or not new_public_id:
        return ""
    try:
        import urllib.parse
        import cloudinary
        import cloudinary.uploader
        import cloudinary.api
        
        # Rename API 호출 전 한글 등의 public_id도 확실히 디코딩된 상태여야 Cloudinary API가 인식함
        decoded_old = urllib.parse.unquote(old_public_id)
        decoded_new = urllib.parse.unquote(new_public_id)
        
        # 1. public_id 변경
        res = cloudinary.uploader.rename(decoded_old, decoded_new, overwrite=True)
        
        # 2. 새로운 public_id에 알맞은 가상 asset_folder 속성 설정
        # 예: new_public_id 가 'test/uniqlo/FILENAME' 이면 'test/uniqlo'를 폴더명으로 추출
        import posixpath
        folder_path = posixpath.dirname(decoded_new)
        if folder_path:
            try:
                cloudinary.api.update(decoded_new, asset_folder=folder_path)
            except Exception as folder_err:
                logger.warning(f"⚠️ Cloudinary asset_folder 메타데이터 업데이트 실패: {folder_err}")
                
        return res.get("secure_url") or res.get("url")
    except Exception as e:
        logger.warning(f"⚠️ Cloudinary 이미지 이동 실패 ({old_public_id} -> {new_public_id}): {e}")
        return ""

async def validate_image_url_async(session: aiohttp.ClientSession, url: str) -> bool:
    """비동기 HTTP GET 요청을 보내 이미지 링크가 유효한지(Status Code 200) 확인합니다."""
    if not url or not url.startswith("http"):
        return False
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning(f"⚠️ 이미지 링크 확인 실패 ({url}): {e}")
        return False

# --- 7. 최종 2단계 스위칭 및 승인 연동 ---
async def swap_staging_to_production(brand_name: str, force: bool = False) -> bool:
    """
    스테이징 DB에 임시 적재된 지 24시간이 경과한(혹은 force=True인) 데이터를 최종 검증하고,
    Cloudinary 이미지를 staging/ 폴더에서 test/ 폴더로 이동시킨 후,
    이미 DW DB(staging_product_embeddings)에 기추출되어 보관 중인 벡터 데이터를 가져와
    Core DB의 products, naver_prices, product_embeddings 테이블로 원자적(Transaction)으로 일괄 복사 및 스위칭 반영합니다.
    """
    # 1. 자동 스위칭 차단(Lock) 여부 검사
    if is_pipeline_blocked(brand_name) and not force:
        err_msg = f"❌ [{brand_name}] 크롤러 데이터에 정합성 위반(BLOCKED) 상태가 걸려 있어 자동 스위칭을 사전 차단합니다. 어드민 페이지에서 수동 조치 및 승인이 필요합니다."
        logger.error(err_msg)
        send_alert(err_msg, level="CRITICAL")
        return False

    # 스위칭 파이프라인 구동 내역을 위한 run_id 미리 생성
    run_id = log_pipeline_start(brand_name, pipeline_name="swap_pipeline")

    stage_conn = get_dw_db_connection()
    stage_conn.autocommit = False
    stage_cur = stage_conn.cursor()

    try:
        brand_upper = brand_name.upper()
        
        # 2. 대상 데이터 조회 (24시간 경과 또는 force)
        if force:
            stage_cur.execute("""
                SELECT product_id, model_code, prod_name, base_price, gender, category_code, img_url, origin_url, create_dt
                FROM staging_products 
                WHERE brand_name = %s
            """, (brand_upper,))
        else:
            stage_cur.execute("""
                SELECT product_id, model_code, prod_name, base_price, gender, category_code, img_url, origin_url, create_dt
                FROM staging_products 
                WHERE brand_name = %s AND create_dt <= CURRENT_TIMESTAMP - INTERVAL '24 hours'
            """, (brand_upper,))
            
        staging_rows = stage_cur.fetchall()
        staging_count = len(staging_rows)
        
        logger.info(f"📊 [{brand_name}] 대기 중인 스테이징 데이터: {staging_count} 건 (force={force})")
        if staging_count == 0:
            err_msg = f"❌ [{brand_name}] 스위칭 대상 스테이징 상품 개수가 0개입니다. 원본 데이터 보호를 위해 이관(Swap)을 차단합니다."
            logger.error(err_msg)
            log_pipeline_error(run_id, "SWAP_GUARD_ZERO_DATA", err_msg)
            log_pipeline_end(run_id, "FAILED", total_items=0, error_count=1)
            stage_cur.close()
            stage_conn.close()
            return False

        # [검증 제약조건 1] 데이터 건수가 지나치게 작으면 에러로 판단 (임계치: 10건)
        MIN_THRESHOLD = 10
        if not force and staging_count < MIN_THRESHOLD:
            raise ValueError(f"수집된 상품 수({staging_count}건)가 임계치({MIN_THRESHOLD}건) 미만입니다. 대기 수량 부족 또는 차단이 의심됩니다.")

        # [검증 제약조건 2] 필수 필드 유실율 검사
        null_names = sum(1 for r in staging_rows if not r[2])
        null_categories = sum(1 for r in staging_rows if not r[5])
        max_allowed_nulls = int(staging_count * 0.05)
        if null_names > max_allowed_nulls:
            raise ValueError(f"이름 유실 건수({null_names}건)가 허용 한도({max_allowed_nulls}건)를 초과했습니다.")
        if null_categories > max_allowed_nulls:
            raise ValueError(f"카테고리 유실 건수({null_categories}건)가 허용 한도({max_allowed_nulls}건)를 초과했습니다.")

        logger.info("✅ 데이터 필드 검증 통과 (Validation Success)! 이미지 유효성 심층 테스트 및 Cloudinary 이관 진행...")

        # [검증 제약조건 3] 이미지 링크 작동성 비동기 검사 및 Cloudinary 폴더 이관
        configure_cloudinary()
        
        updated_rows = []
        sem = asyncio.Semaphore(10)  # 이미지 유효성 체크 동시성 10 설정
        
        async def process_single_product(session, row):
            async with sem:
                prod_id, model_code, prod_name, base_price, gender, cat_code, img_path, origin_url, create_dt = row
                prod_img_url = img_path
                
                # 1. 이미지 유효성 체크
                if img_path and img_path.startswith("http"):
                    is_valid = await validate_image_url_async(session, img_path)
                    if not is_valid:
                        logger.warning(f"⚠️ 깨진 이미지 링크 감지: 상품 {prod_id} ({img_path})")
                        if not force:
                            raise ValueError(f"이미지 URL({img_path})이 작동하지 않거나 만료되었습니다.")

                # 2. Cloudinary staging/ 폴더에서 products/ 폴더로 이동 처리 (DEV/PROD 구분 유지)
                old_pub_id = extract_public_id(img_path)
                if old_pub_id and "staging/" in old_pub_id:
                    new_pub_id = old_pub_id.replace("staging/", "products/")
                    logger.info(f"   🚚 Cloudinary 이미지 이동: {old_pub_id} -> {new_pub_id}")
                    moved_url = await asyncio.to_thread(move_cloudinary_image, old_pub_id, new_pub_id)
                    if moved_url:
                        prod_img_url = moved_url
                    else:
                        # 이미지 이동 실패 시 pipeline_errors 테이블에 경고 로그 기록 (run_id 매칭)
                        err_msg = f"Cloudinary 이미지 이동 실패 (Staging -> Products): {old_pub_id} (이미지가 Cloudinary에 존재하지 않거나 덮어쓰기 권한 에러)"
                        logger.warning(f"⚠️ {err_msg}")
                        log_pipeline_error(run_id, "CLOUDINARY_MOVE_WARN", err_msg, product_id=prod_id, source_url=img_path, is_warning=True)
                
                updated_rows.append((
                    prod_id, model_code, brand_upper, prod_name, base_price, gender, cat_code, prod_img_url, origin_url, create_dt
                ))

        async with aiohttp.ClientSession() as session:
            tasks = [process_single_product(session, row) for row in staging_rows]
            await asyncio.gather(*tasks)
            
        logger.info(f"✨ 전체 {len(updated_rows)}개 상품 이미지 검증 및 Cloudinary 이관 완료.")

        # Staging Naver Price 데이터 가져오기
        if force:
            stage_cur.execute("""
                SELECT product_id, rank, naver_price, mall_name, mall_url, image_url, create_dt
                FROM staging_naver_prices
                WHERE UPPER(brand) = %s
            """, (brand_upper,))
        else:
            stage_cur.execute("""
                SELECT product_id, rank, naver_price, mall_name, mall_url, image_url, create_dt
                FROM staging_naver_prices
                WHERE UPPER(brand) = %s AND create_dt <= CURRENT_TIMESTAMP - INTERVAL '24 hours'
            """, (brand_upper,))
        naver_rows = stage_cur.fetchall()

        # Staging Embeddings 데이터 가져오기 (DW DB에 기저장된 벡터)
        if force:
            stage_cur.execute("""
                SELECT product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                FROM staging_product_embeddings
                WHERE brand = %s
            """, (brand_upper,))
        else:
            stage_cur.execute("""
                SELECT product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                FROM staging_product_embeddings
                WHERE brand = %s AND create_dt <= CURRENT_TIMESTAMP - INTERVAL '24 hours'
            """, (brand_upper,))
        embedding_rows = stage_cur.fetchall()

        # -------------------------------------------------------------
        # 트랜잭션 1: PROD DB 영구 적용 및 커밋 완료 플래그 확보
        # -------------------------------------------------------------
        logger.info("📡 PROD DB (Production) 연결 수립 및 트랜잭션 개시...")
        core_conn = get_prod_db_connection()
        core_conn.autocommit = False
        core_conn.set_session(autocommit=False)
        core_cur = core_conn.cursor()
        
        core_success_flag = False
        try:
            # 1) 기존 임베딩 데이터 DELETE (자식 레코드)
            core_cur.execute("DELETE FROM product_embeddings WHERE brand = %s", (brand_upper,))

            # 2) 기존 최저가 데이터 DELETE (자식 레코드)
            core_cur.execute("""
                DELETE FROM naver_prices 
                WHERE product_id IN (
                    SELECT product_id FROM products WHERE brand_name = %s
                )
            """, (brand_upper,))

            # 3) 기존 브랜드 상품 데이터 DELETE (부모 레코드)
            core_cur.execute("DELETE FROM products WHERE brand_name = %s", (brand_upper,))
            
            # 4) 신규 상품 데이터 INSERT (부모 레코드)
            for u_row in updated_rows:
                core_cur.execute("""
                    INSERT INTO products (
                        product_id, model_code, brand_name, prod_name, base_price, gender, 
                        category_code, img_url, origin_url, create_dt, update_dt
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                """, u_row)

            # 5) 신규 임베딩 데이터 INSERT (자식 레코드)
            if embedding_rows:
                for e_row in embedding_rows:
                    core_cur.execute("""
                        INSERT INTO product_embeddings (
                            product_id, image_vector, text_vector, brand, category, gender, image_path, create_dt
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, e_row)

            # 6) 신규 가격 비교 데이터 INSERT (자식 레코드)
            for n_row in naver_rows:
                core_cur.execute("""
                    INSERT INTO naver_prices (
                        product_id, rank, naver_price, mall_name, mall_url, image_url, create_dt
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, n_row)

            core_conn.commit()
            core_success_flag = True
            logger.info("✅ PROD DB 데이터 영구 반영(Commit) 성공!")
        except Exception as core_err:
            core_conn.rollback()
            raise RuntimeError(f"PROD DB 트랜잭션 반영 실패 (롤백 처리됨): {core_err}")
        finally:
            core_cur.close()
            core_conn.close()

        # -------------------------------------------------------------
        # 트랜잭션 2: 성공 플래그 기반 DW DB 데이터 역순 청소
        # -------------------------------------------------------------
        if core_success_flag:
            logger.info("🧹 DW DB 임시 데이터 청소 개시...")
            try:
                if force:
                    stage_cur.execute("DELETE FROM staging_products WHERE brand_name = %s", (brand_upper,))
                    stage_cur.execute("DELETE FROM staging_naver_prices WHERE UPPER(brand) = %s", (brand_upper,))
                    stage_cur.execute("DELETE FROM staging_product_embeddings WHERE UPPER(brand) = %s", (brand_upper,))
                else:
                    stage_cur.execute("DELETE FROM staging_products WHERE brand_name = %s AND create_dt <= CURRENT_TIMESTAMP - INTERVAL '24 hours'", (brand_upper,))
                    stage_cur.execute("DELETE FROM staging_naver_prices WHERE UPPER(brand) = %s AND create_dt <= CURRENT_TIMESTAMP - INTERVAL '24 hours'", (brand_upper,))
                    stage_cur.execute("DELETE FROM staging_product_embeddings WHERE UPPER(brand) = %s AND create_dt <= CURRENT_TIMESTAMP - INTERVAL '24 hours'", (brand_upper,))

                stage_conn.commit()
                
                # DW DB 내의 runs 에러 수 가져오기
                error_cnt = 0
                try:
                    err_conn = get_dw_db_connection()
                    err_cur = err_conn.cursor()
                    err_cur.execute("SELECT COUNT(*) FROM pipeline_errors WHERE run_id = %s", (run_id,))
                    error_cnt = err_cur.fetchone()[0]
                    err_cur.close()
                    err_conn.close()
                except Exception:
                    pass

                # -------------------------------------------------------------
                # [개선안] Cloudinary test/{brand} 폴더의 찌꺼기 이미지 자동 정리 (GC)
                # -------------------------------------------------------------
                try:
                    import cloudinary.api
                    test_folder = f"test/{brand_name.lower()}"
                    logger.info(f"🧹 Cloudinary '{test_folder}' 내의 미사용 찌꺼기 이미지 탐색 중...")
                    
                    # 1. Cloudinary test/{brand} 폴더의 실제 리소스 목록 획득
                    cloud_res = cloudinary.api.resources(type="upload", prefix=f"{test_folder}/", max_results=500)
                    cloud_pub_ids = [r["public_id"] for r in cloud_res.get("resources", [])]
                    
                    # 2. 현재 프로덕션 DB에 이관 성공하여 활성화 상태인 정상 이미지 public_id 목록 추출
                    active_pub_ids = set()
                    for u_row in updated_rows:
                        # u_row[7] 은 prod_img_url (https://...)
                        p_id = extract_public_id(u_row[7])
                        if p_id:
                            import urllib.parse
                            active_pub_ids.add(urllib.parse.unquote(p_id))
                    
                    # 3. Cloudinary 에는 있으나 DB 목록에는 없는 찌꺼기 식별 및 삭제
                    unused_pub_ids = [pid for pid in cloud_pub_ids if pid not in active_pub_ids]
                    if unused_pub_ids:
                        logger.info(f"🗑️ 사용하지 않는 구버전 이미지 {len(unused_pub_ids)}개 삭제 시도: {unused_pub_ids}")
                        cloudinary.api.delete_resources(unused_pub_ids)
                        logger.info("✅ 미사용 이미지 정리 완료")
                    else:
                        logger.info("✨ 정리할 미사용 구버전 이미지가 없습니다.")
                except Exception as gc_err:
                    logger.warning(f"⚠️ Cloudinary 찌꺼기 이미지 자동 정리 실패 (스킵됨): {gc_err}")

                # 파이프라인 구동 결과 성공 로그 기록
                # 스위칭의 경우 staging_count는 총 수집된 대상(total_items)이며, 이들은 모두 프로덕션에 업데이트/교체되므로
                # updated_items=staging_count 로 처리하고, 신규 유입 개념이 아니므로 new_items=0 으로 기록합니다.
                # 임베딩 생성 개수도 함께 기록합니다 (embedding_rows 의 개수).
                log_pipeline_end(run_id, "SUCCESS", total_items=staging_count, new_items=0, updated_items=staging_count, embed_count=len(embedding_rows), error_count=error_cnt)
                logger.info(f"✨ [{brand_name}] DW DB 클린업 및 이관 로그 갱신 완료!")
            except Exception as stage_err:
                stage_conn.rollback()
                crash_msg = f"⚠️ [CRITICAL] PROD DB는 성공했으나, DW DB 정리 단계에서 에러 발생: {stage_err}"
                logger.error(crash_msg)
                
                # 에러 로그 수집 테이블에 상세 로그 기록 시도 (커밋 롤백 복구를 위해 새 세션 수립)
                try:
                    err_conn = get_dw_db_connection()
                    err_cur = err_conn.cursor()
                    err_cur.execute("""
                        INSERT INTO pipeline_errors (
                            run_id, error_type, error_message, stack_trace, created_at
                        )
                        VALUES (%s, 'DW_CLEANUP_CRASH', %s, %s, CURRENT_TIMESTAMP)
                    """, (run_id, crash_msg, traceback.format_exc()))
                    err_conn.commit()
                    err_cur.close()
                    err_conn.close()
                    logger.info("✅ pipeline_errors 테이블에 크래시 리포트 기록 완료")
                except Exception as log_err:
                    logger.critical(f"❌ 크래시 리포트 기록 자체도 실패함: {log_err}")
                
                log_pipeline_end(run_id, "FAILED", total_items=staging_count)
                return False

        return True

    except Exception as e:
        stage_conn.rollback()
        error_msg = f"[{brand_name}] 지연 이관 프로세스 중 치명적인 오류로 롤백: {e}"
        logger.error(error_msg)
        send_alert(error_msg, level="CRITICAL")
        
        # 에러 리포트 작성
        try:
            err_conn = get_dw_db_connection()
            err_cur = err_conn.cursor()
            err_cur.execute("""
                INSERT INTO pipeline_errors (
                    run_id, error_type, error_message, stack_trace, created_at
                )
                VALUES (%s, 'PIPELINE_SWAP_ERROR', %s, %s, CURRENT_TIMESTAMP)
            """, (run_id, error_msg, traceback.format_exc()))
            err_conn.commit()
            err_cur.close()
            err_conn.close()
        except Exception as log_err:
            logger.critical(f"❌ 에러 리포트 기록 실패: {log_err}")
            
        log_pipeline_end(run_id, "FAILED", total_items=0)
        return False
    finally:
        stage_cur.close()
        stage_conn.close()

def clear_staging_data(brand_name: str) -> None:
    """새로운 배치 시작 전, 이전 찌꺼기가 남아있을 수 있으므로 해당 브랜드의 스테이징 데이터를 비웁니다."""
    conn = get_dw_db_connection()
    cur = conn.cursor()
    brand_upper = brand_name.upper()
    try:
        # 1) DB 데이터 삭제
        cur.execute("DELETE FROM staging_products WHERE brand_name = %s", (brand_upper,))
        cur.execute("DELETE FROM staging_naver_prices WHERE UPPER(brand) = %s", (brand_upper,))
        cur.execute("DELETE FROM staging_product_embeddings WHERE UPPER(brand) = %s", (brand_upper,))
        conn.commit()
        logger.info(f"🧹 [{brand_name}] DW DB 스테이징 클린업 완료 (상품, 최저가, 임베딩 전체 삭제)")
        
        # 2) Cloudinary staging 폴더의 이미지 삭제
        try:
            configure_cloudinary()
            import cloudinary.api
            # ENV_MODE에 따라 DEV/PROD Cloudinary 폴더 분리 삭제
            _env_prefix = "PROD" if os.getenv("ENV_MODE", "local").lower() in ["prod", "production"] else "DEV"
            folder_prefix = f"{_env_prefix}/staging/{brand_name.lower()}/"
            logger.info(f"☁️ [{brand_name}] Cloudinary Staging 이미지 삭제 시도: {folder_prefix}")
            cloudinary.api.delete_resources_by_prefix(folder_prefix)
            try:
                cloudinary.api.delete_folder(f"{_env_prefix}/staging/{brand_name.lower()}")
            except Exception:
                pass
            logger.info(f"☁️ [{brand_name}] Cloudinary Staging 이미지 삭제 완료")
        except Exception as cloud_err:
            logger.warning(f"⚠️ [{brand_name}] Cloudinary Staging 이미지 삭제 실패: {cloud_err}")
            
    except Exception as e:
        conn.rollback()
        logger.warning(f"⚠️ DW DB 스테이징 클린업 중 예외 발생: {e}")
    finally:
        cur.close()
        conn.close()