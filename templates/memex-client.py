#!/usr/bin/env python3
"""memex-client — single-file HTTP client for the memex API.

This script is the on-disk twin of `memex client` (see memex/commands/client_cmd.py),
designed to be dropped into ~/.cursor/agents/ alongside the agent .md files so
Cursor hooks and subagents can talk to the memex server without depending on
the `memex` package being installed on PATH.

Why a separate script:
  - `uv tool install memex` accidentally fetches an unrelated package from PyPI
    called `memex`; even when our package is installed properly, mixing the
    `memex` CLI on PATH with the `memex client` HTTP usage from Cursor is brittle.
  - This script depends only on the Python stdlib (urllib + argparse + json),
    so it just runs on any reasonably modern Python 3 (>=3.8) regardless of
    which `memex` package is on PATH.

Surface mirrors `memex client`:

  memex-client.py [--url URL] [--token TOKEN] <subcommand> ...

Server selection (precedence: CLI flag > env var > default):
  --url URL / -u URL  or  $MEMEX_API_URL    (default http://127.0.0.1:8000)
  --token TOKEN       or  $MEMEX_API_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 60.0


def _resolve_url_token(args: argparse.Namespace) -> tuple[str, str]:
    """CLI flag > env var > default."""
    url = (args.url or os.environ.get("MEMEX_API_URL") or DEFAULT_URL).rstrip("/")
    token = (args.token or os.environ.get("MEMEX_API_TOKEN") or "").strip()
    return url, token


def _request(
    args: argparse.Namespace,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: Any | None = None,
) -> tuple[int, Any, str]:
    """Make an HTTP request. Returns (status, json_or_None, raw_text)."""
    url, token = _resolve_url_token(args)
    qs = ""
    if params:
        flat: list[tuple[str, str]] = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, bool):
                flat.append((k, "true" if v else "false"))
            else:
                flat.append((k, str(v)))
        if flat:
            qs = "?" + urllib.parse.urlencode(flat)
    full = url + path + qs
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(full, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        status = e.code
    except urllib.error.URLError as e:
        sys.stderr.write(f"error: cannot reach {full}: {e}\n")
        sys.exit(2)
    parsed: Any = None
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None
    return status, parsed, text


def _die(status: int, parsed: Any, raw: str) -> None:
    msg = ""
    if isinstance(parsed, dict):
        msg = parsed.get("detail") or parsed.get("error") or ""
        if not msg:
            msg = json.dumps(parsed, ensure_ascii=False)
    if not msg:
        msg = raw or f"HTTP {status}"
    sys.stderr.write(f"error {status}: {msg}\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Output helpers (plain text; no rich/click dep)
# ---------------------------------------------------------------------------


def _print_kv(data: dict[str, Any]) -> None:
    if not data:
        return
    width = max((len(str(k)) for k in data), default=0)
    for k, v in data.items():
        sys.stdout.write(f"{str(k).ljust(width)}  {v}\n")


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> None:
    status, parsed, raw = _request(args, "GET", "/status")
    if status != 200:
        _die(status, parsed, raw)
    if args.json:
        _print_json(parsed)
    else:
        _print_kv(parsed if isinstance(parsed, dict) else {})


def cmd_ctx(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {
        "query": args.query,
        "include_profile": not args.no_profile,
        "include_memories": not args.no_memories,
        "include_docs": not args.no_docs,
    }
    if args.budget is not None:
        payload["budget"] = args.budget
    if args.top_k_docs is not None:
        payload["top_k_docs"] = args.top_k_docs
    if args.top_k_mems is not None:
        payload["top_k_mems"] = args.top_k_mems
    status, parsed, raw = _request(args, "POST", "/ctx", body=payload)
    if status != 200:
        _die(status, parsed, raw)
    block = parsed.get("block", "") if isinstance(parsed, dict) else ""
    if args.write:
        out_path = os.path.expanduser(args.write)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(block)
        tokens = parsed.get("tokens", 0) if isinstance(parsed, dict) else 0
        sys.stderr.write(f"wrote ctx ({tokens} tokens) -> {out_path}\n")
    else:
        sys.stdout.write(block)
        if not block.endswith("\n"):
            sys.stdout.write("\n")


def cmd_raw(args: argparse.Namespace) -> None:
    body: Any = None
    if args.body:
        try:
            body = json.loads(args.body)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"error: --body is not valid JSON: {e}\n")
            sys.exit(2)
    status, parsed, raw = _request(args, args.method, args.path, body=body)
    sys.stdout.write(f"HTTP {status}\n")
    if parsed is not None:
        _print_json(parsed)
    elif raw:
        sys.stdout.write(raw + ("\n" if not raw.endswith("\n") else ""))


# -------- doc --------


def cmd_doc_add(args: argparse.Namespace) -> None:
    if args.source == "-":
        body = sys.stdin.read()
        if not body.strip():
            sys.stderr.write("error: empty stdin\n")
            sys.exit(2)
    else:
        path = os.path.expanduser(args.source)
        if not os.path.exists(path):
            sys.stderr.write(f"error: no such file: {path}\n")
            sys.exit(2)
        with open(path, encoding="utf-8") as f:
            body = f.read()
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    payload = {"body": body, "title": args.title, "tags": tags, "subdir": args.subdir}
    status, parsed, raw = _request(args, "POST", "/doc/add", body=payload)
    if status != 200:
        _die(status, parsed, raw)
    d = parsed or {}
    sys.stdout.write(f"saved {d.get('title', '')}\n")
    sys.stdout.write(f"  id:   {d.get('id', '')}\n")
    sys.stdout.write(f"  path: {d.get('path', '')}\n")
    sys.stdout.write(f"  tags: {', '.join(d.get('tags') or []) or '(none)'}\n")


def cmd_doc_search(args: argparse.Namespace) -> None:
    params: dict[str, Any] = {"q": args.query}
    if args.top_k is not None:
        params["k"] = args.top_k
    if args.tag:
        params["tag"] = args.tag
    if args.since:
        params["since"] = args.since
    status, parsed, raw = _request(args, "GET", "/doc/search", params=params)
    if status != 200:
        _die(status, parsed, raw)
    hits = (parsed or {}).get("hits", []) if isinstance(parsed, dict) else []
    if args.json:
        _print_json(hits)
        return
    if not hits:
        sys.stdout.write("(no hits)\n")
        return
    for h in hits:
        sys.stdout.write(
            f"{h.get('title', '')}  ({h.get('heading', '')})  score={h.get('score', 0):.3f}\n"
        )
        sys.stdout.write(
            f"  {h.get('path', '')}  • tags: {', '.join(h.get('tags') or []) or '-'}\n"
        )
        text = h.get("text") or ""
        for line in text.splitlines():
            sys.stdout.write(f"  > {line}\n")
        sys.stdout.write("\n")


def cmd_doc_ls(args: argparse.Namespace) -> None:
    params: dict[str, Any] = {}
    if args.tag:
        params["tag"] = args.tag
    if args.since:
        params["since"] = args.since
    status, parsed, raw = _request(args, "GET", "/doc", params=params)
    if status != 200:
        _die(status, parsed, raw)
    docs = (parsed or {}).get("docs", []) if isinstance(parsed, dict) else []
    if args.json:
        _print_json(docs)
        return
    if not docs:
        sys.stdout.write("(no documents)\n")
        return
    for d in docs:
        sys.stdout.write(
            f"{d.get('id', '')[-12:]:>12}  {d.get('title', '')}\n"
            f"               tags: {', '.join(d.get('tags') or []) or '-'}  updated: {d.get('updated', '')}\n"
            f"               path: {d.get('path', '')}\n"
        )


def cmd_doc_show(args: argparse.Namespace) -> None:
    status, parsed, raw = _request(args, "GET", f"/doc/{urllib.parse.quote(args.ident, safe='')}")
    if status != 200:
        _die(status, parsed, raw)
    if args.json:
        _print_json(parsed)
        return
    d = parsed or {}
    sys.stdout.write(f"{d.get('title', '')}  ({d.get('id', '')})\n")
    sys.stdout.write(f"{d.get('path', '')}  • tags: {', '.join(d.get('tags') or []) or '-'}\n")


def cmd_doc_rm(args: argparse.Namespace) -> None:
    status, parsed, raw = _request(
        args,
        "DELETE",
        f"/doc/{urllib.parse.quote(args.ident, safe='')}",
        params={"keep_file": args.keep_file},
    )
    if status != 200:
        _die(status, parsed, raw)
    sys.stdout.write(f"removed {(parsed or {}).get('id', args.ident)}\n")


def cmd_doc_reindex(args: argparse.Namespace) -> None:
    status, parsed, raw = _request(args, "POST", "/doc/reindex", params={"all": args.all_})
    if status != 200:
        _die(status, parsed, raw)
    sys.stdout.write(f"reindex ok  {parsed or ''}\n")


# -------- mem --------


def cmd_mem_add(args: argparse.Namespace) -> None:
    payload = {
        "text": args.text,
        "category": args.category,
        "tags": list(args.tag or []),
        "infer": args.infer,
    }
    status, parsed, raw = _request(args, "POST", "/mem/add", body=payload)
    if status != 200:
        _die(status, parsed, raw)
    ids = (parsed or {}).get("ids", []) if isinstance(parsed, dict) else []
    if not ids:
        sys.stdout.write("mem0 returned no new ids (likely deduped)\n")
    else:
        for mid in ids:
            sys.stdout.write(f"stored {mid}  ({args.category})\n")


def cmd_mem_search(args: argparse.Namespace) -> None:
    params: dict[str, Any] = {"q": args.query, "k": args.top_k}
    if args.category:
        params["category"] = args.category
    status, parsed, raw = _request(args, "GET", "/mem/search", params=params)
    if status != 200:
        _die(status, parsed, raw)
    items = (parsed or {}).get("memories", []) if isinstance(parsed, dict) else []
    if args.json:
        _print_json(items)
        return
    if not items:
        sys.stdout.write("(no memories)\n")
        return
    for m in items:
        sys.stdout.write(
            f"{m.get('id', '')[-12:]:>12}  {m.get('category', ''):<10}  "
            f"score={m.get('score', 0):.3f}\n"
        )
        sys.stdout.write(f"               {m.get('text', '')}\n")


def cmd_mem_ls(args: argparse.Namespace) -> None:
    params: dict[str, Any] = {}
    if args.category:
        params["category"] = args.category
    status, parsed, raw = _request(args, "GET", "/mem", params=params)
    if status != 200:
        _die(status, parsed, raw)
    items = (parsed or {}).get("memories", []) if isinstance(parsed, dict) else []
    if args.json:
        _print_json(items)
        return
    if not items:
        sys.stdout.write("(no memories)\n")
        return
    for m in items:
        sys.stdout.write(
            f"{m.get('id', '')[-12:]:>12}  {m.get('category', ''):<10}  {m.get('text', '')}\n"
        )


def cmd_mem_show(args: argparse.Namespace) -> None:
    status, parsed, raw = _request(args, "GET", f"/mem/{urllib.parse.quote(args.mem_id, safe='')}")
    if status != 200:
        _die(status, parsed, raw)
    _print_json(parsed)


def cmd_mem_rm(args: argparse.Namespace) -> None:
    status, parsed, raw = _request(args, "DELETE", f"/mem/{urllib.parse.quote(args.mem_id, safe='')}")
    if status != 200:
        _die(status, parsed, raw)
    sys.stdout.write(f"deleted {args.mem_id}\n")


def cmd_mem_profile(args: argparse.Namespace) -> None:
    status, parsed, raw = _request(args, "GET", "/mem/profile", params={"max_items": args.max_items})
    if status != 200:
        _die(status, parsed, raw)
    block = (parsed or {}).get("block", "") if isinstance(parsed, dict) else ""
    if args.write:
        out_path = os.path.expanduser(args.write)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(block)
        count = parsed.get("count", 0) if isinstance(parsed, dict) else 0
        sys.stderr.write(f"wrote profile ({count} items) -> {out_path}\n")
    else:
        sys.stdout.write(block)
        if not block.endswith("\n"):
            sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# CLI assembly
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Top-level parser.

    `--url` / `--token` live on the root parser only, not on each subparser.
    Mirrors how `memex client` (Typer + Click) handles its callback options:
    they must appear *before* the subcommand:

        memex-client.py --url http://host:8000 --token abc status   ✓
        memex-client.py status --url http://host:8000 …             ✗

    Why not duplicate them on every subparser?  argparse subparsers re-apply
    their own `default=None` to the merged namespace, which would silently
    overwrite the URL the parent already parsed.  Keeping the flags at the
    top is unambiguous, less code, and matches the Typer surface.
    """
    p = argparse.ArgumentParser(
        prog="memex-client",
        description="Standalone HTTP client for the memex API (stdlib-only).",
        epilog="Global flags --url / --token MUST appear before the subcommand.",
    )
    p.add_argument(
        "--url",
        "-u",
        default=None,
        help=f"Server base URL (default: $MEMEX_API_URL or {DEFAULT_URL}).",
    )
    p.add_argument(
        "--token",
        default=None,
        help="Bearer token for Authorization header (default: $MEMEX_API_TOKEN).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # status
    sp = sub.add_parser("status", help="Show the remote memex's status.")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    # ctx
    sp = sub.add_parser("ctx", help="Build a unified context block on the server.")
    sp.add_argument("query", nargs="?", default="")
    sp.add_argument("--budget", type=int, default=None)
    sp.add_argument("--top-k-docs", "-k", type=int, default=None)
    sp.add_argument("--top-k-mems", type=int, default=None)
    sp.add_argument("--no-profile", action="store_true")
    sp.add_argument("--no-memories", action="store_true")
    sp.add_argument("--no-docs", action="store_true")
    sp.add_argument("--write", default=None)
    sp.set_defaults(func=cmd_ctx)

    # raw
    sp = sub.add_parser("raw", help="Arbitrary HTTP call.")
    sp.add_argument("method")
    sp.add_argument("path")
    sp.add_argument("--body", default=None, help="JSON body for POST/PUT.")
    sp.set_defaults(func=cmd_raw)

    # doc
    doc = sub.add_parser("doc", help="Remote wiki operations.")
    doc_sub = doc.add_subparsers(dest="doc_cmd", required=True)

    sp = doc_sub.add_parser("add", help="Add a markdown doc.")
    sp.add_argument("source", nargs="?", default="-")
    sp.add_argument("--title", "-t", default=None)
    sp.add_argument("--tags", default="")
    sp.add_argument("--subdir", "-d", default="inbox")
    sp.set_defaults(func=cmd_doc_add)

    sp = doc_sub.add_parser("search", help="Hybrid wiki search.")
    sp.add_argument("query")
    sp.add_argument("--top-k", "-k", type=int, default=None)
    sp.add_argument("--tag", default=None)
    sp.add_argument("--since", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doc_search)

    sp = doc_sub.add_parser("ls", help="List docs.")
    sp.add_argument("--tag", default=None)
    sp.add_argument("--since", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doc_ls)

    sp = doc_sub.add_parser("show", help="Show one doc.")
    sp.add_argument("ident")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doc_show)

    sp = doc_sub.add_parser("rm", help="Remove a doc.")
    sp.add_argument("ident")
    sp.add_argument("--keep-file", action="store_true")
    sp.set_defaults(func=cmd_doc_rm)

    sp = doc_sub.add_parser("reindex", help="Reindex the wiki.")
    sp.add_argument("--all", dest="all_", action="store_true")
    sp.set_defaults(func=cmd_doc_reindex)

    # mem
    mem = sub.add_parser("mem", help="Remote memory operations.")
    mem_sub = mem.add_subparsers(dest="mem_cmd", required=True)

    sp = mem_sub.add_parser("add", help="Add a memory.")
    sp.add_argument("text")
    sp.add_argument("--category", "-c", default="fact")
    sp.add_argument("--tag", action="append", default=None)
    infer = sp.add_mutually_exclusive_group()
    infer.add_argument("--infer", dest="infer", action="store_true")
    infer.add_argument("--no-infer", dest="infer", action="store_false")
    sp.set_defaults(infer=False, func=cmd_mem_add)

    sp = mem_sub.add_parser("search", help="Semantic memory search.")
    sp.add_argument("query")
    sp.add_argument("--top-k", "-k", type=int, default=5)
    sp.add_argument("--category", "-c", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_mem_search)

    sp = mem_sub.add_parser("ls", help="List memories.")
    sp.add_argument("--category", "-c", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_mem_ls)

    sp = mem_sub.add_parser("show", help="Show one memory.")
    sp.add_argument("mem_id")
    sp.set_defaults(func=cmd_mem_show)

    sp = mem_sub.add_parser("rm", help="Delete a memory (or 'all' to wipe).")
    sp.add_argument("mem_id")
    sp.set_defaults(func=cmd_mem_rm)

    sp = mem_sub.add_parser("profile", help="Render the 'About the user' block.")
    sp.add_argument("--max-items", type=int, default=20)
    sp.add_argument("--write", default=None)
    sp.set_defaults(func=cmd_mem_profile)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
