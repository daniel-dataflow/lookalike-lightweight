from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/v1")

@router.get("/health-check")
async def health_check_wakeup():
    """
    외부 모니터링용 초경량 헬스체크 API.
    DB 연결 없이 서버의 생존 여부만을 신속하게 응답합니다.
    """
    return JSONResponse(
        status_code=200,
        content={"status": "healthy"}
    )
