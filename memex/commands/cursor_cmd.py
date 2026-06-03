"""`memex cursor *` — install Cursor hooks and project rules."""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(help="Integrate memex with Cursor (hooks + rules + subagents, no MCP).")

console = Console()
err_console = Console(stderr=True)

# Custom subagents we ship. Names must match the `name:` frontmatter field
# and the on-disk filename Cursor expects (filename === name + ".md").
AGENT_NAMES = ("memex-ask", "memex-archive", "memex-curator")

# Filename of the standalone HTTP client script that ships in templates/.
# Installed alongside the agents at ~/.cursor/agents/ so hooks and subagents
# can talk to the memex server without depending on the `memex` package being
# on PATH (which is fragile because PyPI has an unrelated package named
# `memex` that `uv tool install memex` happily pulls in).
CLIENT_SCRIPT_NAME = "memex-client.py"


def _default_client_script_path() -> Path:
    """Where the standalone client script lands by default.

    Always user-scope (~/.cursor/agents/) so a single copy serves both
    user-scope and project-scope agent installs. Computed at call time so
    tests can monkeypatch HOME.
    """
    return Path.home() / ".cursor" / "agents" / CLIENT_SCRIPT_NAME


def _install_client_script(target: Path | None = None, force: bool = False) -> tuple[Path, bool]:
    """Drop templates/memex-client.py at `target`, chmod +x.

    Returns (path, wrote). `wrote` is False when the file already existed and
    --force was not set.
    """
    dst = target or _default_client_script_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not force:
        return dst, False
    dst.write_text(_read_template(CLIENT_SCRIPT_NAME), encoding="utf-8")
    # 0o755 = rwxr-xr-x. The script has a `#!/usr/bin/env python3` shebang
    # so it's runnable directly by hooks / agents once executable.
    os.chmod(dst, 0o755)
    return dst, True


def _read_template(name: str) -> str:
    """Load a packaged template file as text.

    During pip install the templates ship as data; during in-tree development
    we fall back to <repo>/templates/. `name` may include a subpath, e.g.
    "agents/memex-ask.md".
    """
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


def _agents_dir(scope: str, project_root: Path | None = None) -> Path:
    """Return the directory where Cursor expects subagent files for this scope."""
    s = scope.lower()
    if s == "user":
        return Path.home() / ".cursor" / "agents"
    if s == "project":
        root = (project_root or Path(".")).expanduser().resolve()
        return root / ".cursor" / "agents"
    raise typer.BadParameter(f"unknown scope {scope!r}; use 'user' or 'project'")


@app.command("install-hooks")
def install_hooks(
    target: Path = typer.Option(
        Path.home() / ".cursor" / "hooks.json",
        "--target",
        help="Where to write hooks.json (default: user-level).",
    ),
    merge: bool = typer.Option(True, "--merge/--replace", help="Merge into existing file."),
    force: bool = typer.Option(False, "--force", help="Overwrite without merge if file exists."),
    install_client: bool = typer.Option(
        True,
        "--install-client/--no-install-client",
        help=(
            "Also drop the standalone memex-client.py script at "
            "~/.cursor/agents/. The hooks reference it by absolute path so "
            "they work without the `memex` package on PATH."
        ),
    ),
):
    """Install Cursor lifecycle hooks (sessionStart / beforeSubmitPrompt)."""
    tpl = json.loads(_read_template("hooks.json"))
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and not force:
        if merge:
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                err_console.print(
                    f"[red]error:[/red] existing {target} is not valid JSON; use --force"
                )
                raise typer.Exit(2) from e
            merged = _merge_hooks(existing, tpl)
            target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
            console.print(f"[green]✓[/green] merged memex hooks into {target}")
        else:
            err_console.print(f"[red]error:[/red] {target} exists; pass --force or --merge")
            raise typer.Exit(2)
    else:
        target.write_text(json.dumps(tpl, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]✓[/green] wrote {target}")
        console.print(
            "  Restart Cursor (or reload the window) for the hooks to take effect.", style="dim"
        )

    if install_client:
        client_path, wrote = _install_client_script()
        if wrote:
            console.print(f"[green]✓[/green] installed standalone client at {client_path}")
        else:
            console.print(
                f"  client script already at {client_path}; pass --force to overwrite",
                style="dim",
            )


@app.command("install-rule")
def install_rule(
    project_root: Path = typer.Argument(
        Path("."), help="The project root in which to place .cursor/rules/memex.mdc."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite if present."),
    install_client: bool = typer.Option(
        True,
        "--install-client/--no-install-client",
        help=(
            "Also drop the standalone memex-client.py script at "
            "~/.cursor/agents/. The rule tells the main thread to call it."
        ),
    ),
):
    """Install a project-level Cursor rule that teaches the agent the memex CLI."""
    project_root = project_root.expanduser().resolve()
    target = project_root / ".cursor" / "rules" / "memex.mdc"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        err_console.print(f"[red]error:[/red] {target} exists; pass --force to overwrite")
        raise typer.Exit(2)
    target.write_text(_read_template("memex.mdc"), encoding="utf-8")
    console.print(f"[green]✓[/green] wrote {target}")

    if install_client:
        client_path, wrote = _install_client_script()
        if wrote:
            console.print(f"[green]✓[/green] installed standalone client at {client_path}")
        else:
            console.print(
                f"  client script already at {client_path}; pass --force to overwrite",
                style="dim",
            )


@app.command("print-hooks")
def print_hooks():
    """Print the hooks.json template (for piping into your own config)."""
    typer.echo(_read_template("hooks.json"))


@app.command("print-rule")
def print_rule():
    """Print the memex.mdc rule template (for inspection / piping)."""
    typer.echo(_read_template("memex.mdc"))


# ---------------------------------------------------------------------------
# Subagents — Cursor reads .md files in ~/.cursor/agents/ (user) or
# .cursor/agents/ (project). See cursor.com/docs/subagents.
# ---------------------------------------------------------------------------


@app.command("install-agents")
def install_agents(
    scope: str = typer.Option(
        "user",
        "--scope",
        "-s",
        help="Install scope: 'user' (~/.cursor/agents/) or 'project' (./.cursor/agents/).",
    ),
    project_root: Path = typer.Option(
        Path("."),
        "--project-root",
        help="Project root for --scope project. Ignored for --scope user.",
    ),
    only: list[str] = typer.Option(
        None,
        "--only",
        help=f"Install only the named agent(s). Choices: {', '.join(AGENT_NAMES)}.",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing agent files."),
    install_client: bool = typer.Option(
        True,
        "--install-client/--no-install-client",
        help=(
            "Also drop the standalone memex-client.py script at "
            "~/.cursor/agents/. The agents call it by absolute path."
        ),
    ),
):
    """Install the memex-ask / memex-archive / memex-curator Cursor subagents."""
    targets = list(only) if only else list(AGENT_NAMES)
    for name in targets:
        if name not in AGENT_NAMES:
            err_console.print(
                f"[red]error:[/red] unknown agent {name!r}; choices: {', '.join(AGENT_NAMES)}"
            )
            raise typer.Exit(2)

    dest_dir = _agents_dir(scope, project_root)
    dest_dir.mkdir(parents=True, exist_ok=True)

    wrote: list[Path] = []
    skipped: list[Path] = []
    for name in targets:
        src = f"agents/{name}.md"
        dst = dest_dir / f"{name}.md"
        if dst.exists() and not force:
            skipped.append(dst)
            continue
        dst.write_text(_read_template(src), encoding="utf-8")
        wrote.append(dst)

    for p in wrote:
        console.print(f"[green]✓[/green] wrote {p}")
    for p in skipped:
        console.print(f"[yellow]skip[/yellow] {p} (exists; pass --force to overwrite)")

    if install_client:
        client_path, client_wrote = _install_client_script(force=force)
        if client_wrote:
            console.print(f"[green]✓[/green] installed standalone client at {client_path}")
        else:
            console.print(
                f"  client script already at {client_path}; pass --force to overwrite",
                style="dim",
            )

    if wrote:
        console.print(
            "\n  Invoke from Cursor chat with [cyan]/memex-ask[/cyan], [cyan]/memex-archive[/cyan], "
            "or [cyan]/memex-curator[/cyan].",
            style="dim",
        )


@app.command("install-client")
def install_client_cmd(
    target: Path = typer.Option(
        None,
        "--target",
        help=(
            "Where to write the standalone memex-client.py "
            "(default: ~/.cursor/agents/memex-client.py)."
        ),
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite if present."),
):
    """Install just the standalone HTTP client script (~/.cursor/agents/memex-client.py).

    The script is stdlib-only and is what Cursor hooks / subagents call to talk
    to the memex server. Useful when you want to refresh the script without
    rewriting hooks.json / agent files.
    """
    dst, wrote = _install_client_script(target=target, force=force)
    if wrote:
        console.print(f"[green]✓[/green] wrote {dst}")
        console.print(
            f"  Try: [cyan]{dst} status[/cyan]   (set MEMEX_API_URL first)",
            style="dim",
        )
    else:
        err_console.print(
            f"[red]error:[/red] {dst} already exists; pass --force to overwrite"
        )
        raise typer.Exit(2)


@app.command("list-agents")
def list_agents():
    """List the subagents this CLI ships, along with their descriptions."""
    import re

    table_rows: list[tuple[str, str, str]] = []
    for name in AGENT_NAMES:
        text = _read_template(f"agents/{name}.md")
        desc = ""
        readonly = ""
        m = re.search(r"^description:\s*(.+?)$", text, re.MULTILINE)
        if m:
            desc = m.group(1).strip().strip("\"'")
        m = re.search(r"^readonly:\s*(true|false)\s*$", text, re.MULTILINE)
        if m:
            readonly = m.group(1)
        table_rows.append((name, readonly, desc))

    from rich.table import Table

    t = Table(show_header=True, header_style="bold")
    t.add_column("agent", style="cyan")
    t.add_column("readonly")
    t.add_column("description", overflow="fold")
    for name, ro, desc in table_rows:
        t.add_row(name, ro, desc)
    console.print(t)


@app.command("print-agent")
def print_agent(
    name: str = typer.Argument(..., help=f"Agent name. Choices: {', '.join(AGENT_NAMES)}."),
):
    """Print a subagent template (for inspection / piping)."""
    if name not in AGENT_NAMES:
        err_console.print(
            f"[red]error:[/red] unknown agent {name!r}; choices: {', '.join(AGENT_NAMES)}"
        )
        raise typer.Exit(2)
    typer.echo(_read_template(f"agents/{name}.md"))


def _merge_hooks(existing: dict, new: dict) -> dict:
    out = dict(existing)
    out_hooks = dict(existing.get("hooks") or {})
    new_hooks = new.get("hooks") or {}
    for event, items in new_hooks.items():
        cur = list(out_hooks.get(event) or [])
        cur_names = {it.get("name") for it in cur if isinstance(it, dict) and "name" in it}
        cur_cmds = {it.get("command") for it in cur if isinstance(it, dict)}
        for it in items:
            if it.get("name") in cur_names or it.get("command") in cur_cmds:
                continue
            cur.append(it)
        out_hooks[event] = cur
    out["hooks"] = out_hooks
    return out
