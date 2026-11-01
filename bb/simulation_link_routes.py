"""Authenticated management pages for authorized training links."""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bb.simulation_link_domains import domain_options, eligible_hostnames
from bb.simulation_link_store import create_link, list_links, set_link_status
from bb.simulation_links import is_expired


def register_simulation_link_routes(
    app,
    settings,
    current_user,
    base_workspace,
    render,
):
    """Register read-only, authenticated Link Manager routes."""

    @app.get("/links", response_class=HTMLResponse)
    async def simulation_links_page(
        request: Request,
        user: dict = Depends(current_user),
    ):
        workspace = base_workspace(user)
        records = list_links(workspace.user_data_dir)

        links = []

        for record in records:
            item = dict(record)

            try:
                item["expired"] = is_expired(
                    item.get("expires_at")
                )
            except (ValueError, TypeError):
                item["expired"] = True

            links.append(item)

        active_count = sum(
            1 for item in links
            if item.get("status") == "active"
            and not item["expired"]
        )

        return await render(
            request,
            "simulation_links.html",
            user,
            links=links,
            active_count=active_count,
            domain_options=domain_options(
                workspace.user_data_dir,
                settings.base_url,
            ),
            eligible_link_hostnames=eligible_hostnames(
                workspace.user_data_dir,
                settings.base_url,
            ),
            approved_destination_hostnames=(
                settings.training_destination_hostnames
            ),
        )

    @app.post("/links/create")
    async def simulation_links_create(
        request: Request,
        user: dict = Depends(current_user),
    ):
        """Create a workspace-scoped training configuration record."""
        from urllib.parse import quote, urlparse

        source_header = (
            request.headers.get("origin")
            or request.headers.get("referer")
        )

        if not source_header:
            raise HTTPException(403, "Request origin required.")

        source = urlparse(source_header)
        target = urlparse(settings.base_url)

        if (
            source.scheme != target.scheme
            or source.netloc != target.netloc
            or source.scheme not in ("http", "https")
        ):
            raise HTTPException(403, "Request origin not allowed.")

        form = await request.form()
        workspace = base_workspace(user)

        try:
            create_link(
                workspace.user_data_dir,
                name=str(form.get("name", "")),
                hostname=str(form.get("hostname", "")),
                path=str(form.get("path", "")),
                destination=str(form.get("destination", "")),
                allowed_hostnames=eligible_hostnames(
                    workspace.user_data_dir,
                    settings.base_url,
                ),
                approved_destination_hostnames=(
                    settings.training_destination_hostnames
                ),
                expires_at=(
                    str(form.get("expires_at", "")).strip() or None
                ),
            )
        except ValueError as exc:
            return RedirectResponse(
                "/links?error=" + quote(str(exc)),
                status_code=303,
            )

        return RedirectResponse(
            "/links?msg=" + quote(
                "Training link configuration saved. "
                "Public routing remains disabled."
            ),
            status_code=303,
        )

    @app.post("/links/status")
    async def simulation_links_status(
        request: Request,
        user: dict = Depends(current_user),
    ):
        """Update only the signed-in user's saved record status."""
        from urllib.parse import quote, urlparse

        source_header = (
            request.headers.get("origin")
            or request.headers.get("referer")
        )

        if not source_header:
            raise HTTPException(403, "Request origin required.")

        source = urlparse(source_header)
        target = urlparse(settings.base_url)

        if (
            source.scheme not in ("http", "https")
            or source.scheme != target.scheme
            or source.netloc != target.netloc
        ):
            raise HTTPException(403, "Request origin not allowed.")

        form = await request.form()
        link_id = str(form.get("link_id", "")).strip()
        status = str(form.get("status", "")).strip()

        if not link_id or status not in ("active", "disabled"):
            raise HTTPException(400, "Invalid status update.")

        workspace = base_workspace(user)

        try:
            set_link_status(
                workspace.user_data_dir,
                link_id,
                status,
            )
        except ValueError as exc:
            return RedirectResponse(
                "/links?error=" + quote(str(exc)),
                status_code=303,
            )

        return RedirectResponse(
            "/links?msg=" + quote(
                "Training link configuration updated. "
                "Public routing remains disabled."
            ),
            status_code=303,
        )
