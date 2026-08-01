"""FastAPI REST 服务入口。

HTTP 层薄封装：
- 接收 URL，创建 job，返回 job_id
- 实际下载委托给 cli.main.download_url 的简化复用

fastapi/uvicorn 是**可选**依赖。若未安装，导入本模块会 ImportError。
"""

from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import CookieManager
from config import ConfigLoader
from control import QueueManager, RateLimiter, RetryHandler
from core import DouyinAPIClient, DownloaderFactory, URLParser
from core.downloader_base import BaseDownloader
from server.jobs import JobManager
from storage import FileManager
from utils.logger import setup_logger
from utils.validators import is_short_url, normalize_short_url

logger = setup_logger("REST")


class DownloadRequest(BaseModel):
    url: str


class JobResponse(BaseModel):
    job_id: str
    status: str
    url: str


class ResolveRequest(BaseModel):
    url: str


class ResolveResponse(BaseModel):
    # camelCase on the wire — the NestJS client parses these exact names.
    awemeId: str
    durationSeconds: float
    coverUrl: Optional[str] = None
    title: Optional[str] = None


class _ServerDeps:
    """跨请求复用的重量级依赖。

    REST 服务在进程生命周期内只需要一份 FileManager / RateLimiter / RetryHandler /
    QueueManager / CookieManager；每个请求重新构造既浪费又会触发文件系统 mkdir。
    DouyinAPIClient 由于持有 aiohttp.ClientSession，依旧按请求创建，避免跨请求泄漏
    连接状态或触发 "Session is closed" 错误。
    """

    def __init__(self, config: ConfigLoader):
        self.config = config
        # Resolve the cookie file path relative to the config file's directory
        # so the sidecar can find it regardless of its working directory (which
        # on macOS is often '/' when launched by Electron).
        if config.config_path:
            from pathlib import Path

            cookie_file = str(Path(config.config_path).resolve().parent / ".cookies.json")
        else:
            cookie_file = ".cookies.json"
        self.cookie_manager = CookieManager(cookie_file=cookie_file)
        # Load cookies from the config (env var / YAML cookie key) first, then
        # fall back to whatever is already on disk in the cookie file. This
        # ensures that cookies saved by a previous session are picked up on
        # restart even when the config doesn't embed them inline.
        initial_cookies = config.get_cookies()
        if initial_cookies:
            self.cookie_manager.set_cookies(initial_cookies)
        else:
            # Trigger a load from disk so get_cookies() returns the persisted
            # session without requiring a fresh login on every app restart.
            self.cookie_manager.get_cookies()
        self.file_manager = FileManager(config.get("path"))
        self.rate_limiter = RateLimiter(max_per_second=float(config.get("rate_limit", 2) or 2))
        self.retry_handler = RetryHandler(max_retries=int(config.get("retry_times", 3) or 3))
        self.queue_manager = QueueManager(max_workers=int(config.get("thread", 5) or 5))


# Internal manifest file written by MetadataHandler into the download root;
# it is bookkeeping, not a downloaded asset, so it is excluded from job files.
_INTERNAL_ARTIFACTS = {"download_manifest.jsonl"}


def _collect_job_files(job_root: Path) -> List[Dict[str, Any]]:
    """Enumerate the media/asset files a job wrote under its isolated dir.

    Each job downloads into ``<download_root>/<job_id>/`` so its output can be
    attributed unambiguously (jobs run concurrently). We walk that directory,
    skip internal artifacts / partial ``.tmp`` files / empty files, and return
    a deterministic, index-ordered list. Sorting by relative path naturally
    puts ``<stem>.mp4`` ahead of ``<stem>_cover.jpg`` / ``_music.mp3`` etc.
    """
    if not job_root.exists():
        return []
    files: List[Dict[str, Any]] = []
    for path in sorted(job_root.rglob("*"), key=lambda p: p.relative_to(job_root).as_posix()):
        if not path.is_file():
            continue
        if path.name in _INTERNAL_ARTIFACTS or path.suffix == ".tmp":
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        files.append(
            {
                "index": len(files),
                "bytes": size,
                "filename": path.name,
                "path": str(path.resolve()),
            }
        )
    return files


async def _execute_download(url: str, deps: "_ServerDeps", job_id: str) -> Dict[str, Any]:
    """简化版 download_url：执行下载并返回计数 + 产出文件清单。

    有意不复用 cli.main.download_url —— 后者绑定了 progress_display 的 rich 状态。
    API client 仍按请求创建（aiohttp session 不跨请求复用）；其余重量级依赖从
    _ServerDeps 共享。

    每个 job 下载到独立子目录 ``<download_root>/<job_id>/``，以便下载完成后能准确
    枚举该 job 产出的文件（供 GET /jobs/{id}/files/{index} 流式返回）。
    """
    job_root = (deps.file_manager.base_path / job_id).resolve()
    job_file_manager = FileManager(str(job_root))

    async with DouyinAPIClient(deps.cookie_manager.get_cookies()) as api_client:
        if is_short_url(url):
            resolved = await api_client.resolve_short_url(normalize_short_url(url))
            if not resolved:
                raise RuntimeError(f"Failed to resolve short URL: {url}")
            url = resolved

        parsed = URLParser.parse(url)
        if not parsed:
            raise RuntimeError(f"Unsupported URL: {url}")

        downloader = DownloaderFactory.create(
            parsed["type"],
            deps.config,
            api_client,
            job_file_manager,
            deps.cookie_manager,
            None,  # database 不在 server 场景里启用，避免单例冲突
            deps.rate_limiter,
            deps.retry_handler,
            deps.queue_manager,
            progress_reporter=None,
        )
        if downloader is None:
            raise RuntimeError(f"No downloader for url_type={parsed['type']}")

        result = await downloader.download(parsed)
        return {
            "total": result.total,
            "success": result.success,
            "failed": result.failed,
            "skipped": result.skipped,
            "files": _collect_job_files(job_root),
        }


async def _resolve_metadata(url: str, deps: "_ServerDeps") -> Dict[str, Any]:
    """Info-only resolve: fetch a single aweme's metadata, no media download.

    Reuses the same resolver path as downloads (short-link 302 follow →
    ``URLParser`` → ``DouyinAPIClient.get_video_detail``) so no signing /
    anti-bot logic is duplicated. Raises ``HTTPException`` with a 4xx status on
    invalid / unresolvable URLs (the NestJS side maps any 4xx → invalid_url).
    """
    async with DouyinAPIClient(deps.cookie_manager.get_cookies()) as api_client:
        if is_short_url(url):
            resolved = await api_client.resolve_short_url(normalize_short_url(url))
            if not resolved:
                raise HTTPException(status_code=404, detail=f"could not resolve short URL: {url}")
            url = resolved

        parsed = URLParser.parse(url)
        if not parsed:
            raise HTTPException(status_code=422, detail=f"unsupported URL: {url}")

        aweme_id = parsed.get("aweme_id")
        if not aweme_id:
            raise HTTPException(
                status_code=422,
                detail=f"URL is not a single video/note (type={parsed.get('type')})",
            )

        detail = await api_client.get_video_detail(str(aweme_id))
        if not detail:
            raise HTTPException(
                status_code=404, detail=f"metadata not found for aweme {aweme_id}"
            )

        return _extract_resolve_fields(detail)


def _extract_resolve_fields(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the camelCase resolve payload out of a raw aweme_detail dict.

    duration lives at ``video.duration`` (milliseconds) — divide by 1000 for
    seconds. ``desc`` is the title/caption. Cover comes from ``video.cover``
    with graceful fallback to ``origin_cover`` / ``dynamic_cover``.
    """
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}

    duration_ms = video.get("duration")
    if duration_ms is None:
        duration_ms = detail.get("duration")
    try:
        duration_seconds = float(duration_ms) / 1000.0 if duration_ms is not None else 0.0
    except (TypeError, ValueError):
        duration_seconds = 0.0

    title = (detail.get("desc") or "").strip() or None
    cover_url = (
        BaseDownloader._extract_first_url(video.get("cover"))
        or BaseDownloader._extract_first_url(video.get("origin_cover"))
        or BaseDownloader._extract_first_url(video.get("dynamic_cover"))
    )

    return {
        "awemeId": str(detail.get("aweme_id") or ""),
        "durationSeconds": duration_seconds,
        "coverUrl": cover_url,
        "title": title,
    }


def build_app(config: ConfigLoader) -> FastAPI:
    deps = _ServerDeps(config)

    async def executor(url: str, job_id: str) -> Dict[str, Any]:
        return await _execute_download(url, deps, job_id)

    server_cfg = config.get("server") or {}
    if not isinstance(server_cfg, dict):
        server_cfg = {}
    manager = JobManager(
        executor=executor,
        max_concurrency=int(config.get("thread", 2) or 2),
        max_jobs=int(server_cfg.get("max_jobs") or JobManager.DEFAULT_MAX_JOBS),
        job_ttl_seconds=float(
            server_cfg.get("job_ttl_seconds") or JobManager.DEFAULT_JOB_TTL_SECONDS
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(
        title="Douyin Downloader API",
        version="1.0",
        description="REST API for dispatching Douyin download jobs.",
        lifespan=lifespan,
    )
    app.state.job_manager = manager
    app.state.deps = deps

    @app.get("/api/v1/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/resolve", response_model=ResolveResponse)
    async def resolve(req: ResolveRequest) -> ResolveResponse:
        if not req.url or not req.url.strip():
            raise HTTPException(status_code=400, detail="url is required")
        try:
            data = await _resolve_metadata(req.url.strip(), deps)
        except HTTPException:
            raise
        except Exception as exc:  # network / upstream error → 502 (unreachable)
            logger.warning("resolve failed for %s: %s", req.url, exc)
            raise HTTPException(
                status_code=502, detail=f"upstream error: {type(exc).__name__}: {exc}"
            )
        return ResolveResponse(**data)

    @app.post("/api/v1/download", response_model=JobResponse)
    async def create_job(req: DownloadRequest) -> JobResponse:
        if not req.url:
            raise HTTPException(status_code=400, detail="url is required")
        job = await manager.submit(req.url)
        return JobResponse(job_id=job.job_id, status=job.status, url=job.url)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> Dict[str, Any]:
        job = await manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    @app.get("/api/v1/jobs/{job_id}/files/{index}")
    async def get_job_file(job_id: str, index: int) -> FileResponse:
        job = await manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if index < 0 or index >= len(job.files):
            raise HTTPException(status_code=404, detail="file index not found")
        entry = job.files[index]
        path = entry.get("path")
        filename = entry.get("filename") or f"{job_id}_{index}"
        if not path or not Path(path).is_file():
            raise HTTPException(status_code=404, detail="file no longer available on disk")
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=filename)

    @app.get("/api/v1/jobs")
    async def list_jobs() -> Dict[str, List[Dict[str, Any]]]:
        jobs = await manager.list_jobs()
        return {"jobs": [j.to_dict() for j in jobs]}

    return app


async def run_server(config: ConfigLoader, *, host: str, port: int) -> None:
    import uvicorn

    app = build_app(config)
    uv_config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(uv_config)
    await server.serve()
