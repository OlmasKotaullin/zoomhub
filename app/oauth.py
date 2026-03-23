"""OAuth providers configuration: Google, Yandex, GitHub."""

from authlib.integrations.starlette_client import OAuth
from starlette.config import Config

from app.config import (
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
    YANDEX_CLIENT_ID, YANDEX_CLIENT_SECRET,
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET,
)

oauth = OAuth()

# --- Google ---
if GOOGLE_CLIENT_ID:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

# --- Yandex ---
if YANDEX_CLIENT_ID:
    oauth.register(
        name="yandex",
        client_id=YANDEX_CLIENT_ID,
        client_secret=YANDEX_CLIENT_SECRET,
        authorize_url="https://oauth.yandex.ru/authorize",
        access_token_url="https://oauth.yandex.ru/token",
        userinfo_endpoint="https://login.yandex.ru/info?format=json",
        client_kwargs={"scope": "login:email login:info"},
    )

# --- GitHub ---
if GITHUB_CLIENT_ID:
    oauth.register(
        name="github",
        client_id=GITHUB_CLIENT_ID,
        client_secret=GITHUB_CLIENT_SECRET,
        authorize_url="https://github.com/login/oauth/authorize",
        access_token_url="https://github.com/login/oauth/access_token",
        userinfo_endpoint="https://api.github.com/user",
        client_kwargs={"scope": "user:email"},
    )


def get_available_providers() -> list[dict]:
    """Return list of configured OAuth providers for UI."""
    providers = []
    if GOOGLE_CLIENT_ID:
        providers.append({"name": "google", "label": "Google", "icon": "G", "color": "#4285f4"})
    if YANDEX_CLIENT_ID:
        providers.append({"name": "yandex", "label": "Яндекс", "icon": "Я", "color": "#fc3f1d"})
    if GITHUB_CLIENT_ID:
        providers.append({"name": "github", "label": "GitHub", "icon": "GH", "color": "#24292e"})
    return providers
