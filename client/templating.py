"""Single Jinja2Templates instance shared by every route module.

Importing this module is what wires templates to ``client/templates/``.
Keeping it in its own file avoids a circular import between routes
(which need the instance) and ``client/app.py`` (which mounts the
routes).
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def _epoch_date(value) -> str:
    """Format a unix timestamp as a UTC ``YYYY-MM-DD`` date. Empty string
    for falsy/unparseable input so templates can use it unguarded."""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


templates.env.filters["epoch_date"] = _epoch_date
