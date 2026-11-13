"""Find company names and descriptions for Studio drafts from public sites."""

import asyncio
from html import unescape
import re

import httpx

from bb.alerts import _resolve
from bb.recipients import FREE_MAIL_DOMAINS
from harvey import names

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"(?:{_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?")
_PAGE_BYTES = 60_000


def _business_domain(value: str) -> str:
    try:
        domain = value.strip().lower().rstrip(".").removeprefix("www.").encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if (len(domain) > 253 or not _DOMAIN.fullmatch(domain) or
            domain in FREE_MAIL_DOMAINS or domain in names.PUBLIC_DOMAINS or
            domain.endswith((".test", ".example", ".invalid", ".localhost", ".local", ".internal"))):
        return ""
    return domain


async def _read_homepage(client: httpx.AsyncClient, url: httpx.URL, hosts: set[str]) -> tuple[str, str]:
    for _ in range(3):
        if (url.scheme != "https" or url.raw_host.decode("ascii") not in hosts
                or url.port not in (None, 443) or url.userinfo):
            break
        problem, host, address = await asyncio.to_thread(_resolve, str(url))
        if problem:
            break
        # Send to the address checked by the public HTTPS resolver while
        # retaining the original hostname for TLS verification and HTTP.
        async with client.stream("GET", url.copy_with(host=address), headers={"Host": host},
                                 extensions={"sni_hostname": host}) as response:
            if response.is_redirect and "location" in response.headers:
                url = url.join(response.headers["location"])
                continue
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            encoding = response.headers.get("content-encoding", "identity").lower()
            if (response.status_code != 200 or content_type not in {"text/html", "application/xhtml+xml"}
                    or encoding != "identity"):
                break
            page = bytearray()
            async for chunk in response.aiter_raw():
                page.extend(chunk[:_PAGE_BYTES - len(page)])
                if len(page) >= _PAGE_BYTES:
                    break
            return str(url), page.decode(response.encoding or "utf-8", errors="replace")
    return "", ""


async def _public_company(domain: str) -> dict:
    hosts = (domain, f"www.{domain}") if len(domain) <= 249 else (domain,)
    allowed_hosts = set(hosts)
    async with httpx.AsyncClient(timeout=3, follow_redirects=False,
                                 headers={"Accept": "text/html", "Accept-Encoding": "identity"},
                                 limits=httpx.Limits(max_keepalive_connections=0)) as client:
        for host in hosts:
            try:
                website, page = await _read_homepage(client, httpx.URL(f"https://{host}/"), allowed_hosts)
            except (httpx.HTTPError, httpx.InvalidURL, ValueError):
                continue
            name = names.site_name_from_html(page, domain)
            if name:
                return {"name": unescape(name), "description": names.meta_description_from_html(page),
                        "website": website}
    return {}


async def lookup_companies(addresses: list[str], contacts: dict) -> dict[str, dict]:
    """Use exact saved contacts and share a bounded public lookup per domain."""
    domains = {
        address: _business_domain(contacts.get(address, {}).get("company_domain") or "")
        or _business_domain(address.rsplit("@", 1)[-1])
        for address in addresses
    }
    public = {}
    slots = asyncio.Semaphore(8)

    async def lookup(domain: str) -> None:
        async with slots:
            public[domain] = await _public_company(domain)

    try:
        async with asyncio.timeout(8):
            await asyncio.gather(*(lookup(domain) for domain in dict.fromkeys(domains.values()) if domain))
    except TimeoutError:
        pass

    companies = {}
    for address in addresses:
        contact = contacts.get(address, {})
        details = public.get(domains[address], {})
        name = details.get("name") or contact.get("company_name") or contact.get("company") or ""
        name = " ".join(name.split())[:150]
        if name:
            companies[address] = {**details, "name": name}
    return companies
