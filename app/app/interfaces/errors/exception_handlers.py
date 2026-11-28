import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.application.audit_context import get_context
from app.application.errors.exceptions import AppException
from app.application.sensitive_paths import (
    is_agent_onboarding_path,
    redact_sensitive_path,
)

logger = logging.getLogger(__name__)


def _operation_id(request: Request) -> str:
    context = get_context() or getattr(request.state, "audit_context", None)
    if context is not None and context.operation_id:
        return context.operation_id
    return "operation-unavailable"


def _is_agent_onboarding(request: Request) -> bool:
    return is_agent_onboarding_path(request.url.path)


def _body(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
) -> dict[str, object]:
    normalized_code = code[:128] or "internal_error"
    normalized_message = message[:1000] or "Request rejected"
    operation_id = _operation_id(request)
    return {
        "type": f"urn:sunmoonai:problem:{normalized_code}",
        "title": normalized_message,
        "status": status,
        "detail": normalized_message,
        "instance": redact_sensitive_path(request.url.path),
        "code": normalized_code,
        "operation_id": operation_id,
        # Compatibility extension for the published browser error contract.
        "error": {
            "code": normalized_code,
            "message": normalized_message,
            "operation_id": operation_id,
        },
    }


def _response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=_body(request, status=status, code=code, message=message),
        media_type="application/problem+json",
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(
        request: Request, error: AppException
    ) -> JSONResponse:
        if not _is_agent_onboarding(request):
            logger.warning(
                "application_error status=%s code=%s path=%s",
                error.status_code,
                error.code,
                redact_sensitive_path(request.url.path),
            )
        return _response(
            request,
            status=error.status_code,
            code=error.code,
            message=error.msg,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        if not _is_agent_onboarding(request):
            logger.warning(
                "request_validation_error path=%s count=%s",
                redact_sensitive_path(request.url.path),
                len(error.errors()),
            )
        return _response(
            request,
            status=422,
            code="invalid_request",
            message="The request payload is invalid",
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, error: HTTPException
    ) -> JSONResponse:
        if not _is_agent_onboarding(request):
            logger.warning(
                "http_error status=%s path=%s",
                error.status_code,
                redact_sensitive_path(request.url.path),
            )
        return _response(
            request,
            status=error.status_code,
            headers={
                k: v
                for k, v in (error.headers or {}).items()
                if k.lower() not in {"content-type", "content-length"}
            },
            code="not_found" if error.status_code == 404 else "request_invalid",
            message=(
                "Resource not found" if error.status_code == 404 else "Request rejected"
            ),
        )

    @app.exception_handler(Exception)
    async def exception_handler(request: Request, error: Exception) -> JSONResponse:
        if not _is_agent_onboarding(request):
            logger.exception(
                "unhandled_exception path=%s type=%s",
                redact_sensitive_path(request.url.path),
                type(error).__name__,
            )
        return _response(
            request,
            status=500,
            code="internal_error",
            message="The service could not complete the request",
        )
