"""`memex init` — bootstrap a new memex root directory."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console

from memex.core.config import CONFIG_FILENAME, resolve_root, write_default_config

console = Console()

DEFAULT_SUBDIRS = ("inbox", "projects", "people", "work", "learning", "reference")

KBIGNORE_TEMPLATE = """# Patterns to skip when indexing. Same syntax as .gitignore (subset).
.git
.cache
node_modules
.obsidian
.trash
"""

GITIGNORE_TEMPLATE = """# memex local state — do not commit
.cache/
"""


def _load_packaged_template(name: str) -> str:
    """Read a file from the `templates/` directory shipped with the package."""
    from importlib import resources

    try:
        return (resources.files("memex") / ".." / "templates" / name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        candidates = [
            Path(__file__).resolve().parents[2] / "templates" / name,
            Path(__file__).resolve().parents[1] / "templates" / name,
        ]
        for c in candidates:
            if c.exists():
                return c.read_text(encoding="utf-8")
    raise FileNotFoundError(f"template not found: {name}")


def _write_local_profile(root: Path, user_id: str) -> Path:
    """Write the fully-local profile config (offline embedder + OpenAI-compat LLM)."""
    cfg_path = root / CONFIG_FILENAME
    if cfg_path.exists():
        return cfg_path
    raw = _load_packaged_template("memex.local.yaml")
    cfg_path.write_text(raw.replace("{USER_ID}", user_id), encoding="utf-8")
    return cfg_path


def init(
    root: Path = typer.Argument(
        None,
        help="Target directory. Defaults to $MEMEX_ROOT or ~/memex.",
    ),
    user_id: str = typer.Option("default", "--user-id", "-u", help="mem0 user_id."),
    profile: str = typer.Option(
        "openai",
        "--profile",
        "-p",
        help=(
            "Config profile to write into memex.yaml. "
            "openai = cloud OpenAI (needs OPENAI_API_KEY); "
            "local = offline embeddings + any OpenAI-compatible LLM endpoint "
            "(e.g. Ollama at http://host:11434/v1)."
        ),
    ),
    no_git: bool = typer.Option(False, "--no-git", help="Skip `git init`."),
    force: bool = typer.Option(False, "--force", "-f", help="Re-stamp templates if they exist."),
):
    """Create directory structure, default config, .kbignore and a git repo."""
    # Validate the profile *before* touching the filesystem so an obvious
    # typo (`--profile lcoal`) fails fast and doesn't pretend success when
    # the target already exists.
    profile_lc = profile.strip().lower()
    if profile_lc not in {"local", "openai"}:
        console.print(
            f"[red]error:[/red] unknown profile {profile!r}. Use one of: openai, local"
        )
        raise typer.Exit(2)

    target = resolve_root(root)
    target.mkdir(parents=True, exist_ok=True)
    docs_dir = target / "docs"
    docs_dir.mkdir(exist_ok=True)

    for sub in DEFAULT_SUBDIRS:
        sub_path = docs_dir / sub
        sub_path.mkdir(exist_ok=True)
        keep = sub_path / ".gitkeep"
        if not keep.exists():
            keep.touch()

    cache = target / ".cache"
    cache.mkdir(exist_ok=True)

    cfg_path = target / CONFIG_FILENAME
    if cfg_path.exists() and not force:
        console.print(f"[dim]memex.yaml already exists; leaving it alone:[/dim] {cfg_path}")
    else:
        if force and cfg_path.exists():
            cfg_path.unlink()
        if profile_lc == "local":
            cfg_path = _write_local_profile(target, user_id=user_id)
        else:  # openai
            cfg_path = write_default_config(target, user_id=user_id)

    kbignore = target / ".kbignore"
    if force or not kbignore.exists():
        kbignore.write_text(KBIGNORE_TEMPLATE, encoding="utf-8")
    gitignore = target / ".gitignore"
    if force or not gitignore.exists():
        gitignore.write_text(GITIGNORE_TEMPLATE, encoding="utf-8")

    if not no_git and not (target / ".git").exists():
        try:
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=target,
                check=True,
                stdout=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            console.print("[yellow]warn: git init failed (is git installed?)[/yellow]")

    console.print(f"[green]✓[/green] memex root ready at [bold]{target}[/bold]")
    console.print(f"  profile: {profile_lc}")
    console.print(f"  config:  {cfg_path}")
    console.print(f"  docs:    {docs_dir}")
    console.print(f"  cache:   {cache}")
    console.print("\nNext steps:")
    console.print("  • [cyan]memex doc add[/cyan] some-note.md")
    console.print("  • [cyan]memex mem add[/cyan] \"I prefer TypeScript over JS\" --category pref")
    console.print("  • [cyan]memex cursor install-hooks[/cyan]   # auto-inject context into Cursor")
