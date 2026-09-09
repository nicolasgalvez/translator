"""ASGI and command-line clients for the translator runtime."""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from caption_uploads import CaptionUploadLimitMiddleware, UploadRequestTooLargeError
from translator_runtime import RuntimeConfig, TranslatorRuntime


def default_runtime_factory():
    return TranslatorRuntime(RuntimeConfig.from_environment(os.environ))


def create_app(runtime_factory=default_runtime_factory) -> FastAPI:
    """Build routes without starting resources; each lifespan owns one runtime."""
    @asynccontextmanager
    async def lifespan(application):
        runtime = runtime_factory()
        application.state.runtime = runtime
        try:
            await runtime.start()
            yield
        finally:
            await runtime.stop()

    application = FastAPI(lifespan=lifespan)
    application.add_middleware(CaptionUploadLimitMiddleware)
    application.add_exception_handler(UploadRequestTooLargeError, UploadRequestTooLargeError.handle)
    assets = Path("frontend/dist/assets")
    if assets.exists():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return await request.app.state.runtime.index(request)

    @application.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        return await request.app.state.runtime.history(request)

    @application.get("/history/{filename}", response_class=HTMLResponse)
    async def view_transcript(request: Request, filename: str):
        return await request.app.state.runtime.view_transcript(request, filename)

    @application.get("/audio/{filename}")
    async def serve_audio(request: Request, filename: str):
        return await request.app.state.runtime.serve_audio(filename)

    @application.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.app.state.runtime.websocket_endpoint(ws)

    @application.get("/captions", response_class=HTMLResponse)
    async def captions_page(request: Request):
        return await request.app.state.runtime.captions_page(request)

    @application.post("/captions/upload")
    async def captions_upload(request: Request, file: UploadFile = File(...)):
        admission = request.scope.get(CaptionUploadLimitMiddleware.ADMISSION_SCOPE_KEY)
        return await request.app.state.runtime.captions_upload(file, admission)

    @application.get("/captions/status/{job_id}")
    async def captions_status(request: Request, job_id: str):
        return await request.app.state.runtime.captions_status(job_id)

    @application.get("/captions/download/{job_id}/{filename}")
    async def captions_download(request: Request, job_id: str, filename: str):
        return await request.app.state.runtime.captions_download(job_id, filename)

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    try:
        config = RuntimeConfig.from_environment(os.environ)
    except ValueError as exc:
        sys.exit(f"translator: {exc}")
    uvicorn.run(create_app(lambda: TranslatorRuntime(config)), host=config.host, port=config.port)
