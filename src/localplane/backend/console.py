"""Opt-in, bounded snapshot of the repository's Vite build, not a file server.

Only index.html and flat, fingerprinted assets are public. Source maps and other
files are never loaded. Routes mirror web/src/App.tsx, not an arbitrary SPA fallback.
Restart after replacing a build; requests never reopen filesystem paths.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import cached_property
from html.parser import HTMLParser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from localplane.backend.serving_files import ServingConfigurationError, pinned_directory, pinned_file

MAX_FILES = 512
MAX_ASSET_BYTES = 16 * 1024 * 1024
MAX_BUILD_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024
MEDIA_TYPES = {
    "js": "text/javascript", "css": "text/css", "woff2": "font/woff2",
    "woff": "font/woff", "ttf": "font/ttf", "otf": "font/otf",
    "svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "avif": "image/avif", "ico": "image/x-icon",
}
ASSET_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*-[A-Za-z0-9_-]{8,}\.(" + "|".join(MEDIA_TYPES) + r")")
SPA_ROUTES = (
    "/", "/index.html", "/network", "/workloads", "/workloads/runtime",
    "/system", "/storage", "/operations", "/operations/findings", "/settings",
    "/network/{object_id}", "/workloads/{object_id}", "/system/{object_id}",
    "/operations/findings/{finding_id}", "/operations/runs/{run_id}",
    "/operations/changes/{change_id}",
)
ID_PREFIXES = {"object_id": "obj", "finding_id": "(?:fnd|own)", "run_id": "run", "change_id": "chg"}


@dataclass(frozen=True)
class Asset:
    body: bytes
    media_type: str

    @cached_property
    def etag(self) -> str:
        return '"' + hashlib.sha256(self.body).hexdigest() + '"'


class _Index(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: set[str] = set()
        self.module = False
        self.root = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.root |= tag == "div" and values.get("id") == "root"
        if tag == "base":
            raise ValueError("build must use the same-origin root")
        for attr in ("src", "href"):
            if attr in values:
                value = values[attr]
                if not value or not value.startswith("/assets/") or not ASSET_NAME.fullmatch(value[8:]):
                    raise ValueError("non-build reference")
                self.references.add(value)
        if tag == "script":
            if values.get("type") != "module" or not str(values.get("src", "")).endswith(".js"):
                raise ValueError("not a compiled module")
            self.module = True


def _read_asset(directory: int, name: str, limit: int) -> bytes:
    with pinned_file(directory, name) as fd:
        if os.fstat(fd).st_size > limit:
            raise ValueError("build size limit")
        result = bytearray()
        while len(result) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        if not result or len(result) > limit:
            raise ValueError("empty or oversized build file")
        return bytes(result)


def load_console(path: Path) -> tuple[Asset, dict[str, Asset]]:
    try:
        with pinned_directory(path) as directory:
            index = _read_asset(directory, "index.html", MAX_INDEX_BYTES)
            assets: dict[str, Asset] = {}
            total = len(index)
            # Reopen relative to the pinned root, not the original path. This
            # also refuses a symlink substituted for the assets directory.
            assets_fd = os.open("assets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                info = os.fstat(assets_fd)
                if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
                    raise OSError("unsafe assets directory")
                with os.scandir(assets_fd) as entries:
                    for count, entry in enumerate(entries, 1):
                        if count > MAX_FILES:
                            raise ValueError("too many build files")
                        match = ASSET_NAME.fullmatch(entry.name)
                        if match is None:
                            continue  # Includes Vite source maps: never public.
                        body = _read_asset(assets_fd, entry.name, min(MAX_ASSET_BYTES, MAX_BUILD_BYTES - total))
                        total += len(body)
                        assets["/assets/" + entry.name] = Asset(body, MEDIA_TYPES[match[1]])
            finally:
                os.close(assets_fd)
        parsed = _Index()
        parsed.feed(index.decode("utf-8"))
        parsed.close()
        if not parsed.root or not parsed.module or not parsed.references <= assets.keys():
            raise ValueError("incomplete build")
        return Asset(index, "text/html"), assets
    except (OSError, ValueError, AssertionError):
        # HTMLParser can raise AssertionError for unsupported declarations;
        # never include its input-bearing diagnostic in the startup failure.
        raise ServingConfigurationError(
            "console_build_invalid",
            "Set LOCALPLANE_CONSOLE_DIR to a complete trusted web/dist build (index.html and fingerprinted assets); rebuild with the repository's web build command. Links, unsafe permissions, and oversized builds are refused.",
        ) from None


def install_console(app: FastAPI, path: Path, *, loaded: tuple[Asset, dict[str, Asset]] | None = None) -> None:
    index, assets = loaded if loaded is not None else load_console(path)

    def response(request: Request, asset: Asset, *, document: bool = False) -> Response:
        # No encoded separators/dot segments, even if an ASGI server decoded
        # them into an otherwise valid route. Query parameters remain available.
        raw = request.scope.get("raw_path", b"")
        if b"%" in raw or b"\\" in raw or b"//" in raw:
            raise HTTPException(404, "Not Found")
        headers = {
            "Cache-Control": "no-store" if document else "public, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        }
        if not document:
            headers["ETag"] = asset.etag
            matches = request.headers.get("if-none-match", "").split(",")
            if any(value.strip().removeprefix("W/") in {asset.etag, "*"} for value in matches):
                return Response(status_code=304, headers=headers)
        headers["Content-Length"] = str(len(asset.body))
        return Response(b"" if request.method == "HEAD" else asset.body, media_type=asset.media_type, headers=headers)

    def document(request: Request) -> Response:
        for parameter, value in request.path_params.items():
            if not re.fullmatch(ID_PREFIXES[parameter] + r"_[A-Za-z0-9_-]{1,128}", value):
                raise HTTPException(404, "Not Found")
        return response(request, index, document=True)

    def compiled_asset(request: Request, asset_name: str) -> Response:
        asset = assets.get("/assets/" + asset_name)
        if asset is None:
            raise HTTPException(404, "Not Found")
        return response(request, asset)

    # Register only actual console routes, after existing API/docs. No mount or
    # catch-all can turn an API method error, private path or typo into HTML.
    for route in SPA_ROUTES:
        app.add_api_route(route, document, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/assets/{asset_name}", compiled_asset, methods=["GET", "HEAD"], include_in_schema=False)
