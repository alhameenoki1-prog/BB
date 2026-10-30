from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

TEMPLATE = Path(__file__).resolve().parent / "templates" / "graph_test.html"


@router.get("/diagnostics/microsoft", response_class=HTMLResponse)
def microsoft_test_page():
    return TEMPLATE.read_text(encoding="utf-8")
