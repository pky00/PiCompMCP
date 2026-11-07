# HTTP side: the GUI page and a small REST API over the Controller (PLAN.md
# sections 5 and 9). Endpoints are plain def, so they run in the threadpool.
#
# Auth: a per-start token, handed over once through /auth?t=, then kept in an
# HttpOnly SameSite=Strict cookie. A cookie's site ignores the port, so a page on
# another localhost port would send it too; every non-GET request must therefore
# also carry our own Origin. TrustedHostMiddleware guards against DNS rebinding.
import contextlib
import logging
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from picomp.controller import NotFound, Refused

log = logging.getLogger(__name__)

COOKIE_NAME = "picomp"
INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
NOT_AUTHORIZED = "not authorized: open the URL printed in the server console"


class RenameBody(BaseModel):
    name: str


class SettingsBody(BaseModel):
    screenshots: str


def _same_token(value, token):
    # Bytes, because compare_digest rejects non-ASCII str (a forged cookie could be).
    return secrets.compare_digest(value.encode(), token.encode())


def _detail_handler(status_code):
    async def handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=status_code)

    return handler


def create_app(controller, token, port=8765):
    origins = ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(controller.startup)
        try:
            yield
        finally:
            await run_in_threadpool(controller.shutdown)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_exception_handler(Refused, _detail_handler(409))
    app.add_exception_handler(NotFound, _detail_handler(404))
    app.add_exception_handler(ValueError, _detail_handler(400))

    @app.middleware("http")
    async def guard(request, call_next):
        # Browsers always send Origin on non-GET fetches; GETs only read, and without
        # CORS headers another origin cannot read the responses.
        if request.method not in ("GET", "HEAD") and request.headers.get("origin") not in origins:
            log.warning("refused %s %s from origin %r", request.method, request.url.path,
                        request.headers.get("origin"))
            return JSONResponse({"detail": "bad origin"}, status_code=403)
        if request.url.path != "/auth" and not _same_token(request.cookies.get(COOKIE_NAME, ""), token):
            return JSONResponse({"detail": NOT_AUTHORIZED}, status_code=401)
        return await call_next(request)

    # Added last, so it is the outermost layer and a foreign Host never gets further.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.get("/auth")
    def auth(t: str = ""):
        if not _same_token(t, token):
            return JSONResponse({"detail": NOT_AUTHORIZED}, status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Strict", path="/")
        return response

    @app.get("/")
    def index():
        return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status():
        return controller.status()

    @app.get("/api/recordings")
    def list_recordings():
        return controller.list_recordings()

    @app.patch("/api/recordings/{rec_id}")
    def rename_recording(rec_id: int, body: RenameBody):
        return controller.rename_recording(rec_id, body.name)

    @app.delete("/api/recordings/{rec_id}", status_code=204)
    def delete_recording(rec_id: int):
        controller.delete_recording(rec_id)
        return Response(status_code=204)

    @app.post("/api/recordings/{rec_id}/replay", status_code=202)
    def start_replay(rec_id: int):
        return controller.start_replay(rec_id)

    @app.post("/api/replay/resume", status_code=204)
    def resume_replay():
        controller.resume_replay()
        return Response(status_code=204)

    @app.post("/api/replay/stop", status_code=204)
    def stop_replay():
        controller.stop_replay()
        return Response(status_code=204)

    @app.get("/api/settings")
    def get_settings():
        return controller.get_settings()

    @app.put("/api/settings")
    def put_settings(body: SettingsBody):
        return controller.set_settings({"screenshots": body.screenshots})

    return app
