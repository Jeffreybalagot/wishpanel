"""Write WishPanel's single sign-on settings from the server (no admin web login needed).

Runs INSIDE the WishPanel container, so it uses WishPanel's own settings code and the secret
is encrypted with /config/secret.key. Copy this file into the container's /config folder
(the appdata folder on Unraid), then e.g.:

    docker exec -it wishpanel python /config/sso-setup.py \\
        --public-url https://comicrequests.example.com \\
        --issuer https://sso.example.com/application/o/wishpanel/ \\
        --client-id <client id> --button "Plex (SSO)" --admin-group wishpanel-admins

The client secret is asked for without echoing it, then single sign-on is switched on.
With --no-secret only the other fields are written and SSO stays as it was.
See docs/SSO-AUTHENTIK.md.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass

from app.config import BootstrapConfig
from app.core.crypto import SecretBox, load_or_create_master_key
from app.db.session import create_engine, create_sessionmaker
from app.services.settings import SettingsService


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Configure WishPanel single sign-on (OIDC).")
    p.add_argument("--public-url", help="WishPanel's public address, e.g. https://wish.example.com")
    p.add_argument("--issuer", help="OIDC issuer URL (Authentik: .../application/o/<slug>/)")
    p.add_argument("--client-id", help="OIDC client ID")
    p.add_argument("--button", help="Text of the SSO button on the sign-in page")
    p.add_argument("--admin-group", help="Provider group whose members are admins ('' = none)")
    p.add_argument(
        "--no-link-by-email",
        action="store_true",
        help="Don't link SSO sign-ins to existing accounts by email",
    )
    p.add_argument(
        "--no-auto-create",
        action="store_true",
        help="Only people who already have an account may use SSO",
    )
    p.add_argument(
        "--no-secret",
        action="store_true",
        help="Don't ask for the client secret; leave SSO on/off as it is",
    )
    return p.parse_args()


async def main() -> None:
    a = _args()
    values: dict[str, object] = {
        "auth.oidc.link_by_email": not a.no_link_by_email,
        "auth.oidc.auto_create": not a.no_auto_create,
    }
    for key, value in (
        ("general.public_url", a.public_url),
        ("auth.oidc.issuer_url", a.issuer),
        ("auth.oidc.client_id", a.client_id),
        ("auth.oidc.button_label", a.button),
        ("auth.oidc.admin_group", a.admin_group),
    ):
        if value is not None:
            values[key] = value

    cfg = BootstrapConfig.from_env()
    box = SecretBox(load_or_create_master_key(cfg.secret_key_path))
    engine = create_engine(cfg.db_url_async)
    try:
        async with create_sessionmaker(engine)() as db:
            settings = SettingsService(db, box)
            changed = [key for key, value in values.items() if await settings.set(key, value)]
            if not a.no_secret:
                secret = getpass.getpass("Client secret (input hidden), then Enter: ").strip()
                if not secret:
                    print("No secret entered - nothing saved.")
                    return
                if await settings.set("auth.oidc.client_secret", secret):
                    changed.append("auth.oidc.client_secret")
                if await settings.set("auth.oidc.enabled", True):
                    changed.append("auth.oidc.enabled")
            await db.commit()
            print("Changed:", ", ".join(changed) or "nothing")
            print(
                "SSO enabled:",
                await settings.get("auth.oidc.enabled"),
                "| secret set:",
                bool(await settings.get("auth.oidc.client_secret")),
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
