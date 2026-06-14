from fastapi import APIRouter
from fastapi.responses import JSONResponse
from ..database import get_prod_cursor, get_dw_cursor

router = APIRouter(prefix="/api/v1")

@router.get("/health-check")
async def health_check_wakeup():
    """
    외부 크론(cron-job.org) 호출용 초경량 헬스체크 및 DB 예열(Warm-up) API.
    Neon Serverless DB가 5분 동안 요청이 없을 시 절전에 들어가는 것을 방지하기 위해,
    해당 API 호출 시 DB에 SELECT 1; 쿼리를 강제 수행합니다.
    """
    db_status = "connected"
    try:
        # Neon DB(PROD 및 DW)에 초경량 SELECT 1; 쿼리를 실행하여 예열(Wakeup) 시킴
        with get_prod_cursor(dict_cursor=False) as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
            
        try:
            with get_dw_cursor(dict_cursor=False) as cur_dw:
                cur_dw.execute("SELECT 1;")
                cur_dw.fetchone()
        except Exception:
            # DW DB는 선택사항이거나 폴백이 있을 수 있으므로 실패하더라도 전체 에러로 내보내지 않음
            pass
            
    except Exception as e:
        db_status = "error"
        
    return JSONResponse(
        status_code=200 if db_status == "connected" else 500,
        content={"status": "healthy", "database": db_status}
    )
