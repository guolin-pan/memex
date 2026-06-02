"""`memex client` — thin HTTP client that LLMs/agents can shell out to.

Mirrors the local CLI surface for the operations agents actually need:

  memex client status
  memex client ctx "<query>" [--budget N] [--write PATH]
  memex client doc {add|search|ls|show|rm}
  memex client mem {add|search|ls|show|rm|profile}

All commands accept `--url` (or env `MEMEX_API_URL`) and `--token` (or env
`MEMEX_API_TOKEN`). Designed to be safe in subagent shell sandboxes — no
local filesystem reads, no chroma/mem0 imports.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Talk to a remote memex API (for agents / Docker deployments).")
doc_app = typer.Typer(help="Remote wiki operations.")
mem_app = typer.Typer(help="Remote memory operations.")
app.add_typer(doc_app, name="doc")
app.add_typer(mem_app, name="mem")

console = Console()
err_console = Console(stderr=True)

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 60.0


def _http():
    """Lazy-import httpx and return a configured client.

    Reads URL + token from env or kwargs. We instantiate per-call so the
    server URL can change between commands in the same shell session.
    """
    import httpx

    url = os.environ.get("MEMEX_API_URL", DEFAULT_URL).rstrip("/")
    token = os.environ.get("MEMEX_API_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(
        base_url=url, headers=headers, timeout=DEFAULT_TIMEOUT, follow_redirects=True
    )


def _die(resp) -> None:
    """Print a clean error and exit with code 1."""
    try:
        body = resp.json()
        msg = body.get("detail") or body.get("error") or json.dumps(body)
    except Exception:
        msg = resp.text or f"HTTP {resp.status_code}"
    err_console.print(f"[red]error {resp.status_code}:[/red] {msg}")
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Top-level: status, ctx, raw
# ---------------------------------------------------------------------------


@app.command("status")
def status_cmd(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
):
    """Show the remote memex's status."""
    with _http() as c:
        r = c.get("/status")
        if r.status_code != 200:
            _die(r)
        data = r.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    t = Table(show_header=False, box=None)
    t.add_column("k", style="bold")
    t.add_column("v")
    for k, v in data.items():
        t.add_row(k, str(v))
    console.print(t)


@app.command("ctx")
def ctx_cmd(
    query: str = typer.Argument("", help="The user's current prompt / topic."),
    budget: int = typer.Option(None, "--budget", help="Token budget (default from server config)."),
    top_k_docs: int = typer.Option(None, "-k", "--top-k-docs"),
    top_k_mems: int = typer.Option(None, "--top-k-mems"),
    no_profile: bool = typer.Option(False, "--no-profile"),
    no_memories: bool = typer.Option(False, "--no-memories"),
    no_docs: bool = typer.Option(False, "--no-docs"),
    write: Path = typer.Option(None, "--write", help="Write the block to this path."),
):
    """Build a unified context block on the server side."""
    payload: dict = {
        "query": query,
        "include_profile": not no_profile,
        "include_memories": not no_memories,
        "include_docs": not no_docs,
    }
    if budget is not None:
        payload["budget"] = budget
    if top_k_docs is not None:
        payload["top_k_docs"] = top_k_docs
    if top_k_mems is not None:
        payload["top_k_mems"] = top_k_mems

    with _http() as c:
        r = c.post("/ctx", json=payload)
        if r.status_code != 200:
            _die(r)
        data = r.json()

    block = data["block"]
    if write:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(block, encoding="utf-8")
        console.print(
            f"[green]✓[/green] wrote ctx ({data.get('tokens', 0)} tokens) → {write}",
            style="dim",
        )
    else:
        typer.echo(block)


@app.command("raw")
def raw_cmd(
    method: str = typer.Argument(..., help="HTTP method (GET/POST/DELETE/...)."),
    path: str = typer.Argument(..., help="API path, e.g. /healthz or /doc/search?q=…"),
    body: str = typer.Option(None, "--body", help="JSON body for POST/PUT (string)."),
):
    """Make an arbitrary HTTP call. Useful for debugging."""
    json_body = None
    if body:
        try:
            json_body = json.loads(body)
        except json.JSONDecodeError as e:
            err_console.print(f"[red]error:[/red] --body is not valid JSON: {e}")
            raise typer.Exit(2) from e
    with _http() as c:
        r = c.request(method.upper(), path, json=json_body)
    typer.echo(f"HTTP {r.status_code}")
    try:
        typer.echo(json.dumps(r.json(), indent=2, ensure_ascii=False))
    except Exception:
        typer.echo(r.text)


# ---------------------------------------------------------------------------
# doc subcommands
# ---------------------------------------------------------------------------


@doc_app.command("add")
def doc_add(
    source: str = typer.Argument("-", help='Path to a .md file, or "-" to read stdin.'),
    title: str = typer.Option(None, "--title", "-t"),
    tags: str = typer.Option("", "--tags"),
    subdir: str = typer.Option("inbox", "--subdir", "-d"),
):
    """Add a markdown doc on the remote server."""
    if source == "-":
        body = sys.stdin.read()
        if not body.strip():
            err_console.print("[red]error:[/red] empty stdin")
            raise typer.Exit(2)
    else:
        p = Path(source).expanduser().resolve()
        if not p.exists():
            err_console.print(f"[red]error:[/red] no such file: {p}")
            raise typer.Exit(2)
        body = p.read_text(encoding="utf-8")

    payload = {
        "body": body,
        "title": title,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "subdir": subdir,
    }
    with _http() as c:
        r = c.post("/doc/add", json=payload)
        if r.status_code != 200:
            _die(r)
        data = r.json()
    console.print(f"[green]✓[/green] added [bold]{data['title']}[/bold]")
    console.print(f"  id:   {data['id']}")
    console.print(f"  path: {data['path']}")
    console.print(f"  tags: {', '.join(data['tags']) or '(none)'}")


@doc_app.command("search")
def doc_search(
    query: str = typer.Argument(...),
    top_k: int = typer.Option(None, "-k", "--top-k"),
    tag: str = typer.Option(None, "--tag"),
    since: str = typer.Option(None, "--since"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Hybrid search over the remote wiki."""
    params: dict = {"q": query}
    if top_k is not None:
        params["k"] = top_k
    if tag:
        params["tag"] = tag
    if since:
        params["since"] = since
    with _http() as c:
        r = c.get("/doc/search", params=params)
        if r.status_code != 200:
            _die(r)
        data = r.json()
    hits = data.get("hits", [])
    if as_json:
        typer.echo(json.dumps(hits, indent=2, ensure_ascii=False))
        return
    if not hits:
        console.print("[yellow](no hits)[/yellow]")
        return
    for h in hits:
        console.print(
            f"[bold cyan]{h['title']}[/bold cyan]  "
            f"[dim]({h['heading']})  score={h['score']:.3f}[/dim]"
        )
        console.print(f"  [dim]{h['path']}  • tags: {', '.join(h['tags']) or '-'}[/dim]")
        for line in (h.get("text") or "").splitlines():
            console.print(f"  > {line}")
        console.print()


@doc_app.command("ls")
def doc_ls(
    tag: str = typer.Option(None, "--tag"),
    since: str = typer.Option(None, "--since"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List docs on the remote server."""
    params: dict = {}
    if tag:
        params["tag"] = tag
    if since:
        params["since"] = since
    with _http() as c:
        r = c.get("/doc", params=params)
        if r.status_code != 200:
            _die(r)
        docs = r.json().get("docs", [])
    if as_json:
        typer.echo(json.dumps(docs, indent=2, ensure_ascii=False))
        return
    if not docs:
        console.print("[yellow](no documents)[/yellow]")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("id", style="dim")
    t.add_column("title")
    t.add_column("tags")
    t.add_column("updated", style="dim")
    t.add_column("path", overflow="fold")
    for d in docs:
        t.add_row(d["id"][-12:], d["title"], ", ".join(d.get("tags") or []), d.get("updated", ""), d["path"])
    console.print(t)


@doc_app.command("show")
def doc_show(ident: str = typer.Argument(...)):
    """Show one doc by id, slug, or path."""
    with _http() as c:
        r = c.get(f"/doc/{ident}")
        if r.status_code != 200:
            _die(r)
        d = r.json()
    console.print(f"[bold cyan]{d['title']}[/bold cyan]  [dim]({d['id']})[/dim]")
    console.print(f"[dim]{d['path']}  • tags: {', '.join(d.get('tags') or []) or '-'}[/dim]")


@doc_app.command("rm")
def doc_rm(
    ident: str = typer.Argument(...),
    keep_file: bool = typer.Option(False, "--keep-file"),
):
    """Remove a doc on the remote."""
    with _http() as c:
        r = c.delete(f"/doc/{ident}", params={"keep_file": keep_file})
        if r.status_code != 200:
            _die(r)
        data = r.json()
    console.print(f"[green]✓[/green] removed {data['id']}")


# ---------------------------------------------------------------------------
# mem subcommands
# ---------------------------------------------------------------------------


@mem_app.command("add")
def mem_add(
    text: str = typer.Argument(...),
    category: str = typer.Option("fact", "--category", "-c"),
    tag: list[str] = typer.Option(None, "--tag"),
    infer: bool = typer.Option(
        False,
        "--infer/--no-infer",
        help="Server-side LLM extraction (default: off; verbatim insert).",
    ),
):
    """Add a memory on the remote server."""
    payload = {
        "text": text,
        "category": category,
        "tags": list(tag or []),
        "infer": infer,
    }
    with _http() as c:
        r = c.post("/mem/add", json=payload)
        if r.status_code != 200:
            _die(r)
        data = r.json()
    ids = data.get("ids", [])
    if not ids:
        console.print("[yellow]mem0 returned no new ids (likely deduped)[/yellow]")
    else:
        for mid in ids:
            console.print(f"[green]✓[/green] stored {mid}  [dim]({category})[/dim]")


@mem_app.command("search")
def mem_search(
    query: str = typer.Argument(...),
    top_k: int = typer.Option(5, "-k", "--top-k"),
    category: str = typer.Option(None, "--category", "-c"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Semantic search over remote memories."""
    params: dict = {"q": query, "k": top_k}
    if category:
        params["category"] = category
    with _http() as c:
        r = c.get("/mem/search", params=params)
        if r.status_code != 200:
            _die(r)
        items = r.json().get("memories", [])
    if as_json:
        typer.echo(json.dumps(items, indent=2, ensure_ascii=False))
        return
    if not items:
        console.print("[yellow](no memories)[/yellow]")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("id", style="dim")
    t.add_column("cat")
    t.add_column("score")
    t.add_column("text", overflow="fold")
    for m in items:
        t.add_row(m["id"][-12:], m["category"], f"{m.get('score', 0):.3f}", m["text"])
    console.print(t)


@mem_app.command("ls")
def mem_ls(
    category: str = typer.Option(None, "--category", "-c"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List remote memories."""
    params: dict = {}
    if category:
        params["category"] = category
    with _http() as c:
        r = c.get("/mem", params=params)
        if r.status_code != 200:
            _die(r)
        items = r.json().get("memories", [])
    if as_json:
        typer.echo(json.dumps(items, indent=2, ensure_ascii=False))
        return
    if not items:
        console.print("[yellow](no memories)[/yellow]")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("id", style="dim")
    t.add_column("cat")
    t.add_column("text", overflow="fold")
    for m in items:
        t.add_row(m["id"][-12:], m["category"], m["text"])
    console.print(t)


@mem_app.command("show")
def mem_show(mem_id: str = typer.Argument(...)):
    """Show one memory by id."""
    with _http() as c:
        r = c.get(f"/mem/{mem_id}")
        if r.status_code != 200:
            _die(r)
        typer.echo(json.dumps(r.json(), indent=2, ensure_ascii=False))


@mem_app.command("rm")
def mem_rm(mem_id: str = typer.Argument(..., help="Memory id, or 'all' to wipe.")):
    """Delete a memory, or wipe everything for this user_id."""
    with _http() as c:
        r = c.delete(f"/mem/{mem_id}")
        if r.status_code != 200:
            _die(r)
    console.print(f"[green]✓[/green] deleted {mem_id}")


@mem_app.command("profile")
def mem_profile(
    max_items: int = typer.Option(20, "--max-items"),
    write: Path = typer.Option(None, "--write", help="Write block to this path."),
):
    """Render the 'About the user' block from the remote server."""
    with _http() as c:
        r = c.get("/mem/profile", params={"max_items": max_items})
        if r.status_code != 200:
            _die(r)
        data = r.json()
    block = data["block"]
    if write:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(block, encoding="utf-8")
        console.print(
            f"[green]✓[/green] wrote profile ({data.get('count', 0)} items) → {write}",
            style="dim",
        )
    else:
        typer.echo(block)
