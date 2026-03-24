"""Authentication routes: login, register, logout, OAuth."""

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import create_token, hash_password, verify_password
from app.database import get_db
from app.deps import templates
from app.models import User, InviteCode
from app.oauth import oauth, get_available_providers

router = APIRouter(tags=["auth"])


def _login_response(user_id: int) -> RedirectResponse:
    token = create_token(user_id)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return response


def _find_or_create_oauth_user(db: Session, email: str, name: str) -> User:
    """Find existing user by email or create new one for OAuth login."""
    user = db.query(User).filter(User.email == email).first()
    if user:
        return user
    # Create new user with random password (OAuth-only)
    user = User(
        name=name or email.split("@")[0],
        email=email,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---- HTML routes ----

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request, "error": None, "providers": get_available_providers(),
    })


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Неверный email или пароль", "providers": get_available_providers()},
            status_code=401,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Аккаунт деактивирован", "providers": get_available_providers()},
            status_code=403,
        )
    return _login_response(user.id)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    from app.config import REQUIRE_INVITE_CODE
    return templates.TemplateResponse("register.html", {
        "request": request, "error": None, "providers": get_available_providers(),
        "require_invite": REQUIRE_INVITE_CODE,
    })


@router.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    invite_code: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.config import REQUIRE_INVITE_CODE
    ctx = {"request": request, "error": None, "providers": get_available_providers(), "require_invite": REQUIRE_INVITE_CODE}

    if password != password_confirm:
        ctx["error"] = "Пароли не совпадают"
        return templates.TemplateResponse("register.html", ctx, status_code=400)
    if len(password) < 6:
        ctx["error"] = "Пароль должен быть не менее 6 символов"
        return templates.TemplateResponse("register.html", ctx, status_code=400)

    # Validate invite code
    invite = None
    if REQUIRE_INVITE_CODE:
        if not invite_code.strip():
            ctx["error"] = "Введите инвайт-код"
            return templates.TemplateResponse("register.html", ctx, status_code=400)
        invite = db.query(InviteCode).filter(
            InviteCode.code == invite_code.strip(),
            InviteCode.is_active == True,
        ).first()
        if not invite or invite.used_count >= invite.max_uses:
            ctx["error"] = "Неверный или использованный инвайт-код"
            return templates.TemplateResponse("register.html", ctx, status_code=400)

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        ctx["error"] = "Пользователь с таким email уже существует"
        return templates.TemplateResponse("register.html", ctx, status_code=409)

    user = User(name=name, email=email, hashed_password=hash_password(password))
    if invite:
        user.invite_code_id = invite.id
        invite.used_count += 1
    db.add(user)
    db.commit()
    db.refresh(user)
    return _login_response(user.id)


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session_token")
    return response


# ---- OAuth routes ----

@router.get("/auth/{provider}")
async def oauth_login(provider: str, request: Request):
    """Redirect to OAuth provider."""
    client = getattr(oauth, provider, None)
    if not client:
        return RedirectResponse("/login", status_code=302)
    redirect_uri = str(request.url_for("oauth_callback", provider=provider))
    # Behind reverse proxy (Fly.io/nginx), force HTTPS
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/{provider}/callback")
async def oauth_callback(provider: str, request: Request, db: Session = Depends(get_db)):
    """Handle OAuth callback."""
    client = getattr(oauth, provider, None)
    if not client:
        return RedirectResponse("/login", status_code=302)

    try:
        token = await client.authorize_access_token(request)
    except Exception:
        return RedirectResponse("/login", status_code=302)

    email = None
    name = None

    if provider == "google":
        userinfo = token.get("userinfo", {})
        email = userinfo.get("email")
        name = userinfo.get("name")

    elif provider == "yandex":
        resp = await client.get("https://login.yandex.ru/info?format=json", token=token)
        data = resp.json()
        email = data.get("default_email") or data.get("emails", [None])[0]
        name = data.get("real_name") or data.get("display_name")

    elif provider == "github":
        resp = await client.get("https://api.github.com/user", token=token)
        data = resp.json()
        name = data.get("name") or data.get("login")
        # GitHub may not return email in profile, fetch from emails API
        email = data.get("email")
        if not email:
            emails_resp = await client.get("https://api.github.com/user/emails", token=token)
            emails = emails_resp.json()
            for e in emails:
                if e.get("primary"):
                    email = e.get("email")
                    break
            if not email and emails:
                email = emails[0].get("email")

    if not email:
        return RedirectResponse("/login", status_code=302)

    user = _find_or_create_oauth_user(db, email, name)
    return _login_response(user.id)


# ---- JSON API routes ----

@router.post("/api/auth/login")
async def api_login(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    email = body.get("email", "")
    password = body.get("password", "")

    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        return JSONResponse({"error": "Неверный email или пароль"}, status_code=401)

    return {"token": create_token(user.id), "user": {"id": user.id, "name": user.name, "email": user.email}}


@router.post("/api/auth/register")
async def api_register(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    name = body.get("name", "")
    email = body.get("email", "")
    password = body.get("password", "")

    if not name or not email or len(password) < 6:
        return JSONResponse({"error": "Заполните все поля (пароль мин. 6 символов)"}, status_code=400)

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        return JSONResponse({"error": "Пользователь уже существует"}, status_code=409)

    user = User(name=name, email=email, hashed_password=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)

    return {"token": create_token(user.id), "user": {"id": user.id, "name": user.name, "email": user.email}}


@router.get("/api/agent/token")
async def get_agent_token(request: Request, db: Session = Depends(get_db)):
    """Generate long-lived API token for local agent."""
    from app.deps import get_current_user_optional

    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Generate a long-lived token (stored in user record)
    if not user.agent_api_token:
        token = create_token(user.id)  # Uses standard JWT
        user.agent_api_token = token
        db.commit()

    return {"token": user.agent_api_token}


# ---- Telegram Bot Webhook ----

@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Telegram bot updates (e.g., /start command to link account)."""
    import httpx
    from app.config import TELEGRAM_BOT_TOKEN
    from app.auth import decode_token

    body = await request.json()
    message = body.get("message", {})
    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))

    if not chat_id:
        return {"ok": True}

    if text.startswith("/start"):
        # /start <token> — link Telegram to ZoomHub account
        parts = text.split(maxsplit=1)
        token = parts[1] if len(parts) > 1 else ""

        reply = ""
        if token:
            user_id = decode_token(token)
            if user_id:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    user.telegram_chat_id = chat_id
                    user.notify_telegram = True
                    db.commit()
                    reply = f"Привет, {user.name}! Telegram подключён к ZoomHub. Вы будете получать уведомления о новых встречах."
                else:
                    reply = "Пользователь не найден. Попробуйте получить новую ссылку в настройках ZoomHub."
            else:
                reply = "Ссылка устарела. Получите новую в настройках ZoomHub."
        else:
            reply = "Для подключения уведомлений перейдите по ссылке из настроек ZoomHub."

        if TELEGRAM_BOT_TOKEN and reply:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": reply},
                )

    return {"ok": True}
