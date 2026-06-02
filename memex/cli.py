"""memex — root CLI.

Usage:
  memex init [DIR]                       Initialize a memex root.
  memex doc add|update|rm|search|...     Manage wiki docs.
  memex mem add|search|ls|profile|...    Manage personal memory (mem0).
  memex ctx "<prompt>"                   Build a unified context block for hooks.
  memex cursor install-hooks|install-rule|install-agents   Wire memex into Cursor.
  memex serve                            Start the HTTP API (for Docker / remote).
  memex client <doc|mem|ctx|status> ...  Thin HTTP client (for LLM/Agent shell-out).
"""

from __future__ import annotations

from pathlib import Path

import typer

from memex import __version__
from memex.commands import (
    client_cmd,
    ctx_cmd,
    cursor_cmd,
    doc_cmd,
    init_cmd,
    mem_cmd,
    serve_cmd,
    status_cmd,
)

app = typer.Typer(
    name="memex",
    help="Personal assistant + knowledge base (mem0 OSS + Chroma + Cursor hooks).",
    no_args_is_help=True,
    add_completion=True,
    invoke_without_command=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"memex {__version__}")
        raise typer.Exit(0)


@app.callback()
def _root(
    ctx: typer.Context,
    root: Path = typer.Option(
        None,
        "--root",
        "-R",
        envvar="MEMEX_ROOT",
        help="memex root directory (default: $MEMEX_ROOT or ~/memex).",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
):
    ctx.ensure_object(dict)
    ctx.obj["root"] = root


app.command(name="init", help="Initialize a knowledge base root directory.")(init_cmd.init)
app.add_typer(doc_cmd.app, name="doc", help="Manage the markdown wiki.")
app.add_typer(mem_cmd.app, name="mem", help="Manage personal memory (mem0).")
app.command(name="ctx", help="Build a unified context block (for Cursor hooks).")(ctx_cmd.ctx)
app.add_typer(
    cursor_cmd.app, name="cursor", help="Cursor integration (hooks + rules + subagents)."
)
app.command(name="status", help="Show memex health (doc count, chunks, sizes, providers).")(
    status_cmd.status
)
app.command(name="backup", help="Snapshot the memex to a .tar.gz.")(status_cmd.backup)
app.command(name="restore", help="Restore a backup into a fresh directory.")(status_cmd.restore)
app.command(name="serve", help="Start the HTTP API (uvicorn).")(serve_cmd.serve)
app.add_typer(
    client_cmd.app,
    name="client",
    help="Talk to a remote memex API (for agents / Docker deployments).",
)


if __name__ == "__main__":
    app()
