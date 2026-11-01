"""Authenticated management pages for authorized training links."""

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from bb.simulation_link_domains import domain_options
from bb.simulation_link_store import list_links
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
        )
