"""Admin panel: dashboard, users, tickets, invite codes."""

import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates, get_current_user_optional
from app.models import (
    User, Meeting, MeetingStatus, Transcript, InviteCode, SupportTicket,
)

router = APIRouter(prefix="/admin")


def _require_admin(request: Request, db: Session):
    user = get_current_user_optional(request, db)
    if not user or not getattr(user, "is_admin", False):
        return None
    return user


# ---- Dashboard ----

@router.get("", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    user = _require_admin(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

    total_users = db.query(func.count(User.id)).scalar()
    active_users = db.query(func.count(User.id)).filter(User.is_active == True).scalar()
    total_meetings = db.query(func.count(Meeting.id)).scalar()
    ready_meetings = db.query(func.count(Meeting.id)).filter(Meeting.status == MeetingStatus.ready).scalar()
    error_meetings = db.query(func.count(Meeting.id)).filter(Meeting.status == MeetingStatus.error).scalar()

    # Hours of audio (estimate from transcript word count)
    total_words = db.query(func.sum(func.length(Transcript.full_text))).scalar() or 0
    audio_hours = round(total_words / 800 / 60, 1)  # ~800 chars/min speech

    # Unread tickets
    unread_tickets = db.query(func.count(SupportTicket.id)).filter(SupportTicket.is_read == False).scalar()

    # Activity last 30 days (meetings per day)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    daily_activity = (
        db.query(
            func.date(Meeting.created_at).label("day"),
            func.count(Meeting.id).label("cnt"),
        )
        .filter(Meeting.created_at >= thirty_days_ago)
        .group_by(func.date(Meeting.created_at))
        .order_by(func.date(Meeting.created_at))
        .all()
    )
    chart_labels = [str(row.day) for row in daily_activity]
    chart_data = [row.cnt for row in daily_activity]

    # Meetings this week / month
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    meetings_week = db.query(func.count(Meeting.id)).filter(Meeting.created_at >= week_ago).scalar()
    meetings_month = db.query(func.count(Meeting.id)).filter(Meeting.created_at >= month_ago).scalar()

    # Invite stats
    total_invites = db.query(func.count(InviteCode.id)).scalar()
    used_invites = db.query(func.sum(InviteCode.used_count)).scalar() or 0

    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "tab": "dashboard",
        "total_users": total_users, "active_users": active_users,
        "total_meetings": total_meetings, "ready_meetings": ready_meetings,
        "error_meetings": error_meetings, "audio_hours": audio_hours,
        "unread_tickets": unread_tickets,
        "chart_labels": chart_labels, "chart_data": chart_data,
        "meetings_week": meetings_week, "meetings_month": meetings_month,
        "total_invites": total_invites, "used_invites": used_invites,
    })


# ---- Users ----

@router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request, db: Session = Depends(get_db)):
    user = _require_admin(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

    users_data = []
    all_users = db.query(User).order_by(desc(User.created_at)).all()
    for u in all_users:
        meeting_count = db.query(func.count(Meeting.id)).filter(Meeting.user_id == u.id).scalar()
        ready_count = db.query(func.count(Meeting.id)).filter(
            Meeting.user_id == u.id, Meeting.status == MeetingStatus.ready
        ).scalar()
        error_count = db.query(func.count(Meeting.id)).filter(
            Meeting.user_id == u.id, Meeting.status == MeetingStatus.error
        ).scalar()
        # Word count → hours
        words = db.query(func.sum(func.length(Transcript.full_text))).join(Meeting).filter(
            Meeting.user_id == u.id
        ).scalar() or 0
        hours = round(words / 800 / 60, 1)
        # Last meeting
        last_meeting = db.query(Meeting).filter(Meeting.user_id == u.id).order_by(desc(Meeting.created_at)).first()
        # Conversion rate
        conversion = round(ready_count / meeting_count * 100) if meeting_count else 0

        invite_count = db.query(func.count(InviteCode.id)).filter(InviteCode.owner_id == u.id).scalar()
        invite_available = db.query(func.count(InviteCode.id)).filter(
            InviteCode.owner_id == u.id, InviteCode.used_count < InviteCode.max_uses
        ).scalar()

        users_data.append({
            "user": u,
            "meetings": meeting_count,
            "ready": ready_count,
            "errors": error_count,
            "hours": hours,
            "last_meeting": last_meeting,
            "conversion": conversion,
            "invites_total": invite_count,
            "invites_available": invite_available,
        })

    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "tab": "users",
        "users_data": users_data,
    })


@router.post("/users/{user_id}/toggle", response_class=HTMLResponse)
async def toggle_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    admin = _require_admin(request, db)
    if not admin:
        return RedirectResponse("/", status_code=302)

    target = db.query(User).filter(User.id == user_id).first()
    if target and target.id != admin.id:
        target.is_active = not target.is_active
        db.commit()

    return RedirectResponse("/admin/users", status_code=303)


# ---- Tickets ----

@router.get("/tickets", response_class=HTMLResponse)
async def admin_tickets(request: Request, status: str = "", db: Session = Depends(get_db)):
    user = _require_admin(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

    query = db.query(SupportTicket).order_by(desc(SupportTicket.created_at))
    if status:
        query = query.filter(SupportTicket.status == status)
    tickets = query.all()

    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "tab": "tickets",
        "tickets": tickets, "filter_status": status,
    })


@router.post("/tickets/{ticket_id}/status", response_class=HTMLResponse)
async def update_ticket_status(
    request: Request, ticket_id: int, status: str = Form(...), db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if not admin:
        return RedirectResponse("/", status_code=302)

    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if ticket:
        ticket.status = status
        ticket.is_read = True
        db.commit()

    return RedirectResponse("/admin/tickets", status_code=303)


@router.post("/tickets/{ticket_id}/reply", response_class=HTMLResponse)
async def reply_ticket(
    request: Request, ticket_id: int, reply: str = Form(...), db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if not admin:
        return RedirectResponse("/", status_code=302)

    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if ticket:
        ticket.admin_reply = reply
        ticket.is_read = True
        if ticket.status == "new":
            ticket.status = "in_progress"
        db.commit()

    return RedirectResponse("/admin/tickets", status_code=303)


# ---- Invites ----

@router.get("/invites", response_class=HTMLResponse)
async def admin_invites(request: Request, db: Session = Depends(get_db)):
    user = _require_admin(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

    invites = db.query(InviteCode).order_by(desc(InviteCode.created_at)).all()

    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "tab": "invites",
        "invites": invites,
    })


@router.post("/invites", response_class=HTMLResponse)
async def create_invite(
    request: Request, max_uses: int = Form(1), db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if not admin:
        return RedirectResponse("/", status_code=302)

    code = f"ZOOM-{secrets.token_hex(3).upper()}"
    invite = InviteCode(code=code, max_uses=max_uses)
    db.add(invite)
    db.commit()

    return RedirectResponse("/admin/invites", status_code=303)


@router.post("/users/{user_id}/give-invites", response_class=HTMLResponse)
async def give_invites(
    request: Request, user_id: int, count: int = Form(2), db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    if not admin:
        return RedirectResponse("/", status_code=302)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return RedirectResponse("/admin/users", status_code=303)

    for _ in range(min(count, 5)):
        code = f"ZH-{secrets.token_hex(3).upper()}"
        db.add(InviteCode(code=code, max_uses=1, owner_id=target.id))
    db.commit()

    return RedirectResponse("/admin/users", status_code=303)


@router.post("/recalc-usage")
async def recalc_usage(request: Request, db: Session = Depends(get_db)):
    """Пересчитать usage для всех пользователей по их meetings.duration_seconds."""
    admin = _require_admin(request, db)
    if not admin:
        return {"error": "forbidden"}

    import subprocess
    from datetime import datetime, timezone
    from pathlib import Path

    now = datetime.now(timezone.utc)
    users = db.query(User).all()
    results = []

    for user in users:
        meetings = db.query(Meeting).filter(
            Meeting.user_id == user.id,
            Meeting.status == "ready",
        ).all()

        # Fix missing duration_seconds via ffprobe
        for m in meetings:
            if not m.duration_seconds and m.audio_path and Path(m.audio_path).exists():
                try:
                    probe = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", m.audio_path],
                        capture_output=True, text=True, timeout=15
                    )
                    if probe.returncode == 0 and probe.stdout.strip():
                        m.duration_seconds = int(float(probe.stdout.strip()))
                except Exception:
                    pass

        # Recalculate monthly usage
        total_seconds = sum(m.duration_seconds or 0 for m in meetings
                           if m.created_at and m.created_at.month == now.month and m.created_at.year == now.year)
        old_usage = user.usage_seconds_month or 0
        user.usage_seconds_month = total_seconds
        if not user.usage_month_start or user.usage_month_start.month != now.month:
            user.usage_month_start = now
        results.append({"user": user.email, "old": old_usage, "new": total_seconds})

    db.commit()
    return {"status": "ok", "users": results}
