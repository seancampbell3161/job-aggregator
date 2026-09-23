"""The web UI login: the session cookie, the /welcome, /login, /logout, and
/account/password pages, and the login gate that requires a session for
everything outside PUBLIC_PATHS / PUBLIC_PREFIXES."""
from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from src.auth.errors import AlreadyClaimed, PasswordRejected, WrongPassword
from src.auth.passwords import MIN_PASSWORD_LENGTH
from src.auth.service import SESSION_IDLE_TTL

log = logging.getLogger(__name__)

SESSION_COOKIE = "jobagg_session"

# Reachable without a session. Exact paths, so a future /tailor/... route is
# gated unless added here on purpose. /logout is public so signing out with an
# expired session still clears the cookie.
PUBLIC_PATHS = frozenset({"/login", "/welcome", "/logout", "/tailor", "/tailor/pdf", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_IDLE_TTL.total_seconds()), path="/",
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")


def safe_next(value: str) -> str:
    """``value`` when it is a path on this site, else "/". Refuses
    protocol-relative URLs (//host), backslashes (browsers read /\\host as
    //host), absolute URLs, and control characters — each an open redirect."""
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def _page(request: Request, name: str, *, status_code: int = 200,
          headers: dict[str, str] | None = None, **context) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, name, {"min_length": MIN_PASSWORD_LENGTH, **context},
        status_code=status_code, headers=headers,
    )


def _signed_in_redirect(request: Request, url: str, token: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    set_session_cookie(response, request, token)
    return response


def _throttled(request: Request, template: str, wait: int, **context) -> HTMLResponse:
    log.warning("login_throttled", extra={"retry_after_s": wait})
    return _page(request, template, status_code=429, headers={"Retry-After": str(wait)},
                 error=f"Too many attempts. Try again in {wait} s.", **context)


def _has_password_or_fail_closed(request: Request) -> bool | PlainTextResponse:
    """auth.has_password(), or the login gate's own fail-closed 503 if the
    store can't be read. /welcome and /login are in PUBLIC_PATHS, so the
    gate's try/except never runs for them — without this they'd 500 instead."""
    try:
        return request.app.state.auth.has_password()
    except Exception as exc:  # noqa: BLE001 — fail closed, matching the login gate
        log.error("auth_store_unavailable", extra={"error": str(exc)})
        return PlainTextResponse("Cannot read the login database.", status_code=503)


def register_auth_routes(app: FastAPI) -> None:
    @app.get("/welcome", response_class=HTMLResponse)
    def welcome(request: Request):
        has_password = _has_password_or_fail_closed(request)
        if isinstance(has_password, PlainTextResponse):
            return has_password
        if has_password:
            return RedirectResponse("/login?claimed=1", status_code=303)
        return _page(request, "welcome.html")

    @app.post("/welcome", response_class=HTMLResponse)
    def create_password(request: Request, password: str = Form(""), confirm: str = Form("")):
        auth = request.app.state.auth
        has_password = _has_password_or_fail_closed(request)
        if isinstance(has_password, PlainTextResponse):
            return has_password
        if has_password:
            return RedirectResponse("/login?claimed=1", status_code=303)
        if password != confirm:
            return _page(request, "welcome.html", status_code=400,
                         error="The passwords don't match.")
        try:
            token = auth.claim(password)
        except AlreadyClaimed:
            return RedirectResponse("/login?claimed=1", status_code=303)
        except PasswordRejected as exc:
            return _page(request, "welcome.html", status_code=400, error=str(exc))
        log.info("password_created")
        return _signed_in_redirect(request, "/", token)

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request, next_path: str = Query("", alias="next"), claimed: str = ""):
        has_password = _has_password_or_fail_closed(request)
        if isinstance(has_password, PlainTextResponse):
            return has_password
        if not has_password:
            return RedirectResponse("/welcome", status_code=303)
        return _page(request, "login.html",
                     next=safe_next(next_path) if next_path else "", claimed=claimed == "1")

    @app.post("/login", response_class=HTMLResponse)
    def sign_in(request: Request, password: str = Form(""),
                next_path: str = Form("", alias="next")):
        auth, throttle = request.app.state.auth, request.app.state.login_throttle
        shown_next = safe_next(next_path) if next_path else ""
        has_password = _has_password_or_fail_closed(request)
        if isinstance(has_password, PlainTextResponse):
            return has_password
        if not has_password:
            return RedirectResponse("/welcome", status_code=303)
        wait = throttle.check()
        if wait is not None:
            return _throttled(request, "login.html", wait, next=shown_next)
        token = auth.login(password)
        if token is None:
            failures = throttle.record_failure()
            log.warning("login_failed", extra={"consecutive_failures": failures})
            return _page(request, "login.html", status_code=401, next=shown_next,
                         error="Incorrect password.")
        throttle.record_success()
        from src.web.home import landing_url  # lazy: home imports wizard routes
        target = shown_next or landing_url(request)
        return _signed_in_redirect(request, target, token)

    @app.post("/logout")
    def sign_out(request: Request):
        request.app.state.auth.logout(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookie(response)
        return response

    @app.get("/account/password", response_class=HTMLResponse)
    def account_password(request: Request):
        return _page(request, "account_password.html")

    @app.post("/account/password", response_class=HTMLResponse)
    def change_password(request: Request, current: str = Form(""),
                        password: str = Form(""), confirm: str = Form("")):
        auth, throttle = request.app.state.auth, request.app.state.login_throttle
        wait = throttle.check()
        if wait is not None:
            return _throttled(request, "account_password.html", wait)
        if password != confirm:
            return _page(request, "account_password.html", status_code=400,
                         error="The new passwords don't match.")
        try:
            token = auth.change_password(current, password)
        except WrongPassword:
            failures = throttle.record_failure()
            log.warning("login_failed", extra={"consecutive_failures": failures})
            return _page(request, "account_password.html", status_code=400,
                         error="Incorrect password.")
        except PasswordRejected as exc:
            return _page(request, "account_password.html", status_code=400, error=str(exc))
        throttle.record_success()
        response = _page(request, "account_password.html", changed=True)
        set_session_cookie(response, request, token)
        return response


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def login_url(next_path: str) -> str:
    return "/login" if next_path == "/" else f"/login?next={quote(next_path, safe='')}"


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _return_path(request: Request) -> str:
    """Where to land after signing in: for an htmx request that sent
    HX-Current-URL, the page that issued it; otherwise this request's own
    URL — which, for an htmx request missing that header, is the fragment
    endpoint, not the page the browser is showing."""
    current = request.headers.get("hx-current-url") if _is_htmx(request) else None
    parts = urlsplit(current) if current else request.url
    path = parts.path or "/"
    return safe_next(f"{path}?{parts.query}" if parts.query else path)


def _deny(request: Request, location: str, *, clear_cookie: bool) -> Response:
    """htmx acts on HX-Redirect before its error handling, so a 60 s poll
    navigates to the login page instead of swapping it into a fragment."""
    response: Response
    if _is_htmx(request):
        response = Response(status_code=401, headers={"HX-Redirect": location})
    elif request.method in ("GET", "HEAD"):
        response = RedirectResponse(location, status_code=303)
    else:
        response = PlainTextResponse("Sign in required.", status_code=401)
    if clear_cookie:
        clear_session_cookie(response)
    return response


def _sets_session_cookie(response: Response) -> bool:
    return any(value.startswith(f"{SESSION_COOKIE}=")
               for value in response.headers.getlist("set-cookie"))


def register_login_gate(app: FastAPI) -> None:
    @app.middleware("http")
    async def _login_gate(request: Request, call_next):
        request.state.session = None
        if is_public(request.scope["path"]):
            return await call_next(request)
        auth = request.app.state.auth
        token = request.cookies.get(SESSION_COOKIE)
        try:
            has_password = await run_in_threadpool(auth.has_password)
            session = (await run_in_threadpool(auth.resolve, token)
                       if has_password and token else None)
        except Exception as exc:  # noqa: BLE001 — fail closed: never serve a page we can't authorize
            log.error("auth_store_unavailable", extra={"error": str(exc)})
            return PlainTextResponse("Cannot read the login database.", status_code=503)
        if not has_password:
            return _deny(request, "/welcome", clear_cookie=token is not None)
        if session is None:
            return _deny(request, login_url(_return_path(request)), clear_cookie=token is not None)
        request.state.session = session
        response = await call_next(request)
        # Slide the browser's cookie along with the server-side session — but
        # never over a cookie the route set itself (change-password).
        if session.touched and not _sets_session_cookie(response):
            set_session_cookie(response, request, token)
        return response
