"""Shared UI primitives for every HTML page in the GamerAI stack.

One source of truth for tokens (colors, spacing, radius), the topbar,
buttons, links, tables, and the mobile-first responsive defaults.
Every page template imports ``BASE_CSS`` and ``VIEWPORT_META`` from
here rather than redefining its own. Page-specific styles still live
inside each template's own ``<style>`` block — but only the things
that ARE specific to that page.

Mobile-first rule: defaults target small screens (single column,
full-width controls, smaller paddings). Larger screens get refinements
via a single ``@media (min-width: 640px)`` breakpoint.

Templates that need runtime substitutions use ``string.Template`` so
their CSS braces don't collide with ``str.format`` placeholders.
"""

VIEWPORT_META = (
    '<meta name="viewport" '
    'content="width=device-width,initial-scale=1,viewport-fit=cover">'
)


# Layout breakpoint. Anything above this is "comfortable desktop";
# below is phone / cramped tablet. Single breakpoint keeps the CSS
# tractable; nothing in the product needs a fancy responsive grid.
_BREAKPOINT = "640px"


BASE_CSS = f"""
:root {{
  --brand: #2d6cdf;
  --brand-hover: #1f55b8;
  --text: #1a1a1a;
  --muted: #666;
  --surface: #ffffff;
  --bg: #f7f7f8;
  --border: #e5e5e5;
  --border-soft: #f0f0f0;
  --radius: 6px;
  --err-bg: #fde0e0;
  --err-border: #f5b0b0;
  --err-text: #900;
  --code-bg: #f3f3f3;
  --mono: ui-monospace, Menlo, Consolas, monospace;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
  color: var(--text);
  background: var(--bg);
  line-height: 1.5;
  -webkit-text-size-adjust: 100%;
}}
a {{ color: var(--brand); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
h1, h2, h3 {{ line-height: 1.25; }}
h1 {{ font-size: 1.4rem; margin: 0 0 .5rem; }}
h2 {{ font-size: 1.1rem; margin: 1.5rem 0 .5rem; }}
code {{
  background: var(--code-bg); padding: .05rem .3rem; border-radius: 3px;
  font-size: .9em; font-family: var(--mono);
}}
.muted {{ color: var(--muted); font-size: .9rem; }}
.page {{ max-width: 720px; margin: 1.5rem auto; padding: 0 1rem; }}
.card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 1rem 1.1rem;
  margin-bottom: 1rem;
}}
.alert-err {{
  background: var(--err-bg); border: 1px solid var(--err-border);
  color: var(--err-text); padding: .6rem .9rem;
  border-radius: var(--radius); margin-bottom: 1rem;
}}
table {{
  width: 100%; border-collapse: collapse; font-size: .9rem;
  /* tables get a horizontal scroll on phones so a wide admin grid
     doesn't blow out the layout */
  display: block; overflow-x: auto;
}}
th, td {{
  padding: .5rem .55rem; border-bottom: 1px solid var(--border);
  text-align: left; vertical-align: top; white-space: nowrap;
}}
th {{ background: #f5f5f5; font-weight: 600; }}
/* Buttons and form controls scale to full width by default — looks
   right on a phone, gets capped on desktop via the breakpoint below. */
button, .btn {{
  display: inline-block; padding: .6rem 1rem; font-size: 1rem;
  background: var(--brand); color: #fff; border: 0;
  border-radius: var(--radius); cursor: pointer;
  font-family: inherit; line-height: 1.3;
}}
.btn {{ text-decoration: none; }}
.btn:hover, button:hover {{ background: var(--brand-hover); }}
button:disabled, .btn[disabled] {{
  background: #aaa; cursor: not-allowed;
}}
.btn-quiet {{
  background: var(--surface); color: var(--text);
  border: 1px solid var(--border);
}}
.btn-quiet:hover {{ background: #f5f5f5; }}
input[type=text], input[type=email], input[type=password], textarea {{
  width: 100%; padding: .6rem; font-size: 1rem;
  font-family: inherit; line-height: 1.4;
  border: 1px solid #ccc; border-radius: var(--radius);
}}
input:focus, textarea:focus {{ outline: 0; border-color: var(--brand); }}
.topbar {{
  display: flex; justify-content: space-between; align-items: center;
  padding: .55rem .9rem; background: var(--surface);
  border-bottom: 1px solid var(--border); font-size: .9rem;
  gap: .5rem;
}}
.topbar .brand {{ font-weight: 600; color: var(--text); }}
.topbar .topbar-actions {{ display: flex; gap: .9rem; align-items: center; }}
.topbar a {{ color: var(--brand); }}
/* On phones, hide non-essential topbar items; admin/terms/logout still
   reachable from the dashboard. Override per page if needed. */
.topbar .hide-mobile {{ display: none; }}

@media (min-width: {_BREAKPOINT}) {{
  .page {{ max-width: 760px; margin: 2.5rem auto; padding: 0 1.5rem; }}
  h1 {{ font-size: 1.6rem; }}
  h2 {{ font-size: 1.25rem; }}
  table {{ display: table; overflow: visible; }}
  .topbar {{ padding: .55rem 1.25rem; }}
  .topbar .hide-mobile {{ display: inline; }}
  /* Buttons inside .topbar / forms stop trying to be full-width once
     we have horizontal room. */
  button.full, .btn.full {{ width: auto; }}
}}
"""
