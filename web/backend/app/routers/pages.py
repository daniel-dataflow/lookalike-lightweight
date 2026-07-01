"""
Jinja2 기반 프론트엔드 웹페이지 라우터
(로컬 테스트 및 레거시 프론트엔드 호환용)
"""
import os
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..database import get_session
from .product import get_product_detail

router = APIRouter(tags=["웹 페이지"])

# pages.py 위치: web/backend/app/routers/pages.py → web/frontend/templates
_TEMPLATES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "frontend", "templates")
)
from ..config import get_settings
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.globals["settings"] = get_settings()

def _get_session(request: Request) -> dict | None:
    token = request.cookies.get("session_token")
    if not token:
        return None
    return get_session(token, is_admin=False)

def _get_admin_session(request: Request) -> dict | None:
    token = request.cookies.get("admin_session_token")
    if not token:
        return None
    return get_session(token, is_admin=True)

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = ""):
    return templates.TemplateResponse(request=request, name="search_results.html", context={"request": request, "query": q})

@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail_page(request: Request, product_id: str):
    try:
        # FastAPI 백엔드의 get_product_detail을 직접 호출하여 데이터 획득
        data = await get_product_detail(product_id)
        product_dict = data.product.model_dump()
        
        # 템플릿 호환성을 위해 변수 조정
        product_dict["img_hdfs_path"] = product_dict.get("img_url", "")
        product_dict["local_url"] = product_dict.get("local_url", "")
        
        prices_list = [p.model_dump() for p in data.naver_prices]
        base_price = product_dict.get("base_price", 0)
        for p in prices_list:
            discount = base_price - p.get("naver_price", 0)
            p["discount"] = discount
            p["discount_rate"] = int((discount / base_price) * 100) if base_price > 0 else 0
            
        return templates.TemplateResponse(request=request, name="product_detail.html", context={
            "request": request, 
            "product": product_dict,
            "prices": prices_list
        })
    except HTTPException as e:
        return templates.TemplateResponse(request=request, name="error.html", context={"request": request, "error": e.detail}, status_code=e.status_code)
    except Exception as e:
        return templates.TemplateResponse(request=request, name="error.html", context={"request": request, "error": "서버 오류가 발생했습니다"}, status_code=500)

@router.get("/mypage", response_class=HTMLResponse)
async def mypage(request: Request):
    session = _get_session(request)
    if not session:
        return RedirectResponse(url="/?error=login_required", status_code=302)
    request.state.user = session
    return templates.TemplateResponse(request=request, name="mypage.html", context={"request": request})

@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    session = _get_admin_session(request)
    if session and session.get("is_admin"):
        return RedirectResponse(url="/admin/infra", status_code=302)
    return templates.TemplateResponse(request=request, name="admin_login.html", context={"request": request})

def _check_page_permission(request: Request, required_permission: str):
    """특정 어드민 페이지 접근 권한 검증 및 세션 반환 헬퍼"""
    session = _get_admin_session(request)
    if not session or not session.get("is_admin"):
        return False, RedirectResponse(url="/admin/login", status_code=302)
        
    perm = session.get("admin_permission", "")
    
    # 1. SUPER_ADMIN은 모든 권한 통과
    if perm == "SUPER_ADMIN":
        return True, session
        
    # 2. 사용자 관리(users) 페이지는 최고 관리자(SUPER_ADMIN)만 진입 가능
    if required_permission == "users":
        return False, templates.TemplateResponse(
            request=request, 
            name="admin_forbidden.html", 
            context={"request": request, "required_menu": required_permission}
        )
        
    # 3. 그 외 페이지는 쉼표로 분리된 문자열 대조
    perms = [p.strip() for p in perm.split(",") if p.strip()]
    if required_permission in perms:
        return True, session
        
    # 4. 권한 없음 페이지 렌더링
    return False, templates.TemplateResponse(
        request=request, 
        name="admin_forbidden.html", 
        context={"request": request, "required_menu": required_permission}
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request):
    session = _get_admin_session(request)
    if not session or not session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return RedirectResponse(url="/admin/infra", status_code=302)


@router.get("/admin/infra", response_class=HTMLResponse)
async def admin_infra(request: Request):
    ok, res = _check_page_permission(request, "infra")
    if not ok:
        return res
    return templates.TemplateResponse(request=request, name="admin_infra.html", context={
        "request": request,
        "admin_permissions": res.get("admin_permission", ""),
        "admin_username": res.get("user_name") or res.get("user_id") or "Admin"
    })


@router.get("/admin/stats", response_class=HTMLResponse)
async def admin_stats(request: Request):
    ok, res = _check_page_permission(request, "stats")
    if not ok:
        return res
    return templates.TemplateResponse(request=request, name="admin_dashboard.html", context={
        "request": request,
        "admin_permissions": res.get("admin_permission", ""),
        "admin_username": res.get("user_name") or res.get("user_id") or "Admin"
    })


@router.get("/inquiry", response_class=HTMLResponse)
async def inquiry_page(request: Request):
    return templates.TemplateResponse(request=request, name="inquiry.html", context={"request": request})


@router.get("/recent", response_class=HTMLResponse)
async def recent_viewed(request: Request):
    session = _get_session(request)
    if not session:
        return RedirectResponse(url="/?error=login_required", status_code=302)
    return templates.TemplateResponse(request=request, name="recent.html", context={"request": request})


@router.get("/likes", response_class=HTMLResponse)
async def likes(request: Request):
    session = _get_session(request)
    if not session:
        return RedirectResponse(url="/?error=login_required", status_code=302)
    return templates.TemplateResponse(request=request, name="likes.html", context={"request": request})


@router.get("/search-history", response_class=HTMLResponse)
async def search_history(request: Request):
    return templates.TemplateResponse(request=request, name="search_history.html", context={"request": request})


@router.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse(request=request, name="terms.html", context={"request": request})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse(request=request, name="privacy.html", context={"request": request})


@router.get("/teams_history", response_class=HTMLResponse)
async def teams_history_page(request: Request):
    return templates.TemplateResponse(request=request, name="teams_history.html", context={"request": request})


@router.get("/teams_renewal", response_class=HTMLResponse)
async def teams_renewal_page(request: Request):
    return templates.TemplateResponse(request=request, name="teams_renewal.html", context={"request": request})


@router.get("/teams", response_class=HTMLResponse)
async def teams_page(request: Request):
    return templates.TemplateResponse(request=request, name="teams.html", context={"request": request})


@router.get("/admin/batch", response_class=HTMLResponse)
async def admin_batch(request: Request):
    ok, res = _check_page_permission(request, "batch")
    if not ok:
        return res
    return templates.TemplateResponse(request=request, name="admin_batch.html", context={
        "request": request,
        "admin_permissions": res.get("admin_permission", ""),
        "admin_username": res.get("user_name") or res.get("user_id") or "Admin"
    })


@router.get("/admin/inquiry", response_class=HTMLResponse)
async def admin_inquiry(request: Request):
    ok, res = _check_page_permission(request, "inquiry")
    if not ok:
        return res
    return templates.TemplateResponse(request=request, name="admin_inquiry.html", context={
        "request": request,
        "admin_permissions": res.get("admin_permission", ""),
        "admin_username": res.get("user_name") or res.get("user_id") or "Admin"
    })


@router.get("/admin/crawling", response_class=HTMLResponse)
async def admin_crawling(request: Request):
    """크롤링 파이프라인 모니터링 — 스테이징 현황 및 수동 스위칭 페이지"""
    ok, res = _check_page_permission(request, "crawling")
    if not ok:
        return res
    return templates.TemplateResponse(request=request, name="admin_crawling.html", context={
        "request": request,
        "admin_permissions": res.get("admin_permission", ""),
        "admin_username": res.get("user_name") or res.get("user_id") or "Admin"
    })


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    """사용자 관리 및 통제 대시보드 페이지"""
    ok, res = _check_page_permission(request, "users")
    if not ok:
        return res
    return templates.TemplateResponse(request=request, name="admin_users.html", context={
        "request": request,
        "admin_permissions": res.get("admin_permission", ""),
        "admin_username": res.get("user_name") or res.get("user_id") or "Admin"
    })


@router.get("/admin/logs", response_class=HTMLResponse)
async def admin_logs(request: Request):
    ok, res = _check_page_permission(request, "logs")
    if not ok:
        return res
    return templates.TemplateResponse(request=request, name="admin_logs.html", context={
        "request": request,
        "admin_permissions": res.get("admin_permission", ""),
        "admin_username": res.get("user_name") or res.get("user_id") or "Admin"
    })


@router.get("/admin/visitors", response_class=HTMLResponse)
async def admin_visitors(request: Request, owner: bool = False):
    """방문자 분석 대시보드 페이지"""
    ok, res = _check_page_permission(request, "visitors")
    if not ok:
        return res
        
    # ?owner=true가 파라미터로 제공될 경우 접속자의 IP를 OWNER IP로 자동 등록
    if owner and request.client:
        ip = request.client.host
        if ip:
            try:
                if ip in ["127.0.0.1", "localhost", "::1"]:
                    memo = "Localhost (Parameter)"
                else:
                    memo = "Parameter-Registered"
                with get_session(request.cookies.get("admin_session_token"), is_admin=True) as dummy: # DB 세션 획득용으로만 씀
                    pass
                from ..database import get_pg_cursor
                with get_pg_cursor() as cur:
                    cur.execute("""
                        INSERT INTO owner_ips (ip_address, memo)
                        VALUES (%s, %s)
                        ON CONFLICT (ip_address) DO UPDATE
                        SET memo = EXCLUDED.memo;
                    """, (ip, memo))
                # 메모리 캐시에도 추가
                from .admin import _admin_ips
                _admin_ips.add(ip)
            except Exception as e:
                pass
                
    return templates.TemplateResponse(request=request, name="admin_visitors.html", context={
        "request": request,
        "admin_permissions": res.get("admin_permission", ""),
        "admin_username": res.get("user_name") or res.get("user_id") or "Admin"
    })


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    """비밀번호 분실 찾기 페이지"""
    return templates.TemplateResponse(request=request, name="forgot_password.html", context={"request": request})




