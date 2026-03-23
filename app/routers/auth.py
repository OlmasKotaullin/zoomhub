"""Authentication routes: login, register, logout."""

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import create_token, hash_password, verify_password
from app.database import get_db
from app.deps import templates
from app.models import User

router = APIRouter(tags=["auth"])


# ---- HTML routes ----

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


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
            "login.html", {"request": request, "error": "Неверный email или пароль"}, status_code=401
        )
    if not user.is_active:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Аккаунт деактивирован"}, status_code=403
        )

    token = create_token(user.id)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


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
            "register.html", {"request": request, "error": "Пароли не совпадают"}, status_code=400
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Пароль должен быть не менее 6 символов"}, status_code=400
        )

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Пользователь с таким email уже существует"}, status_code=409
        )

    user = User(name=name, email=email, hashed_password=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_token(user.id)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session_token")
    return response


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
