"""Does the public link actually work? The site asks itself through the
internet: a GET of /health at the public address. Cloudflare's own error
pages (1033: tunnel not connected, 530) are told apart from a dead
hostname and from a site that answers but is not this one."""

import time

import httpx

_cache: dict = {}
CACHE_SECONDS = 30


async def check_public_url(url: str, use_cache: bool = True) -> dict:
    """{ok, detail, checked_at}. Never raises."""
    url = (url or "").rstrip("/")
    now = time.time()
    if use_cache and url in _cache and now - _cache[url]["checked_at"] < CACHE_SECONDS:
        return _cache[url]
    out = {"ok": False, "detail": "", "checked_at": now, "url": url}
    if not url:
        out["detail"] = "no public address is set"
    elif "localhost" in url or "127.0.0.1" in url:
        out["detail"] = "the address is this computer only; other people cannot open it"
    else:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(url + "/health")
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json") and r.json().get("ok"):
                out.update(ok=True, detail="reachable from the internet and answering")
            elif "1016" in r.text or "Origin DNS error" in r.text:
                out["detail"] = ("Cloudflare no longer knows this address (error 1016): the quick tunnel that created it has stopped, "
                                 "so this link is dead; the current link is the one on the Users page")
            elif r.status_code in (530, 1033) or "1033" in r.text or "Cloudflare Tunnel error" in r.text:
                out["detail"] = "the address exists but the tunnel behind it is not connected (Cloudflare error 1033); the tunnel process is down or pointing at another port"
            elif r.status_code == 404 and "cloudflare" in r.text.lower():
                out["detail"] = "Cloudflare answers but no tunnel owns this hostname any more (a quick tunnel from an earlier start)"
            else:
                out["detail"] = f"something answers at this address but not this site (HTTP {r.status_code})"
        except httpx.ConnectError as e:
            msg = str(e).lower()
            if "name" in msg or "resolve" in msg or "nodename" in msg or "getaddrinfo" in msg:
                out["detail"] = "the hostname does not exist any more (a quick tunnel's address dies when the tunnel restarts)"
            else:
                out["detail"] = f"could not connect: {str(e)[:120]}"
        except httpx.HTTPError as e:
            out["detail"] = f"could not reach it: {type(e).__name__}: {str(e)[:120]}"
    _cache[url] = out
    return out
