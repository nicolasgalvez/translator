"""Bound caption request bodies before multipart parsing can spool their contents."""

from contextlib import suppress

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class UploadRequestTooLargeError(HTTPException):
    """Abort multipart parsing while retaining the upload error response contract."""

    def __init__(self, max_upload_bytes):
        super().__init__(413, f"Upload exceeds maximum size of {max_upload_bytes} bytes")

    def response(self):
        return JSONResponse({"error": self.detail}, status_code=self.status_code)

    @staticmethod
    async def handle(_request, exception):
        return exception.response()


class CaptionUploadLimitMiddleware:  # pylint: disable=too-few-public-methods
    """Enforce the file allowance plus 64 KiB of multipart framing per request."""

    MULTIPART_OVERHEAD_BYTES = 65536

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["method"] != "POST"
                or scope["path"] != "/captions/upload"):
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

        await self.app(scope, limited_receive, send)
