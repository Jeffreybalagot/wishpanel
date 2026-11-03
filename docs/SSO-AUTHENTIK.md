# Single sign-on with Authentik (e.g. "Log in with Plex")

This guide sets up WishPanel's OpenID Connect sign-in against [Authentik](https://goauthentik.io).
It is written from a real setup where Authentik signs people in with their Plex account, but any
Authentik login source works the same way.

After setup the sign-in page shows **two ways in, side by side**:

- the SSO button (label is yours, e.g. "Plex (SSO)"), and
- the normal Kavita username/password form — unchanged, and your way in if Authentik is down.

## 1. Authentik: provider and application

1. **Applications → Providers → Create → OAuth2/OpenID Provider**
   - Client type: **Confidential**
   - Redirect URI (strict): `https://<your WishPanel address>/api/auth/oidc/callback`
   - Signing key: any certificate (e.g. "authentik Self-signed Certificate")
   - Scopes: `openid`, `email`, `profile` (`offline_access` is optional)
2. **Applications → Applications → Create**, slug e.g. `wishpanel`, provider = the one above.
   - No policy bindings = everyone who can sign in to Authentik may use WishPanel. Bind a group
     to restrict it.
3. Optional admin group: create a group (e.g. `wishpanel-admins`) and add the people who should
   be WishPanel admins. Authentik's default `profile` scope already sends a `groups` claim.

Note the **issuer**: `https://<authentik>/application/o/<slug>/` (shown on the provider's
overview page as "OpenID Configuration Issuer").

### Gotchas we hit

- **Grant types empty (Authentik 2026.x, providers created through the API):** every sign-in fails
  with `invalid_request` and the Authentik log says "Invalid grant_type for provider". Edit the
  provider and make sure *Authorization code* and *Refresh token* are allowed grant types.
  Providers created in the UI are fine.
- **`email_verified` is false:** Authentik's default `email` scope mapping sends
  `"email_verified": false` for everyone. WishPanel only links an SSO sign-in to an existing
  (Kavita-created) account by email when the email is not marked unverified, so returning users
  would get a second, empty account instead. If every Authentik user comes from a source that
  verifies emails (Plex, Google, …), add a scope mapping and use it on the provider instead of the
  default email mapping:

  ```python
  # Customization → Property Mappings → Create → Scope Mapping, scope name: email
  return {"email": request.user.email, "email_verified": True}
  ```

- **An existing Authentik session is reused.** If the browser is already signed in to Authentik,
  the SSO button signs in as that user without showing the Plex/Google picker. Test other
  accounts in a private window.
- **Copying the client secret** (Authentik 2026.x UI): provider → **Edit** → Client Secret →
  **Modify** shows the value; press *Cancel* afterwards, not *Save*.

## 2. WishPanel: settings

**Admin → Settings → Authentication → Single sign-on**

| Field | Value |
|---|---|
| Single sign-on | on |
| Issuer URL | `https://<authentik>/application/o/<slug>/` |
| Client ID / Client secret | from the Authentik provider |
| Button text | e.g. `Plex (SSO)` |
| Admin group | e.g. `wishpanel-admins` (empty = SSO never grants admin) |
| Link to existing accounts by email | on (only if emails are verified, see above) |
| Create accounts for new SSO users | on, or off to allow only people who already have an account |

Also set **General → Public URL** (e.g. `https://comicrequests.example.com`) if WishPanel sits
behind a reverse proxy — the redirect URI is built from it. Press **Test** to check that the
issuer's discovery document is reachable from the container.

### Without the web UI

If you can't sign in to WishPanel as an admin from the browser you're using, the same settings
can be written from the server with [`unraid/sso-setup.py`](../unraid/sso-setup.py). It uses
WishPanel's own settings code (the secret is encrypted with `/config/secret.key`) and asks for the
client secret without echoing it:

```bash
# copy the script into the container's /config folder (appdata), then:
docker exec -it wishpanel python /config/sso-setup.py \
  --public-url https://comicrequests.example.com \
  --issuer https://sso.example.com/application/o/wishpanel/ \
  --client-id <client id> --button "Plex (SSO)" --admin-group wishpanel-admins
```

Add `--no-secret` to write only the non-secret fields (SSO stays off); run it again without the
flag to paste the secret and switch SSO on. Re-run it any time to rotate the secret.

## 3. Who ends up as which account

1. Someone who signed in with SSO before → same account (matched by the provider's `sub`).
2. Otherwise an existing account with the same email (typically created by a Kavita sign-in) →
   that account, with its requests and daily limits. It can still sign in with Kavita too.
3. Otherwise a new, non-admin account (if "Create accounts for new SSO users" is on).

Admin rights: Kavita admins stay admins either way. For SSO-only accounts, membership of the
admin group decides at every SSO sign-in. For an account linked to Kavita, the group can add admin
rights at SSO sign-in; the next Kavita password sign-in applies Kavita's role again.

## Turning it off

Switch **Single sign-on** off in the settings (or run the script and answer nothing to the
secret prompt, then switch it off in the UI). The Kavita login is unaffected.
