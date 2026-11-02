# BB Microsoft Graph vault — generated links, device codes, and multi-account operations

The BB Link Manager at `/links` can **Generate live link** for an authorized
Microsoft account owner, e.g. `https://bb.centrolbank.com/l/<random-token>`.
Each link is random, expires after 30 days, and can be regenerated (rotating
away the previous URL) or revoked from Link Manager.

The generated link opens a **BB-branded page**, with no imitation Microsoft
sign-in and no collection of Microsoft passwords, MFA codes, or browser cookies.

## Device-code authorization (code shown in BB)

1. Open your generated `/l/<token>` URL. Sign into the *same BB account* that
   generated the link. Other BB users cannot redeem it.
2. **Enable authenticator MFA** at `/security/mfa` (required for the vault).
3. Click **Show Microsoft device authorization code**. BB calls Microsoft's
   real `/oauth2/v2.0/devicecode` endpoint and displays Microsoft's
   returned `user_code` and `verification_uri`.
4. At the *official* Microsoft verification website, enter the device code,
   authenticate and complete MFA if Microsoft requests it, then approve the
   delegated Graph permissions.
5. BB polls Microsoft's `/token` endpoint at Microsoft's requested interval,
   checks the resulting identity with Graph `/me`, and seals both access and
   refresh tokens **server-side** in that BB user's per-user SQLite mail.db.
   No Microsoft OAuth token enters the browser.
6. BB opens `/vault?mailbox=<id>`, with a separate account tile for every
   connected mailbox.

A device code is **not** a Microsoft MFA code. The user enters it on
Microsoft's official site, never in a BB form. If device authorization is
refused or expires, start again.

## Browser redirect alternative

The same generated link still offers **Continue to Microsoft**, using the
Microsoft-hosted OAuth authorization-code flow. Microsoft redirects to
`<BB_BASE_URL>/microsoft/callback`, which now stores that mailbox in the
**same encrypted per-user mail database**.

The existing `/mail/microsoft/callback` command-center flow also persists
tokens and metadata in this database.

## Vault and security

- OAuth refresh tokens and cached access tokens are sealed with **AES-256-GCM**
  using the server-held BB secret through `bb.mail.crypto`. The key comes
  from `BB_SECRET_KEY` or BB's locally generated owner-only secret file. Set
  a strong stable secret in production, keep it out of the repo and out of
  backups shared with other users, and plan for secure key rotation.
- Each mailbox's sealed creds, granted scopes and access-token expiry are
  in the mailbox row of the per-user SQLite `mail.db`. They are independently
  addressable by opaque mailbox ID.
- Token expiry is checked before Graph calls. Tokens within 60 seconds of
  expiration are refreshed; replacement refresh tokens and timestamps are
  sealed back into the database. Microsoft can revoke or expire authorization
  regardless of how recently BB used it.
- On revoked/invalid grants, a mailbox is marked **needs reconnect**.
  Network errors do not automatically mean revoked authorization.
- The BB browser receives only BB session cookies: HttpOnly, SameSite=Lax,
  and Secure on the production HTTPS URL.
- New BB passwords require at least 14 characters. Authenticator TOTP
  enrollment is available under `/security/mfa`; the vault requires TOTP,
  and enabled users must verify TOTP on every new BB login.
- Mailbox disconnect requires another valid TOTP and removes that
  mailbox's encrypted credentials from BB. A Microsoft-side app revocation
  can additionally invalidate Microsoft's authorization.
- Older single-mailbox plaintext JSON token files are migrated to the
  encrypted database and deleted at first vault access. Existing historical
  backups may still contain older credentials and must be protected/cleaned.
  Do not store passwords or session cookies in the vault.

## Inside BB

The `/vault` page offers separate mailbox tiles. Selecting one scopes
subsequent Graph calls to that mailbox's own credentials:
- Read up to 25 latest `/me/messages` and view individual messages
- `/me/sendMail` and `/me/messages/{id}/reply`
- List `/me/drive/root/children`; upload files to the selected account's
  OneDrive (up to 4 MB), download files (up to 10 MB)
- Read `/organization` metadata if the granted scopes and tenant policy
  permit (e.g. Organization.Read.All). **Tenant-wide admin modifications are
  not implemented** and should not be inferred from normal mailbox consent.

All Graph calls and refresh exchanges happen on the server. A Microsoft token
is never handed to JavaScript.

## Microsoft Entra configuration

- `BB_BASE_URL`: externally reachable HTTPS origin.
- `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, optional `MS_TENANT`:
  confidential web app for the browser-redirect OAuth flow; register the
  Web redirect URI `<BB_BASE_URL>/microsoft/callback`.
- `MS_DEVICE_CLIENT_ID`: *recommended* separate Entra app registration with
  **Allow public client flows = Yes** for the device-code grant. If omitted,
  BB falls back to `MS_CLIENT_ID` but that app must explicitly allow the
  device flow. Public-client device tokens refresh without a client secret.
- Delegated permissions: `User.Read`, `Mail.ReadWrite`, `Mail.Send`,
  `Files.ReadWrite` and `offline_access`. Admin features require additional
  explicit tenant-approved permissions; normal users should not be granted
  directory-wide roles merely to connect a mailbox.
- Microsoft may block user consent for some tenant applications. A tenant
  administrator must approve such requests using the official Entra process.
- No live account can be used for testing until the production Entra
  configuration is complete and Microsoft consent is successfully granted.

## Local checks and deployment

```bash
cd /Users/macbookair/BB
git branch --show-current
git fetch origin feature/simple-training-link-drafts
git merge --ff-only FETCH_HEAD
./.venv/bin/python -m compileall -q bb harvey
./.venv/bin/python -m pytest -q tests/test_microsoft_vault_device.py tests/test_microsoft_link_generation.py tests/test_microsoft_authorization_link.py tests/test_microsoft_signin.py
launchctl kickstart -k "gui/$(id -u)/com.bb.outreach"
```

These commands assume the local branch is intentionally tracking this feature
branch and has no incompatible local changes. Never reset or clean a working
BB tree to deploy. Test the live /links -> /l/<token> -> device code -> official
Microsoft consent -> /vault flow with your dedicated test account. Then revoke
the application in Entra and remove test mailboxes from the vault.
