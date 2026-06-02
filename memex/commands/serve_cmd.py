"""`memex serve` — run the FastAPI server."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

console = Console()


def serve(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind address."),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port."),
    reload: bool = typer.Option(
        False, "--reload", help="Auto-reload on code change (development only)."
    ),
    workers: int = typer.Option(
        1, "--workers", "-w", min=1, help="Number of uvicorn workers (must be 1 in dev mode)."
    ),
    root: Path = typer.Option(
        None,
        "--root",
        "-R",
        envvar="MEMEX_ROOT",
        help="memex root for the server (overrides global --root for clarity).",
    ),
):
    """Start the memex HTTP API. Endpoints: /, /healthz, /status, /doc/*, /mem/*, /ctx, /docs (OpenAPI UI)."""
    import uvicorn

    effective_root = root or (ctx.obj.get("root") if ctx.obj else None)
    root_str = str(effective_root) if effective_root else None

    if reload and workers > 1:
        console.print(
            "[yellow]warn:[/yellow] --reload disables --workers; using a single worker."
        )
        workers = 1

    console.print(
        f"[green]✓[/green] memex API on [bold]http://{host}:{port}[/bold]"
        f"  (root={root_str or '<default>'})"
    )
    console.print(
        f"  Try: [cyan]curl http://{host}:{port}/healthz[/cyan]"
        "   or open [cyan]/docs[/cyan] for the OpenAPI UI",
        style="dim",
    )

    if reload:
        # --reload needs an import string (not an instance) so uvicorn can
        # rebuild the app after each code change. We use a factory and pass
        # MEMEX_ROOT via env so the factory can reproduce the same config.
        import os

        if root_str:
            os.environ["MEMEX_ROOT"] = root_str
        uvicorn.run(
            "memex.server.factory:reload_app",
            host=host,
            port=port,
            reload=True,
            factory=True,
            workers=1,
        )
        return

    from memex.server.api import build_app

    app = build_app(root_str)
    uvicorn.run(app, host=host, port=port, workers=workers)
