"""Zoom User-Level OAuth 2.0 flow."""
import logging
from datetime import datetime, timezone, timedelta
import httpx
from app.config import ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
TOKEN_URL = "https://zoom.us/oauth/token"

def get_authorize_url(redirect_uri: str) -> str:
    """Build Zoom OAuth authorization URL."""
    params = {
        "response_type": "code",
        "client_id": ZOOM_CLIENT_ID,
        "redirect_uri": redirect_uri,
    }
    return f"{AUTHORIZE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"

async def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange authorization code for access/refresh tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }, auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET))
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
        }

async def refresh_token(refresh_token_value: str) -> dict:
    """Refresh expired access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
        }, auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET))
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token_value),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
        }

async def get_user_access_token(user) -> str | None:
    """Get valid access token for user, refreshing if needed."""
    if not user.zoom_access_token:
        return None

    # Check if token is expired (refresh 5 min before expiry)
    if user.zoom_token_expires_at and user.zoom_token_expires_at < datetime.now(timezone.utc) + timedelta(minutes=5):
        try:
            tokens = await refresh_token(user.zoom_refresh_token)
            user.zoom_access_token = tokens["access_token"]
            user.zoom_refresh_token = tokens["refresh_token"]
            user.zoom_token_expires_at = tokens["expires_at"]
            # Note: caller must commit the session
        except Exception as e:
            logger.error(f"Failed to refresh Zoom token for user {user.id}: {e}")
            return None

    return user.zoom_access_token

async def get_zoom_user_info(access_token: str) -> dict | None:
    """Get Zoom user profile info."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.zoom.us/v2/users/me", headers={
                "Authorization": f"Bearer {access_token}"
            })
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None

async def get_user_recordings(access_token: str, from_date: str = None) -> list:
    """Get recent recordings for a Zoom user."""
    if not from_date:
        from_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.zoom.us/v2/users/me/recordings",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"from": from_date, "page_size": 30}
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("meetings", [])
    except Exception as e:
        logger.error(f"Failed to get recordings: {e}")
        return []
