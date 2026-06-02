"""`memex doc *` — CRUD + search + reindex + watch over the markdown wiki."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from memex.core.config import load_config
from memex.core.utils import truncate_to_tokens
from memex.core.wiki import Wiki

app = typer.Typer(help="Manage the markdown wiki (add/update/rm/search/list/watch).")

console = Console()
err_console = Console(stderr=True)


def _wiki(ctx: typer.Context) -> Wiki:
    cfg = load_config(ctx.obj.get("root") if ctx.obj else None)
    return Wiki(cfg)


# ---------------------------------------------------------------------------
# add / update / rm
# ---------------------------------------------------------------------------


@app.command("add")
def add(
    ctx: typer.Context,
    source: str = typer.Argument(
        "-",
        help='Path to a .md file, or "-" to read from stdin.',
    ),
    title: str = typer.Option(None, "--title", "-t", help="Document title."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    subdir: str = typer.Option(
        "inbox", "--subdir", "-d", help="Subdirectory under docs/ to land in."
    ),
    open_after: bool = typer.Option(False, "--open", help="Open in $EDITOR after creating."),
):
    """Add a markdown document into the wiki and index it."""
    wiki = _wiki(ctx)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    if source == "-":
        body = sys.stdin.read()
        if not body.strip():
            err_console.print("[red]error:[/red] empty stdin")
            raise typer.Exit(2)
        doc = wiki.add(
            source_path=None,
            body=body,
            title=title,
            tags=tag_list,
            target_subdir=subdir,
            source="chat" if not sys.stdin.isatty() else "manual",
        )
    else:
        src_path = Path(source).expanduser().resolve()
        if not src_path.exists():
            err_console.print(f"[red]error:[/red] no such file: {src_path}")
            raise typer.Exit(2)
        doc = wiki.add(
            source_path=src_path,
            body=None,
            title=title,
            tags=tag_list,
            target_subdir=subdir,
        )

    console.print(f"[green]✓[/green] added [bold]{doc.title}[/bold]")
    console.print(f"  id:   {doc.id}")
    console.print(f"  path: {doc.path}")
    console.print(f"  tags: {', '.join(doc.tags) or '(none)'}")

    if open_after:
        editor = os.environ.get("EDITOR", "vi")
        subprocess.call([editor, str(doc.path)])


@app.command("update")
def update(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="Path to a markdown file inside docs/."),
):
    """Re-index a single document after manual edits."""
    wiki = _wiki(ctx)
    doc = wiki.update_path(path.expanduser().resolve())
    if doc is None:
        err_console.print(f"[red]error:[/red] not a wiki markdown file: {path}")
        raise typer.Exit(2)
    console.print(f"[green]✓[/green] reindexed [bold]{doc.title}[/bold] ({doc.id})")


@app.command("rm")
def rm(
    ctx: typer.Context,
    ident: str = typer.Argument(..., help="Document id, slug, or path."),
    keep_file: bool = typer.Option(False, "--keep-file", help="Drop from index only."),
):
    """Delete a document from disk and the index."""
    wiki = _wiki(ctx)
    try:
        doc_id, removed_path = wiki.remove(ident, delete_file=not keep_file)
    except FileNotFoundError as e:
        err_console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2) from e
    console.print(f"[green]✓[/green] removed {doc_id}")
    if removed_path:
        action = "kept" if keep_file else "deleted"
        console.print(f"  file {action}: {removed_path}")


@app.command("edit")
def edit(
    ctx: typer.Context,
    ident: str = typer.Argument(..., help="Document id, slug, or path."),
):
    """Open the doc in $EDITOR and re-index on save."""
    wiki = _wiki(ctx)
    doc = wiki.get(ident)
    if doc is None:
        err_console.print(f"[red]error:[/red] no such document: {ident}")
        raise typer.Exit(2)
    editor = os.environ.get("EDITOR", "vi")
    subprocess.call([editor, str(doc.path)])
    wiki.update_path(doc.path)
    console.print(f"[green]✓[/green] reindexed {doc.title}")


# ---------------------------------------------------------------------------
# reindex / watch
# ---------------------------------------------------------------------------


@app.command("reindex")
def reindex(
    ctx: typer.Context,
    all_: bool = typer.Option(False, "--all", help="Force-rebuild every doc."),
    changed: bool = typer.Option(
        False, "--changed", help="(default) Only re-embed docs whose content_hash differs."
    ),
):
    """Walk docs/ and bring the Chroma index in sync."""
    if all_ and changed:
        err_console.print("[red]error:[/red] --all and --changed are mutually exclusive")
        raise typer.Exit(2)
    wiki = _wiki(ctx)
    res = wiki.reindex(only_changed=not all_)
    console.print(
        f"[green]✓[/green] added={len(res.added)} updated={len(res.updated)} "
        f"skipped={len(res.skipped)} deleted={len(res.deleted)}"
    )
    if res.deleted:
        for doc_id in res.deleted:
            console.print(f"  - {doc_id} (removed, file no longer present)")


@app.command("watch")
def watch(
    ctx: typer.Context,
    debounce: float = typer.Option(1.0, "--debounce", help="Coalesce events for N seconds."),
):
    """Watch docs/ and keep the index in sync continuously."""
    from memex.integrations.watcher import run_watcher

    wiki = _wiki(ctx)
    console.print(f"[cyan]watching[/cyan] {wiki.cfg.docs_dir}  (Ctrl-C to stop)")
    try:
        run_watcher(wiki, debounce_seconds=debounce, on_event=_log_event)
    except KeyboardInterrupt:
        console.print("\n[yellow]stopped[/yellow]")


def _log_event(kind: str, target: str) -> None:
    console.print(f"  [dim]{time.strftime('%H:%M:%S')}[/dim] {kind:>8s}  {target}")


# ---------------------------------------------------------------------------
# search / ls / show / graph
# ---------------------------------------------------------------------------


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search query."),
    top_k: int = typer.Option(None, "-k", "--top-k", help="Number of hits."),
    tag: str = typer.Option(None, "--tag", help="Filter by tag."),
    since: str = typer.Option(None, "--since", help="Duration (e.g. 30d) or ISO date."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of markdown."),
    snippet_tokens: int = typer.Option(180, "--snippet-tokens", help="Tokens per snippet."),
):
    """Hybrid (vector + BM25) search over wiki chunks."""
    wiki = _wiki(ctx)
    hits = wiki.search(query, top_k=top_k, tag=tag, since=since)
    if as_json:
        out = [
            {
                "chunk_id": h.chunk_id,
                "doc_id": h.doc_id,
                "title": h.title,
                "path": h.path,
                "heading": h.heading,
                "score": round(h.score, 4),
                "tags": h.tags,
                "updated": h.updated,
                "snippet": truncate_to_tokens(h.text, snippet_tokens),
            }
            for h in hits
        ]
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if not hits:
        console.print("[yellow](no hits)[/yellow]")
        return

    for h in hits:
        console.print(
            f"[bold cyan]{h.title}[/bold cyan]  "
            f"[dim]({h.heading})  score={h.score:.3f}[/dim]"
        )
        console.print(f"  [dim]{h.path}  • tags: {', '.join(h.tags) or '-'}[/dim]")
        snippet = truncate_to_tokens(h.text, snippet_tokens).strip()
        for line in snippet.splitlines():
            console.print(f"  > {line}")
        console.print()


@app.command("ls")
def ls(
    ctx: typer.Context,
    tag: str = typer.Option(None, "--tag", help="Filter by tag."),
    since: str = typer.Option(None, "--since", help="Duration (e.g. 30d) or ISO date."),
    as_json: bool = typer.Option(False, "--json", help="JSON output."),
):
    """List documents in the wiki."""
    wiki = _wiki(ctx)
    docs = wiki.list_docs(tag=tag, since=since)
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "id": d.id,
                        "title": d.title,
                        "path": str(d.path),
                        "tags": d.tags,
                        "updated": d.updated,
                    }
                    for d in docs
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not docs:
        console.print("[yellow](no documents)[/yellow]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id", style="dim")
    table.add_column("title")
    table.add_column("tags")
    table.add_column("updated", style="dim")
    table.add_column("path", overflow="fold")
    for d in docs:
        rel = _relpath(d.path, wiki.cfg.docs_dir)
        table.add_row(d.id[-12:], d.title, ", ".join(d.tags), d.updated, rel)
    console.print(table)


@app.command("show")
def show(
    ctx: typer.Context,
    ident: str = typer.Argument(..., help="Document id, slug, or path."),
    raw: bool = typer.Option(False, "--raw", help="Print raw file contents."),
):
    """Print a document."""
    wiki = _wiki(ctx)
    doc = wiki.get(ident)
    if doc is None:
        err_console.print(f"[red]error:[/red] no such document: {ident}")
        raise typer.Exit(2)
    if raw:
        typer.echo(doc.to_text())
        return
    console.print(f"[bold cyan]{doc.title}[/bold cyan]  [dim]({doc.id})[/dim]")
    console.print(f"[dim]{doc.path}  • tags: {', '.join(doc.tags) or '-'}  • updated: {doc.updated}[/dim]\n")
    typer.echo(doc.body)


@app.command("graph")
def graph(ctx: typer.Context):
    """Emit a mermaid graph of inter-doc links (from frontmatter `links`)."""
    wiki = _wiki(ctx)
    typer.echo(wiki.graph_mermaid())


def _relpath(p: Path, root: Path) -> str:
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p)
