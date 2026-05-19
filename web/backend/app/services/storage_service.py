"""
Cloudflare R2 / 로컬 통합 스토리지 서비스

동작 모드 (자동 감지):
  1. 로컬 모드 (ENV_MODE=local) 또는 R2 키 누락
     -> web/frontend/static/uploads/ 에 WebP 저장
     -> URL: /static/uploads/<folder>/<id>.webp
  2. 프로덕션 모드 + R2 키 완비
     -> Cloudflare R2 버킷에 업로드
     -> URL: CF_R2_PUBLIC_URL/<folder>/<id>.webp
"""
import logging
import io
import os
import uuid

from PIL import Image
from fastapi import UploadFile

from ..config import get_settings

logger = logging.getLogger(__name__)


class StorageService:
    def __init__(self):
        self._settings = get_settings()
        # web/backend/app/services -> web/frontend/static/uploads
        self._local_upload_base = os.path.normpath(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "..", "frontend", "static", "uploads"
            )
        )

    # ──────────────────────────────────────
    # 모드 판별
    # ──────────────────────────────────────
    def _is_local_mode(self) -> bool:
        if self._settings.ENV_MODE.lower() == "local":
            return True
        r2_ready = all([
            self._settings.CF_R2_ACCESS_KEY,
            self._settings.CF_R2_SECRET_KEY,
            self._settings.CF_R2_ENDPOINT,
        ])
        if not r2_ready:
            logger.warning("R2 설정 누락 -> 로컬 스토리지 fallback")
            return True
        return False

    def _get_s3_client(self):
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=self._settings.CF_R2_ENDPOINT,
            aws_access_key_id=self._settings.CF_R2_ACCESS_KEY,
            aws_secret_access_key=self._settings.CF_R2_SECRET_KEY,
            region_name="auto",
        )

    # ──────────────────────────────────────
    # 유효성 검사
    # ──────────────────────────────────────
    def validate_image_file(self, file: UploadFile):
        if not file.content_type or not file.content_type.startswith("image/"):
            raise ValueError("이미지 파일만 업로드 가능합니다")
        max_bytes = self._settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        if file.size and file.size > max_bytes:
            raise ValueError(f"이미지 크기가 {self._settings.MAX_UPLOAD_SIZE_MB}MB를 초과합니다")

    # ──────────────────────────────────────
    # 핵심: 압축 & 업로드
    # ──────────────────────────────────────
    async def compress_and_upload(
        self,
        image_bytes: bytes,
        folder: str = "search",
        filename: str | None = None,
        quality: int = 85,
        max_width: int = 800,
    ) -> dict:
        image_id = filename or uuid.uuid4().hex

        # 1. 이미지 리사이즈 + WebP 압축
        img = Image.open(io.BytesIO(image_bytes))
        orig_w, orig_h = img.size
        if orig_w > max_width:
            ratio = max_width / orig_w
            img = img.resize((max_width, int(orig_h * ratio)), Image.LANCZOS)
        width, height = img.size

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="WEBP", quality=quality)
        compressed = buf.getvalue()
        file_size = len(compressed)

        storage_path = f"{folder}/{image_id}.webp"

        # 2. 저장 분기
        if self._is_local_mode():
            url = self._save_local(storage_path, compressed)
        else:
            url = self._upload_r2(storage_path, compressed)

        return {
            "image_id": image_id,
            "url": url,
            "storage_path": storage_path,
            "file_size": file_size,
            "width": width,
            "height": height,
        }

    def _save_local(self, storage_path: str, data: bytes) -> str:
        target = os.path.join(self._local_upload_base, storage_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
        url = f"/static/uploads/{storage_path}"
        logger.info(f"[로컬 스토리지] 저장 완료: {target}")
        return url

    def _upload_r2(self, storage_path: str, data: bytes) -> str:
        s3 = self._get_s3_client()
        bucket = self._settings.CF_R2_BUCKET
        s3.put_object(
            Bucket=bucket,
            Key=storage_path,
            Body=data,
            ContentType="image/webp",
        )
        base_url = self._settings.CF_R2_PUBLIC_URL or self._settings.CF_R2_ENDPOINT
        url = f"{base_url.rstrip('/')}/{storage_path}"
        logger.info(f"[R2 스토리지] 업로드 완료: {url}")
        return url

    # ──────────────────────────────────────
    # 썸네일 생성 (프로필 등)
    # ──────────────────────────────────────
    async def create_thumbnail(self, image_bytes: bytes, user_id: str) -> dict:
        thumb_id = uuid.uuid4().hex[:12]
        result = await self.compress_and_upload(
            image_bytes=image_bytes,
            folder=f"thumbnails/{user_id}",
            filename=thumb_id,
            quality=self._settings.THUMBNAIL_QUALITY,
            max_width=self._settings.THUMBNAIL_SIZE,
        )
        return {
            "thumbnail_url": result["url"],
            "file_size": result["file_size"],
            "width": result["width"],
            "height": result["height"],
        }


storage_service = StorageService()
