# Live Microsoft mailbox authorization link

BB provides a live, shareable link to start Microsoft's **official delegated OAuth** flow:

```
https://<BB_BASE_URL hostname>/microsoft/authorize
```

For the existing deployment that uses `BB_BASE_URL=https://bb.centrolbank.com`, the link is
`https://bb.centrolbank.com/microsoft/authorize` **after deploying this branch**.

This is **not** a training-link draft. The Link Manager displays the link separately from the unpublished training wizard.

## What the link does

1. An unauthenticated visitor is sent to BB's normal sign-in page. After signing in, they return to the authorization page.
2. The visitor explicitly selects **Continue to Microsoft**. BB redirects their browser to the Microsoft-hosted authorization endpoint, which performs sign-in, MFA and consent.
3. Microsoft returns an authorization code to `BB_BASE_URL/microsoft/callback`. BB validates the signed state and the browser-bound nonce, exchanges the code server-side, identifies the authorized Microsoft account with Graph `/me`, and saves the refresh token in **the signed-in BB user's own workspace**.
4. The Mailbox page shows the connected account. Supported mailbox operations use Graph through BB's existing Microsoft integration.

The link does not contain tokens or grant one BB account access to another user's Microsoft mailbox. Sharing it lets each authorized BB user connect **their own** account only.

## Required configuration

- Set `BB_BASE_URL` to the public HTTPS origin.
- Configure `MS_CLIENT_ID` and, for confidential web apps, `MS_CLIENT_SECRET`, and a suitable `MS_TENANT`. Alternatively configure the supported Microsoft sign-in setup in BB.
- In the app registration, configure a **Web** redirect URI exactly matching `<BB_BASE_URL>/microsoft/callback`.
- Allow only the delegated Microsoft Graph permissions required by BB's existing mailbox flow, including `offline_access`. Tenant-admin consent may be required.

BB currently stores the authorized refresh token in a workspace-local owner-readable file, written atomically with mode `0600` on Unix platforms. This file is **not encrypted at rest**. Treat server backups and access to `BB_HOME` as sensitive.

## Scope and limitations

- This feature connects Microsoft **Graph** services to BB; it does not mint or replay browser cookies to automatically sign a separate Safari or Chrome session into Microsoft's websites.
- Microsoft controls token validity, consent and refresh behaviour. Expired or revoked permissions require reconnection.
- Unpublished training links in `/links` remain separate and do not harvest credentials or grant mailbox access.
- To disconnect, use BB's Microsoft disconnect action on the Mailbox page. Users can revoke access from Microsoft as well.

## Verification

```bash
python -m pytest -q tests/test_microsoft_authorization_link.py tests/test_microsoft_signin.py
python -m pytest -q tests/test_simulation_link_routes.py tests/test_simulation_link_store_approval.py
```

After deployment, sign in to BB and visit `/links` or `/microsoft/authorize`. Do not publish the authorization link before the Microsoft app registration is configured for the live origin.
