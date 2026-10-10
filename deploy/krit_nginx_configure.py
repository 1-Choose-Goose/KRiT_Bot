#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from pathlib import Path


def _server_blocks(text: str) -> Iterable[tuple[int, int, str]]:
    for match in re.finditer(r"\bserver\s*\{", text):
        brace = text.find("{", match.start())
        depth = 0
        index = brace
        while index < len(text):
            character = text[index]
            if character == "#":
                newline = text.find("\n", index)
                index = len(text) if newline < 0 else newline
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    yield match.start(), end, text[match.start():end]
                    break
            index += 1


def _without_legacy_krit_locations(block: str) -> str:
    ranges: list[tuple[int, int]] = []
    for match in re.finditer(r"\blocation\b[^;{]*\{", block):
        header = match.group(0)
        if "/krit-api/" not in header:
            continue
        brace = block.find("{", match.start())
        depth = 0
        index = brace
        while index < len(block):
            character = block[index]
            if character == "#":
                newline = block.find("\n", index)
                index = len(block) if newline < 0 else newline
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    start = block.rfind("\n", 0, match.start()) + 1
                    end = index + 1
                    if end < len(block) and block[end] == "\n":
                        end += 1
                    ranges.append((start, end))
                    break
            index += 1
    for start, end in reversed(ranges):
        block = block[:start] + block[end:]
    return block


def _is_https_host(block: str, hostname: str) -> bool:
    host = re.escape(hostname)
    has_name = re.search(rf"\bserver_name\s+[^;]*\b{host}\b[^;]*;", block) is not None
    has_tls = re.search(r"\blisten\s+[^;]*443[^;]*\bssl\b[^;]*;", block) is not None
    return has_name and has_tls


def configure_nginx_site(
    hostname: str,
    *,
    sites: Iterable[Path],
    include_path: str,
) -> Path:
    if re.fullmatch(r"[A-Za-z0-9.-]+", hostname) is None:
        raise ValueError("Invalid hostname")
    include_line = f"include {include_path};"
    candidates: list[tuple[Path, int, int, str]] = []
    for site in sites:
        text = site.read_text(encoding="utf-8")
        for start, end, block in _server_blocks(text):
            if _is_https_host(block, hostname):
                candidates.append((site, start, end, text))
                break
    if not candidates:
        raise RuntimeError(f"HTTPS server block for {hostname} was not found")

    site, start, end, text = next(
        (item for item in candidates if item[0].name == "krit"),
        candidates[0],
    )
    block = _without_legacy_krit_locations(text[start:end])
    if include_line not in block:
        server_name = re.search(r"\bserver_name\s+[^;]+;", block)
        if server_name is None:
            raise RuntimeError("server_name directive was not found")
        insert_at = block.find("\n", server_name.end())
        if insert_at < 0:
            insert_at = server_name.end()
        block = block[:insert_at] + f"\n    {include_line}" + block[insert_at:]
    text = text[:start] + block + text[end:]
    site.write_text(text, encoding="utf-8", newline="\n")
    return site


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("hostname")
    parser.add_argument("--sites-enabled", type=Path, default=Path("/etc/nginx/sites-enabled"))
    parser.add_argument(
        "--include-path",
        default="/etc/nginx/snippets/krit-api.conf",
    )
    args = parser.parse_args()
    sites = sorted(path for path in args.sites_enabled.iterdir() if path.is_file())
    configured = configure_nginx_site(
        args.hostname,
        sites=sites,
        include_path=args.include_path,
    )
    for site in sites:
        if site.name == "krit" and site != configured and site.is_symlink():
            site.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
