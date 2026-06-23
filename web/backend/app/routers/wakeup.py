from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/v1")

@router.api_route("/health-check", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def health_check_wakeup(request: Request):
    """
    외부 모니터링용 초경량 헬스체크 API.
    DB 연결 없이 서버의 생존 여부만을 신속하게 응답합니다.
    UptimeRobot 등 모든 외부 모니터링 도구의 HTTP 메서드를 무조건 수용합니다.
    """
    return JSONResponse(
        status_code=200,
        content={"status": "healthy"}
    )
