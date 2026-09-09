"""Admit and bound caption requests before multipart parsing can spool them."""

from contextlib import suppress

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.routing import Match


class UploadRequestTooLargeError(HTTPException):
    """Abort multipart parsing while retaining the upload error response contract."""

    def __init__(self, max_upload_bytes):
        super().__init__(413, f"Upload exceeds maximum size of {max_upload_bytes} bytes")

    def response(self):
        return JSONResponse({"error": self.detail}, status_code=self.status_code)

    @staticmethod
    async def handle(_request, exception):
        return exception.response()


class CaptionUploadCapacityError(HTTPException):
    """Retain the existing response contract when no upload slot is available."""

    def __init__(self):
        super().__init__(429, "Caption capacity is full; retry after current jobs finish")

    def response(self):
        return JSONResponse(
            {"error": self.detail}, status_code=self.status_code, headers={"Retry-After": "5"},
        )


class CaptionUploadLimitMiddleware:  # pylint: disable=too-few-public-methods
    """Reserve capacity and bound each body before multipart parsing."""

    MULTIPART_OVERHEAD_BYTES = 65536
    ADMISSION_SCOPE_KEY = "caption_upload_admission"

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _matches_upload_route(scope):
        """Use the router's first full match, including its compiled path semantics."""
        for route in scope["app"].router.routes:
            match, _child_scope = route.matches(scope)
            if match == Match.FULL:
                return getattr(route, "path", None) == "/captions/upload"
        return False

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["method"] != "POST"
                or not self._matches_upload_route(scope)):
            await self.app(scope, receive, send)
            return

        max_upload_bytes = scope["app"].state.runtime.config.max_upload_bytes
        request_limit = max_upload_bytes + self.MULTIPART_OVERHEAD_BYTES
        error = UploadRequestTooLargeError(max_upload_bytes)
        for name, value in scope["headers"]:
            if name.lower() == b"content-length":
                with suppress(ValueError):
                    if int(value) > request_limit:
                        await error.response()(scope, receive, send)
                        return

        runtime = scope["app"].state.runtime
        admission = await runtime.admit_caption_upload()
        if admission is None:
            await CaptionUploadCapacityError().response()(scope, receive, send)
            return
        scope[self.ADMISSION_SCOPE_KEY] = admission

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > request_limit:
                    # Starlette closes partial multipart files as this unwinds parsing.
                    raise error
            return message

        try:
            await self.app(scope, limited_receive, send)
        finally:
            admission.release()
