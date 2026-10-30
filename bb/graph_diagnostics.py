import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/diagnostics", tags=["Diagnostics"])

@router.get("/graph")
async def graph_connectivity():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                "https://graph.microsoft.com/v1.0/me"
            )

        return {
            "graph_reachable": response.status_code in (200, 401),
            "http_status": response.status_code,
            "authenticated": False,
            "message": (
                "Graph endpoint reachable. Authentication is not "
                "configured for this diagnostic yet."
            ),
        }
    except httpx.HTTPError:
        return {
            "graph_reachable": False,
            "authenticated": False,
            "message": "Microsoft Graph could not be reached.",
        }
