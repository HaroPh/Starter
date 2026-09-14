"""The landing page.

This is the URL verify.sh curls, so it must return 200 under every condition the reviewer
can produce: an empty database, an import still running, or an import that failed.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "pages/home.html", {"title": "Overview"})
