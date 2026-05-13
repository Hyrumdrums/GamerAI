"""Single Jinja2Templates instance shared by every route module.

Importing this module is what wires templates to ``client/templates/``.
Keeping it in its own file avoids a circular import between routes
(which need the instance) and ``client/app.py`` (which mounts the
routes).
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
