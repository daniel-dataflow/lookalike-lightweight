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
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

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
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = ""):
    return templates.TemplateResponse("search_results.html", {"request": request, "query": q})

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
            
        return templates.TemplateResponse("product_detail.html", {
            "request": request, 
            "product": product_dict,
            "prices": prices_list
        })
    except HTTPException as e:
        return templates.TemplateResponse("error.html", {"request": request, "error": e.detail}, status_code=e.status_code)
    except Exception as e:
        return templates.TemplateResponse("error.html", {"request": request, "error": "서버 오류가 발생했습니다"}, status_code=500)

@router.get("/mypage", response_class=HTMLResponse)
async def mypage(request: Request):
    session = _get_session(request)
    if not session:
        return RedirectResponse(url="/?error=login_required", status_code=302)
    request.state.user = session
    return templates.TemplateResponse("mypage.html", {"request": request})

@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    session = _get_admin_session(request)
    if session and session.get("is_admin"):
        return RedirectResponse(url="/admin/infra", status_code=302)
    return templates.TemplateResponse("admin_login.html", {"request": request})

@router.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request):
    session = _get_admin_session(request)
    if not session or not session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return RedirectResponse(url="/admin/infra", status_code=302)

@router.get("/admin/infra", response_class=HTMLResponse)
async def admin_infra(request: Request):
    session = _get_admin_session(request)
    if not session or not session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("admin_infra.html", {"request": request})

@router.get("/admin/stats", response_class=HTMLResponse)
async def admin_stats(request: Request):
    session = _get_admin_session(request)
    if not session or not session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("admin_dashboard.html", {"request": request})

@router.get("/inquiry", response_class=HTMLResponse)
async def inquiry_page(request: Request):
    return templates.TemplateResponse("inquiry.html", {"request": request})

@router.get("/recent", response_class=HTMLResponse)
async def recent_viewed(request: Request):
    session = _get_session(request)
    if not session:
        return RedirectResponse(url="/?error=login_required", status_code=302)
    return templates.TemplateResponse("recent.html", {"request": request})

@router.get("/likes", response_class=HTMLResponse)
async def likes(request: Request):
    session = _get_session(request)
    if not session:
        return RedirectResponse(url="/?error=login_required", status_code=302)
    return templates.TemplateResponse("likes.html", {"request": request})

@router.get("/search-history", response_class=HTMLResponse)
async def search_history(request: Request):
    return templates.TemplateResponse("search_history.html", {"request": request})

@router.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse("terms.html", {"request": request})

@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})

@router.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    return templates.TemplateResponse("team.html", {"request": request})

@router.get("/teams", response_class=HTMLResponse)
async def teams_page(request: Request):
    return templates.TemplateResponse("teams.html", {"request": request})

@router.get("/admin/batch", response_class=HTMLResponse)
async def admin_batch(request: Request):
    session = _get_admin_session(request)
    if not session or not session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("admin_batch.html", {"request": request})

@router.get("/admin/inquiry", response_class=HTMLResponse)
async def admin_inquiry(request: Request):
    session = _get_admin_session(request)
    if not session or not session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("admin_inquiry.html", {"request": request})

@router.get("/admin/logs", response_class=HTMLResponse)
async def admin_logs(request: Request):
    session = _get_admin_session(request)
    if not session or not session.get("is_admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("admin_logs.html", {"request": request})

