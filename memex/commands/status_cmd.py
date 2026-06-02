"""`memex status` and `memex backup` — operational visibility and snapshots."""

from __future__ import annotations

import json
import tarfile
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from memex.backends.chroma_store import ChromaStore
from memex.core.config import load_config
from memex.core.wiki import Wiki

console = Console()
err_console = Console(stderr=True)


def status(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable status."),
):
    """Show memex health: doc count, chunk count, embedder, on-disk usage."""
    cfg = load_config(ctx.obj.get("root") if ctx.obj else None)
    wiki = Wiki(cfg)
    store: ChromaStore | None = None
    chunk_count = 0
    try:
        store = wiki.store
        chunk_count = store.count()
    except Exception as e:  # noqa: BLE001 - status is best-effort
        err_console.print(f"[yellow]warn:[/yellow] could not open chroma: {e}", style="dim")

    docs = list(wiki.iter_doc_paths())
    info = {
        "root": str(cfg.root),
        "user_id": cfg.user_id,
        "docs_count": len(docs),
        "chunks_count": chunk_count,
        "embedder": f"{cfg.embedder.provider}:{cfg.embedder.model}",
        "llm": f"{cfg.llm.provider}:{cfg.llm.model}",
        "docs_dir_bytes": _dir_size(cfg.docs_dir),
        "chroma_dir_bytes": _dir_size(cfg.chroma_dir),
        "mem0_dir_bytes": _dir_size(cfg.mem0_dir),
        "history_dir_bytes": _dir_size(cfg.history_dir),
    }
    if as_json:
        typer.echo(json.dumps(info, indent=2))
        return

    t = Table(show_header=False, box=None)
    t.add_column("k", style="bold")
    t.add_column("v")
    t.add_row("root", info["root"])
    t.add_row("user_id", info["user_id"])
    t.add_row("docs", str(info["docs_count"]))
    t.add_row("chunks", str(info["chunks_count"]))
    t.add_row("embedder", info["embedder"])
    t.add_row("llm", info["llm"])
    t.add_row("docs size", _human_bytes(info["docs_dir_bytes"]))
    t.add_row("chroma size", _human_bytes(info["chroma_dir_bytes"]))
    t.add_row("mem0 size", _human_bytes(info["mem0_dir_bytes"]))
    t.add_row("history size", _human_bytes(info["history_dir_bytes"]))
    console.print(t)


def backup(
    ctx: typer.Context,
    target: Path = typer.Option(
        None,
        "--out",
        "-o",
        help="Output path (.tar.gz). Default: <root>/.cache/backup/memex-<ts>.tar.gz.",
    ),
    include_cache: bool = typer.Option(
        False, "--include-cache", help="Also archive .cache/ (Chroma + mem0 stores)."
    ),
):
    """Create a tar.gz snapshot of the memex. By default only docs/, .kbignore, memex.yaml."""
    cfg = load_config(ctx.obj.get("root") if ctx.obj else None)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if target is None:
        out_dir = cfg.cache_dir / "backup"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"memex-{ts}.tar.gz"
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    excludes = set()
    if not include_cache:
        excludes.add(str(cfg.cache_dir.resolve()))

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        full = (cfg.root / tarinfo.name).resolve()
        for ex in excludes:
            try:
                full.relative_to(ex)
                return None
            except ValueError:
                continue
        return tarinfo

    with tarfile.open(target, "w:gz") as tar:
        tar.add(str(cfg.root), arcname=".", filter=_filter)

    size = target.stat().st_size
    console.print(f"[green]✓[/green] backup written: {target}  ({_human_bytes(size)})")
    if not include_cache:
        console.print(
            "  [dim]hint: pass --include-cache to also snapshot chroma & mem0 stores[/dim]"
        )


def restore(
    ctx: typer.Context,
    archive: Path = typer.Argument(..., help="Path to a memex-*.tar.gz produced by `memex backup`."),
    target: Path = typer.Option(
        None, "--target", help="Restore into this directory (default: a sibling 'memex-restored')."
    ),
):
    """Extract a backup archive into a fresh directory (never overwrites the live memex)."""
    archive = archive.expanduser().resolve()
    if not archive.exists():
        err_console.print(f"[red]error:[/red] no such archive: {archive}")
        raise typer.Exit(2)

    cfg = load_config(ctx.obj.get("root") if ctx.obj else None)
    if target is None:
        target = cfg.root.parent / "memex-restored"
    target = target.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        err_console.print(
            f"[red]error:[/red] target exists and is non-empty: {target}. "
            "Refusing to overwrite."
        )
        raise typer.Exit(2)
    target.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:gz") as tar:
        # filter for Python 3.12+; fall back to no-filter on older.
        try:
            tar.extractall(path=str(target), filter="data")  # type: ignore[arg-type]
        except TypeError:
            tar.extractall(path=str(target))  # noqa: S202
    console.print(f"[green]✓[/green] restored to: {target}")
    console.print(
        f"  [dim]use `MEMEX_ROOT={target} memex status` to verify, then move into place.[/dim]"
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0  # type: ignore[assignment]
    return f"{n:,.1f} PB"


__all__ = ["status", "backup", "restore"]
