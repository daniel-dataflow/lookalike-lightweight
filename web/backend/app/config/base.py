"""
애플리케이션 설정 - 환경변수 기반 (Supabase + 외부 ML API)
ENV_MODE=local  -> 로컬 PostgreSQL(5433)
ENV_MODE=production -> Supabase DB URL
"""
from pydantic import model_validator
from pydantic_settings import BaseSettings
from functools import lru_cache
import os


class Settings(BaseSettings):
    """환경변수에서 설정을 로드"""

    # === 앱 ===
    APP_TITLE: str = "Lookalike"
    APP_VERSION: str = "2.0.0"
    ENV_MODE: str = "production"   # "local" | "production"

    # === Cloudflare R2 스토리지 ===
    CF_R2_ACCESS_KEY: str = ""
    CF_R2_SECRET_KEY: str = ""
    CF_R2_ENDPOINT: str = ""
    CF_R2_PUBLIC_URL: str = ""
    CF_R2_BUCKET: str = "lookalike-assets"

    # === Supabase (Production) ===
    SUPABASE_DB_URL: str = ""      # postgresql://...@db.xxx.supabase.co:5432/postgres

    # === PostgreSQL (Local, 포트 5433) ===
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5433
    POSTGRES_DB: str = "datadb"
    POSTGRES_USER: str = "datauser"
    POSTGRES_PASSWORD: str = "DataPass2026!"

    # === OAuth2 소셜 로그인 ===
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    NAVER_CLIENT_ID: str = ""
    NAVER_CLIENT_SECRET: str = ""
    KAKAO_CLIENT_ID: str = ""
    KAKAO_CLIENT_SECRET: str = ""
    X_NAVER_CLIENT_ID: str = ""
    X_NAVER_CLIENT_SECRET: str = ""

    # === 세션 및 관리자 ===
    SESSION_SECRET_KEY: str = "change-this-in-production"
    SESSION_EXPIRE_HOURS: int = 24
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin1234!"

    # === 외부 ML API ===
    HF_TOKEN: str = ""
    HF_SPACE_URL: str = ""         # https://<user>-<space>.hf.space
    HF_SPACE_TOKEN: str = ""
    HF_CLIP_MODEL: str = "openai/clip-vit-base-patch32"
    GEMINI_API_KEY: str = ""
    GEMINI_EMBED_MODEL: str = "text-embedding-004"

    # === 이미지 업로드 ===
    MAX_UPLOAD_SIZE_MB: int = 10
    THUMBNAIL_SIZE: int = 150
    THUMBNAIL_QUALITY: int = 85

    # === DB 커넥션 풀 ===
    POSTGRES_MIN_CONN: int = 2
    POSTGRES_MAX_CONN: int = 10

    DATABASE_URL: str = ""

    @model_validator(mode="after")
    def compute_database_url(self) -> "Settings":
        # 1. 환경 변수나 .env에 명시적으로 외부/원격 DB(Neon 등)의 DATABASE_URL이 설정되어 있다면 최우선적으로 사용합니다.
        if self.DATABASE_URL:
            is_local = any(h in self.DATABASE_URL for h in ["localhost", "127.0.0.1"])
            if not is_local:
                return self

        # 2. 로컬 및 도커 컨테이너 환경의 기본값 생성
        mode = self.ENV_MODE.lower()
        if mode == "local":
            self.DATABASE_URL = (
                f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
                f"@localhost:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )
        elif mode == "docker":
            self.DATABASE_URL = (
                f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
                f"@{self.POSTGRES_HOST}:5432/{self.POSTGRES_DB}"
            )
        else:
            # 프로덕션 모드에서는 시스템 환경 변수(DATABASE_URL)를 최우선으로 하며, 없으면 SUPABASE_DB_URL을 사용합니다.
            if not self.DATABASE_URL:
                self.DATABASE_URL = self.SUPABASE_DB_URL
        return self

    def is_oauth_configured(self, provider: str) -> bool:
        if provider == "google":
            return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)
        if provider == "naver":
            return bool(self.NAVER_CLIENT_ID and self.NAVER_CLIENT_SECRET)
        if provider == "kakao":
            return bool(self.KAKAO_CLIENT_ID and self.KAKAO_CLIENT_SECRET)
        return False

    class Config:
        # 프로젝트 루트 .env 참조 (web/backend/app/config/base.py 에서 4단계 위)
        env_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "..", ".env"
        )
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
