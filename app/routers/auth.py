"""Authentication routes: login, register, logout, OAuth."""

import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import create_token, hash_password, verify_password
from app.database import get_db
from app.deps import templates
from app.models import User
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
    return templates.TemplateResponse("register.html", {
        "request": request, "error": None, "providers": get_available_providers(),
    })


@router.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if password != password_confirm:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Пароли не совпадают", "providers": get_available_providers()},
            status_code=400,
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Пароль должен быть не менее 6 символов", "providers": get_available_providers()},
            status_code=400,
        )
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Пользователь с таким email уже существует", "providers": get_available_providers()},
            status_code=409,
        )

    user = User(name=name, email=email, hashed_password=hash_password(password))
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
