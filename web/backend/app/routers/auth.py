"""
사용자 세션 관리 및 인증(소셜 OAuth2, 이메일)을 총괄하는 핵심 라우터 모듈.
- Redis 기반 → DB(user_sessions) 기반 Stateful 세션으로 전환
"""
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from datetime import datetime
import uuid
import json
import logging

import httpx
import bcrypt

from ..database import get_pg_cursor, create_session, get_session, delete_session
from ..models.user import (
    UserRegisterRequest,
    UserLoginRequest,
    AdminLoginRequest,
    UserUpdateRequest,
    UserResponse,
    LoginResponse,
    OAuthConfigResponse,
)
from ..config import get_settings
from ..config.auth import OAUTH_CONFIGS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["인증"])

# ──────────────────────────────────────
# 비밀번호 해싱 (bcrypt 직접 사용)
# ──────────────────────────────────────


def _hash_password(password: str) -> str:
    """bcrypt 비밀번호 해싱"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    """bcrypt 비밀번호 검증"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ──────────────────────────────────────
# DB 기반 세션 관리 (Redis → user_sessions 테이블)
# ──────────────────────────────────────
def _create_session(response: Response, user_data: dict) -> str:
    """DB 기반 세션 생성 + 쿠키 설정"""
    settings = get_settings()
    token = create_session(user_data, is_admin=False)

    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        max_age=settings.SESSION_EXPIRE_HOURS * 3600,
        samesite="lax",
        secure=False,
        path="/",
    )
    return token


def _get_session(request: Request) -> dict | None:
    """쿠키 토큰으로 DB에서 세션 조회"""
    token = request.cookies.get("session_token")
    if not token:
        return None
    return get_session(token, is_admin=False)


def _delete_session(request: Request, response: Response):
    """DB에서 세션 삭제 + 쿠키 제거"""
    token = request.cookies.get("session_token")
    if token:
        delete_session(token)
    response.delete_cookie(key="session_token", path="/")


# ──────────────────────────────────────
# Admin 전용 DB 세션 관리
# ──────────────────────────────────────
def _create_admin_session(response: Response, user_data: dict) -> str:
    """어드민 DB 세션 생성"""
    settings = get_settings()
    token = create_session(user_data, is_admin=True)

    response.set_cookie(
        key="admin_session_token",
        value=token,
        httponly=True,
        max_age=settings.SESSION_EXPIRE_HOURS * 3600,
        samesite="lax",
        secure=False,
        path="/",
    )
    return token


def _get_admin_session(request: Request) -> dict | None:
    """어드민 세션 조회"""
    token = request.cookies.get("admin_session_token")
    if not token:
        return None
    return get_session(token, is_admin=True)


def _delete_admin_session(request: Request, response: Response):
    """어드민 세션 삭제"""
    token = request.cookies.get("admin_session_token")
    if token:
        delete_session(token)
    response.delete_cookie(key="admin_session_token", path="/")


def _user_row_to_dict(row: dict) -> dict:
    """DB 행을 세션 저장용 딕셔너리로 변환"""
    return {
        "user_id": row["user_id"],
        "name": row.get("name"),
        "email": row.get("email"),
        "role": row.get("role", "USER"),
        "provider": row.get("provider", "email"),
        "profile_image": row.get("profile_image"),
    }


# ──────────────────────────────────────
# 이메일 회원가입
# ──────────────────────────────────────
@router.post("/register", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
async def register(req: UserRegisterRequest, response: Response):
    """네이티브 이메일 기반 회원가입"""
    if req.password != req.password_confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="비밀번호가 일치하지 않습니다",
        )

    try:
        email_prefix = req.email.split("@")[0]
        user_id = f"{email_prefix}_{uuid.uuid4().hex[:4]}"

        with get_pg_cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE email = %s", (req.email,))
            if cur.fetchone():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="이미 사용 중인 이메일입니다",
                )

            cur.execute(
                """
                INSERT INTO users (user_id, password, user_name, email, provider)
                VALUES (%s, %s, %s, %s, 'email')
                RETURNING user_id, user_name as name, email, role, provider, profile_image, create_dt
                """,
                (user_id, _hash_password(req.password), req.name, req.email),
            )
            row = cur.fetchone()

        user_data = _user_row_to_dict(row)
        _create_session(response, user_data)

        return LoginResponse(
            success=True,
            message="회원가입이 완료되었습니다",
            user=UserResponse(**row),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"회원가입 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류가 발생했습니다")


# ──────────────────────────────────────
# 이메일 로그인
# ──────────────────────────────────────
@router.post("/login", response_model=LoginResponse)
async def login(req: UserLoginRequest, response: Response):
    """이메일/비밀번호 로그인"""
    try:
        with get_pg_cursor() as cur:
            cur.execute(
                """
                SELECT user_id, user_name as name, email, role, provider, profile_image,
                       last_login, create_dt, password
                FROM users WHERE email = %s AND provider = 'email'
                """,
                (req.email,),
            )
            row = cur.fetchone()

            if not row:
                return LoginResponse(success=False, message="존재하지 않는 이메일입니다")

            if not row["password"] or not _verify_password(req.password, row["password"]):
                return LoginResponse(success=False, message="비밀번호가 일치하지 않습니다")

            cur.execute(
                "UPDATE users SET last_login = NOW() WHERE user_id = %s",
                (row["user_id"],),
            )

        user_data = _user_row_to_dict(row)
        _create_session(response, user_data)

        return LoginResponse(
            success=True,
            message="로그인 성공",
            user=UserResponse(**{k: v for k, v in row.items() if k != "password"}),
        )

    except Exception as e:
        logger.error(f"로그인 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류가 발생했습니다")


# ──────────────────────────────────────
# 로그아웃
# ──────────────────────────────────────
@router.post("/logout")
async def logout(request: Request, response: Response):
    """로그아웃 (DB 세션 삭제)"""
    _delete_session(request, response)
    return {"success": True, "message": "로그아웃 되었습니다"}


# ──────────────────────────────────────
# 어드민 로그인 / 로그아웃
# ──────────────────────────────────────
@router.post("/admin/login")
async def admin_login(req: AdminLoginRequest, response: Response):
    """관리자 전용 로그인"""
    settings = get_settings()
    if req.username != settings.ADMIN_USERNAME or req.password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="관리자 아이디 또는 비밀번호가 일치하지 않습니다")

    # 세션 생성 시 user_sessions.user_id FK 제약 조건을 만족하기 위해 admin 계정을 users 테이블에 보장
    try:
        with get_pg_cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, user_name, email, role, provider)
                VALUES ('admin', '시스템 관리자', 'admin@lookalike.com', 'ADMIN', 'system')
                ON CONFLICT (user_id) DO NOTHING
            """)
    except Exception as e:
        logger.error(f"어드민 계정 보장 실패: {e}")

    user_data = {
        "user_id": "admin",
        "name": "시스템 관리자",
        "email": "admin@lookalike.com",
        "role": "ADMIN",
        "provider": "system",
        "profile_image": "",
        "is_admin": True,
    }
    _create_admin_session(response, user_data)
    return {"success": True, "message": "관리자 접속 성공"}


@router.post("/admin/logout")
async def admin_logout(request: Request, response: Response):
    """관리자 전용 로그아웃"""
    _delete_admin_session(request, response)
    return {"success": True, "message": "관리자 로그아웃 되었습니다"}


# ──────────────────────────────────────
# 현재 로그인 사용자 정보
# ──────────────────────────────────────
@router.get("/me", response_model=LoginResponse)
async def get_current_user(request: Request):
    """현재 로그인한 사용자 정보 (DB 세션에서 반환)"""
    session = _get_session(request)
    if not session:
        return LoginResponse(success=False, message="로그인이 필요합니다")

    return LoginResponse(
        success=True,
        message="인증된 사용자",
        user=UserResponse(
            user_id=session.get("user_id", ""),
            name=session.get("name"),
            email=session.get("email"),
            role=session.get("role", "USER"),
            provider=session.get("provider", "email"),
            profile_image=session.get("profile_image"),
            last_login=None,
            create_dt=None,
        ),
    )


# ──────────────────────────────────────
# OAuth 제공자 활성화 상태
# ──────────────────────────────────────
@router.get("/oauth/providers", response_model=OAuthConfigResponse)
async def get_oauth_providers():
    """활성화된 OAuth 제공자 목록 조회"""
    settings = get_settings()
    return OAuthConfigResponse(
        google=settings.is_oauth_configured("google"),
        naver=settings.is_oauth_configured("naver"),
        kakao=settings.is_oauth_configured("kakao"),
    )


# ──────────────────────────────────────
# OAuth2: 소셜 로그인 시작 (→ 제공자로 리다이렉트)
# ──────────────────────────────────────
@router.get("/oauth/{provider}")
async def oauth_login(provider: str, request: Request):
    """소셜 인증 시작: 사용자를 OAuth 제공자 화면으로 리다이렉트"""
    settings = get_settings()

    if provider not in OAUTH_CONFIGS:
        raise HTTPException(status_code=400, detail="지원하지 않는 OAuth 제공자입니다")

    if not settings.is_oauth_configured(provider):
        raise HTTPException(status_code=400, detail=f"{provider} 로그인이 설정되지 않았습니다")

    config = OAUTH_CONFIGS[provider]

    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.hostname))
    redirect_uri = f"{scheme}://{host}/api/auth/oauth/{provider}/callback"

    client_id = getattr(settings, f"{provider.upper()}_CLIENT_ID")

    # state (CSRF 방지) — DB에 저장
    state = uuid.uuid4().hex
    try:
        with get_pg_cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_sessions (token, user_id, session_data, is_admin, expires_at)
                VALUES (%s, %s, %s, FALSE, NOW() + INTERVAL '10 minutes')
                """,
                (f"oauth_{state}", "oauth", json.dumps({"provider": provider})),
            )
    except Exception:
        pass

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }

    if config["scope"]:
        params["scope"] = config["scope"]

    if provider == "naver":
        params["response_type"] = "code"

    query = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url=f"{config['auth_url']}?{query}")


# ──────────────────────────────────────
# OAuth2: 콜백 처리
# ──────────────────────────────────────
@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str = None,
    state: str = "",
    error: str = None,
    error_description: str = None,
    response: Response = None,
):
    """OAuth 인가 코드 → 액세스 토큰 교환 → 사용자 프로필 → DB Upsert"""
    settings = get_settings()

    if error or not code:
        logger.warning(f"OAuth 콜백 에러 ({provider}): error={error}, desc={error_description}")
        return RedirectResponse(url="/?error=oauth_failed")

    if provider not in OAUTH_CONFIGS:
        raise HTTPException(status_code=400, detail="지원하지 않는 OAuth 제공자입니다")

    config = OAUTH_CONFIGS[provider]

    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.hostname))
    redirect_uri = f"{scheme}://{host}/api/auth/oauth/{provider}/callback"

    client_id = getattr(settings, f"{provider.upper()}_CLIENT_ID")
    client_secret = getattr(settings, f"{provider.upper()}_CLIENT_SECRET")

    # 1. 인가 코드 → 액세스 토큰 교환
    token_data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(config["token_url"], data=token_data)

        if token_resp.status_code != 200:
            logger.error(f"OAuth 토큰 교환 실패 ({provider}): {token_resp.text}")
            return RedirectResponse(url="/?error=oauth_failed")

        token_json = token_resp.json()
        access_token = token_json.get("access_token")

        if not access_token:
            logger.error(f"액세스 토큰 없음 ({provider}): {token_json}")
            return RedirectResponse(url="/?error=oauth_failed")

        # 2. 액세스 토큰 → 사용자 정보 조회
        headers = {"Authorization": f"Bearer {access_token}"}
        userinfo_resp = await client.get(config["userinfo_url"], headers=headers)

        if userinfo_resp.status_code != 200:
            logger.error(f"사용자 정보 조회 실패 ({provider}): {userinfo_resp.text}")
            return RedirectResponse(url="/?error=oauth_failed")

        userinfo = userinfo_resp.json()

    # 3. 제공자별 프로필 파싱
    provider_id, name, email, profile_image = _parse_oauth_userinfo(provider, userinfo)

    if not provider_id:
        logger.error(f"소셜 ID 추출 실패 ({provider}): {userinfo}")
        return RedirectResponse(url="/?error=oauth_failed")

    # 4. DB Upsert
    try:
        with get_pg_cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE provider = %s AND provider_id = %s",
                (provider, provider_id),
            )
            existing = cur.fetchone()

            if existing:
                cur.execute(
                    """
                    UPDATE users SET last_login = NOW(), user_name = COALESCE(%s, user_name),
                           profile_image = COALESCE(%s, profile_image)
                    WHERE provider = %s AND provider_id = %s
                    RETURNING user_id, user_name as name, email, role, provider, profile_image, create_dt
                    """,
                    (name, profile_image, provider, provider_id),
                )
                row = cur.fetchone()
            else:
                user_id = f"{provider}_{uuid.uuid4().hex[:8]}"
                cur.execute(
                    """
                    INSERT INTO users (user_id, user_name, email, provider, provider_id, profile_image)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING user_id, user_name as name, email, role, provider, profile_image, create_dt
                    """,
                    (user_id, name, email, provider, provider_id, profile_image),
                )
                row = cur.fetchone()

        user_data = _user_row_to_dict(row)
        redirect = RedirectResponse(url="/", status_code=302)
        _create_session(redirect, user_data)
        return redirect

    except Exception as e:
        logger.error(f"OAuth 사용자 처리 실패 ({provider}): {e}")
        return RedirectResponse(url="/?error=oauth_failed")


def _parse_oauth_userinfo(provider: str, info: dict) -> tuple:
    """각 소셜 플랫폼별 프로필 JSON 정규화"""
    if provider == "google":
        return (
            info.get("id"),
            info.get("name"),
            info.get("email"),
            info.get("picture"),
        )
    elif provider == "naver":
        resp = info.get("response", {})
        return (
            resp.get("id"),
            resp.get("name") or resp.get("nickname"),
            resp.get("email"),
            resp.get("profile_image"),
        )
    elif provider == "kakao":
        kakao_account = info.get("kakao_account", {})
        profile = kakao_account.get("profile", {})
        return (
            str(info.get("id")),
            profile.get("nickname"),
            kakao_account.get("email"),
            profile.get("profile_image_url"),
        )
    return (None, None, None, None)


# ──────────────────────────────────────
# 사용자 정보 조회
# ──────────────────────────────────────
@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: str):
    """사용자 상세 정보 조회"""
    try:
        with get_pg_cursor() as cur:
            cur.execute(
                """
                SELECT user_id, user_name as name, email, role, provider, profile_image,
                       last_login, create_dt
                FROM users WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

        return UserResponse(**row)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 조회 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")


# ──────────────────────────────────────
# 사용자 정보 수정
# ──────────────────────────────────────
@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(user_id: str, req: UserUpdateRequest, request: Request, response: Response):
    """비밀번호 변경 (네이티브 로그인 사용자 전용)"""
    session = _get_session(request)
    if not session or session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="권한이 없습니다")

    try:
        with get_pg_cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            current_user = cur.fetchone()
            if not current_user:
                raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

            if current_user["provider"] != "email":
                raise HTTPException(status_code=403, detail="소셜 로그인 사용자는 회원정보를 수정할 수 없습니다")

            updates = []
            values = []

            if req.new_password:
                if not req.current_password:
                    raise HTTPException(status_code=400, detail="현재 비밀번호를 입력해주세요")

                if not _verify_password(req.current_password, current_user["password"]):
                    raise HTTPException(status_code=400, detail="현재 비밀번호가 일치하지 않습니다")

                updates.append("password = %s")
                values.append(_hash_password(req.new_password))

            if not updates:
                raise HTTPException(status_code=400, detail="수정할 내용이 없습니다")

            updates.append("update_dt = NOW()")
            values.append(user_id)

            cur.execute(
                f"""
                UPDATE users SET {', '.join(updates)}
                WHERE user_id = %s
                RETURNING user_id, user_name as name, email, role, provider, profile_image,
                          last_login, create_dt
                """,
                values,
            )
            row = cur.fetchone()

            _delete_session(request, response)

        return UserResponse(**row)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"사용자 수정 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")


# ──────────────────────────────────────
# 회원 탈퇴
# ──────────────────────────────────────
@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request, response: Response):
    """회원 탈퇴 (DB 삭제 + 로그아웃)"""
    session = _get_session(request)
    if not session or session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="권한이 없습니다")

    try:
        with get_pg_cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

        _delete_session(request, response)

        return {"success": True, "message": "회원 탈퇴가 완료되었습니다"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"회원 탈퇴 실패: {e}")
        raise HTTPException(status_code=500, detail="서버 오류")
