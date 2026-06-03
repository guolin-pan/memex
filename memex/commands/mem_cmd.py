"""`memex mem *` — CRUD + search + profile + learn over mem0 OSS."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from memex.backends.mem_store import (
    ALLOWED_CATEGORIES,
    PROFILE_CATEGORIES,
    MemoryItem,
    MemStore,
)
from memex.core.config import load_config

app = typer.Typer(help="Manage personal memory (mem0).")

console = Console()
err_console = Console(stderr=True)


def _store(ctx: typer.Context) -> MemStore:
    cfg = load_config(ctx.obj.get("root") if ctx.obj else None)
    return MemStore(cfg)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@app.command("add")
def add(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="The fact to remember."),
    category: str = typer.Option(
        "fact", "--category", "-c", help=f"One of: {sorted(ALLOWED_CATEGORIES)}"
    ),
    tag: list[str] = typer.Option(None, "--tag", help="Optional tag(s)."),
    infer: bool = typer.Option(
        False,
        "--infer/--no-infer",
        help=(
            "Run mem0's LLM-driven fact extractor over the input "
            "(may split, merge, or dedupe). Off by default: `mem add \"X\"` "
            "stores X verbatim with the supplied category. Use `mem learn` "
            "for transcripts where you DO want LLM extraction."
        ),
    ),
):
    """Add a memory verbatim (default) or via LLM fact-extraction (`--infer`)."""
    store = _store(ctx)
    metadata = {"tags": list(tag or [])} if tag else None
    ids = store.add(text, category=category, metadata=metadata, infer=infer)
    if not ids:
        console.print(
            "[yellow]mem0 did not return new ids "
            "(likely deduped into an existing memory; try --no-infer for verbatim insert)[/yellow]"
        )
    else:
        for mid in ids:
            console.print(f"[green]✓[/green] stored {mid}  [dim]({category})[/dim]")


@app.command("ls")
def ls(
    ctx: typer.Context,
    category: str = typer.Option(None, "--category", "-c", help="Filter by category."),
    as_json: bool = typer.Option(False, "--json", help="JSON output."),
):
    """List all stored memories for the configured user_id."""
    store = _store(ctx)
    items = store.list(category=category)
    if as_json:
        typer.echo(json.dumps([_item_dict(m) for m in items], ensure_ascii=False, indent=2))
        return
    _render_table(items)


@app.command("show")
def show(
    ctx: typer.Context,
    mem_id: str = typer.Argument(..., help="Memory id."),
):
    """Print a single memory by id."""
    store = _store(ctx)
    item = store.get(mem_id)
    if item is None:
        err_console.print(f"[red]error:[/red] no such memory: {mem_id}")
        raise typer.Exit(2)
    console.print(json.dumps(_item_dict(item), ensure_ascii=False, indent=2))


@app.command("update")
def update(
    ctx: typer.Context,
    mem_id: str = typer.Argument(..., help="Memory id."),
    text: str = typer.Argument(..., help="New text."),
):
    """Replace a memory's text by id."""
    store = _store(ctx)
    store.update(mem_id, text)
    console.print(f"[green]✓[/green] updated {mem_id}")


@app.command("rm")
def rm(
    ctx: typer.Context,
    mem_id: str = typer.Argument(..., help="Memory id, or 'all' to wipe."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation when removing all."),
):
    """Delete a memory (or wipe everything for this user_id)."""
    store = _store(ctx)
    if mem_id == "all":
        if not yes:
            confirm = typer.confirm("Delete ALL memories for this user_id?")
            if not confirm:
                raise typer.Abort()
        store.delete_all()
        console.print("[green]✓[/green] wiped all memories")
    else:
        # KeyError = no such memory (or short suffix didn't match anything).
        # ValueError = ambiguous suffix matches multiple memories.
        # Either way it's a user-facing error, not a bug; exit 1 with a hint.
        try:
            store.delete(mem_id)
        except (KeyError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
        console.print(f"[green]✓[/green] deleted {mem_id}")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search query."),
    top_k: int = typer.Option(5, "-k", "--top-k"),
    category: str = typer.Option(None, "--category", "-c"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Semantic search over personal memories."""
    store = _store(ctx)
    items = store.search(query, top_k=top_k, category=category)
    if as_json:
        typer.echo(json.dumps([_item_dict(m) for m in items], ensure_ascii=False, indent=2))
        return
    _render_table(items, show_score=True)


# ---------------------------------------------------------------------------
# Profile / learn — used by Cursor hooks
# ---------------------------------------------------------------------------


@app.command("profile")
def profile(
    ctx: typer.Context,
    write: Path = typer.Option(None, "--write", help="Write markdown to this path."),
    max_items: int = typer.Option(20, "--max-items", help="Truncate to N profile/pref items."),
):
    """Render a stable 'About the user' block from profile + pref memories."""
    store = _store(ctx)
    items: list[MemoryItem] = []
    for cat in PROFILE_CATEGORIES:
        items.extend(store.list(category=cat))
    items = items[:max_items]

    md = _render_profile(items)
    if write:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(md, encoding="utf-8")
        console.print(f"[green]✓[/green] wrote profile ({len(items)} items) → {write}")
    else:
        typer.echo(md)


@app.command("learn")
def learn(
    ctx: typer.Context,
    source: str = typer.Argument(
        None,
        help='Path to a file, or "-" / omit to read stdin.',
    ),
    from_path: Path = typer.Option(
        None,
        "--from",
        help="Alternative to the positional arg; path to a file.",
    ),
    from_cursor_transcript: bool = typer.Option(
        False, "--from-cursor-transcript", help="Read $CURSOR_TRANSCRIPT_PATH if set."
    ),
    category: str = typer.Option(
        "learning", "--category", "-c", help=f"One of: {sorted(ALLOWED_CATEGORIES)}"
    ),
):
    """Feed a chunk of text (notes / chat transcript) into mem0 for fact extraction.

    Always runs with infer=True so mem0 will split, dedupe, and merge against
    existing memories. For storing a single discrete fact verbatim, use
    `mem add` instead.
    """
    effective: Path | None
    if from_path is not None:
        effective = from_path
    elif source and source != "-":
        effective = Path(source)
    else:
        effective = None  # → stdin
    text = _read_learn_input(effective, from_cursor_transcript)
    if not text or not text.strip():
        err_console.print("[yellow]nothing to learn (empty input)[/yellow]")
        raise typer.Exit(0)
    store = _store(ctx)
    ids = store.learn(text, category=category)
    console.print(f"[green]✓[/green] learn produced {len(ids)} memory id(s)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_learn_input(from_path: Path | None, from_cursor: bool) -> str:
    if from_cursor:
        import os

        p = os.environ.get("CURSOR_TRANSCRIPT_PATH")
        if not p:
            return ""
        path = Path(p)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
    if from_path is not None and str(from_path) != "-":
        return Path(from_path).read_text(encoding="utf-8")
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def _item_dict(m: MemoryItem) -> dict:
    return {
        "id": m.id,
        "text": m.text,
        "category": m.category,
        "score": m.score,
        "metadata": m.metadata or {},
    }


def _render_table(items: list[MemoryItem], *, show_score: bool = False) -> None:
    if not items:
        console.print("[yellow](no memories)[/yellow]")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("id", style="dim")
    t.add_column("cat")
    if show_score:
        t.add_column("score")
    t.add_column("text", overflow="fold")
    for m in items:
        row = [m.id[-12:] if m.id else "-", m.category]
        if show_score:
            row.append(f"{m.score:.3f}")
        row.append(m.text)
        t.add_row(*row)
    console.print(t)


def _render_profile(items: list[MemoryItem]) -> str:
    if not items:
        return "## About the user\n\n_(no profile memories yet — use `memex mem add ... -c profile`)_\n"
    lines = ["## About the user", ""]
    for m in items:
        lines.append(f"- ({m.category}) {m.text}")
    lines.append("")
    return "\n".join(lines)
