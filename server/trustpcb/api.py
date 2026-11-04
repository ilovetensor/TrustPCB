"""Loopback-only HTTP boundary for TrustPCB.

The routes that do review work call one named function in a service module, looked up when the
request arrives:

  trustpcb.intake.service.ingest(*, store, version, upload_path, filename) -> dict | None
      Called by POST /api/projects/{id}/versions after the upload was streamed to disk. `version`
      is the freshly created versions row (status "uploaded"); unpack into store.version_dir(version["id"]).
      `upload_path` is the zip file, or a directory holding the uploaded files (folder drag, or a
      single non-zip file). May be sync or async. Raise ValueError for a bad upload (HTTP 422).
      Return None, or a dict of versions fields to store (status, stackup, detection, label).
      The temporary upload folder is deleted after the call, so copy what you keep.
  trustpcb.review.service.start_review(*, store, manager, version_id, goal, depth, extent) -> dict
  trustpcb.review.extent_api.extent_options(*, store, version_id) -> dict
  trustpcb.review.service.restart_review(*, store, manager, review_id) -> dict
  trustpcb.review.sims.list_sims(*, store, review_id) -> list
  trustpcb.review.followup.start_thread(*, store, manager, review_id, body) -> dict
  trustpcb.review.followup.send_message(*, store, manager, thread_id, text, selected, request_id) -> dict
  trustpcb.review.report.render_revision(*, store, review, revision, fmt) -> bytes     fmt: html | pdf
  trustpcb.review.compare.compare_versions(*, store, from_id, to_id) -> dict

Each may be sync or async. A ValueError becomes HTTP 400, SessionConflict becomes 409.
"""
import asyncio
import hashlib
import hmac
import importlib
import inspect
import logging
import mimetypes
import os
import re
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from python_multipart.multipart import MultipartParser, parse_options_header

from . import settings_store
from .datalock import DataFolderInUse, DataLock
from .events import KEEPALIVE_SECONDS, POLL_SECONDS, sse_events
from .sessions import SessionConflict, SessionManager
from .storage import Store

logger = logging.getLogger('trustpcb')
REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_UPLOAD_BYTES = int(float(os.getenv('TRUSTPCB_MAX_UPLOAD_MB', '512')) * 1024 * 1024)
MAX_UPLOAD_FILES = 5000
MAX_FIELD_BYTES = 8 * 1024
TOKEN_HEADER = 'x-trustpcb-token'
BACKEND_PORT = int(os.environ.get('TRUSTPCB_BACKEND_PORT', '8118'))
ALLOWED_HOSTS = {f'127.0.0.1:{BACKEND_PORT}', f'localhost:{BACKEND_PORT}', f'[::1]:{BACKEND_PORT}'}
ALLOWED_ORIGINS = {
    'http://127.0.0.1:3118', 'http://localhost:3118', 'http://[::1]:3118',
    f'http://127.0.0.1:{BACKEND_PORT}', f'http://localhost:{BACKEND_PORT}', f'http://[::1]:{BACKEND_PORT}',
}
FINDING_STATUS_CHANGES = {'dismissed', 'open'}
MUTATING_METHODS = {'POST', 'PATCH', 'PUT', 'DELETE'}
DEMO_READONLY_MESSAGE = ('This is a read-only demo: uploading, starting reviews and every other change are switched off. '
                         'Run TrustPCB on your own machine to review your own board (see the repository README).')


_kicad_ok: tuple[str, str] | None = None      # (KICAD_CLI_PATH it was found with, version)


async def kicad_status() -> dict:
    """Is a usable kicad-cli installed? Reported in /api/bootstrap and logged at start-up; a missing KiCad never stops the app."""
    global _kicad_ok
    configured = os.getenv('KICAD_CLI_PATH', '')
    if _kicad_ok and _kicad_ok[0] == configured:
        return {'available': True, 'version': _kicad_ok[1], 'reason': None}
    from .pcb import kicad_cli
    try:
        found = '.'.join(str(part) for part in await kicad_cli.version())
        _kicad_ok = (configured, found)
        return {'available': True, 'version': found, 'reason': None}
    except Exception as exc:  # missing, wrong version, not runnable
        logger.debug('kicad-cli is not usable: %s', exc, exc_info=True)
        return {'available': False, 'version': None, 'reason': str(exc)[:300]}


async def warn_if_kicad_missing() -> None:
    status = await kicad_status()
    if not status['available']:
        logger.warning('kicad-cli is not available (%s). Uploads still work, but KiCad checks, statistics and the 3D model are skipped. '
                       'Set KICAD_CLI_PATH or install KiCad 9 or newer.', status['reason'])


def token_matches(given: str, expected: str) -> bool:
    """Constant-time token check. Compares bytes, because a header with non-ASCII text would make a str comparison raise."""
    return hmac.compare_digest(given.encode('utf-8'), expected.encode('utf-8'))


def demo_readonly() -> bool:
    """TRUSTPCB_DEMO_READONLY=1 turns the backend into a viewer of a finished data directory: every POST, PATCH, PUT and DELETE is refused with 403."""
    return os.getenv('TRUSTPCB_DEMO_READONLY', '').strip().lower() in {'1', 'true', 'yes', 'on'}


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ReviewBody(BaseModel):
    goal: dict = Field(default_factory=dict)
    depth: str = Field(default='standard', max_length=40)
    extent: list[str] | None = Field(default=None, max_length=12)       # the checks chosen at the start (layout, antenna, power, thermal, signal, datasheets); omitted means the default


class ThreadBody(BaseModel):
    level: str | None = Field(default=None, max_length=40)


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    selected: list[dict] = Field(default_factory=list, max_length=50)
    request_id: str = Field(min_length=1, max_length=200)


class ResponseBody(BaseModel):
    answers: dict[str, str] | None = None
    allow: bool | None = None


class KeyBody(BaseModel):
    value: str = Field(min_length=1, max_length=settings_store.MAX_VALUE_CHARS)


class FindingPatch(BaseModel):
    status: str
    reason: str | None = Field(default=None, max_length=2000)


def default_data_dir() -> Path:
    configured = os.getenv('TRUSTPCB_DATA_DIR')
    return Path(configured).expanduser() if configured else REPO_ROOT / '.trustpcb'


def safe_relative_name(raw: str) -> str:
    """A browser or client file name as a safe relative path (no traversal, no absolute parts)."""
    parts = [part for part in PurePosixPath(raw.replace('\\', '/').replace('\0', '')).parts if part not in ('', '.', '/')]
    if any(part == '..' for part in parts):
        raise ValueError('Upload file name may not contain ..')
    if any(len(part) > 255 for part in parts) or sum(len(part) + 1 for part in parts) > 1000:
        raise ValueError('Upload file name is too long')
    return '/'.join(parts) or 'upload.bin'


class UploadSink:
    """Streams multipart or raw upload bytes to disk, enforcing size and count limits as bytes arrive."""

    def __init__(self, directory: Path, limit: int | None = None, max_files: int | None = None):
        self.root = directory / 'files'
        self.root.mkdir(parents=True, exist_ok=True)
        self.limit, self.max_files = limit or MAX_UPLOAD_BYTES, max_files or MAX_UPLOAD_FILES
        self.total = 0
        self.hasher = hashlib.sha256()
        self.files: dict[str, int] = {}
        self.fields: dict[str, str] = {}
        self._handle = None
        self._relative = None
        self._field_name = None
        self._field_bytes = bytearray()
        self._headers: dict[str, str] = {}
        self._header_name = bytearray()
        self._header_value = bytearray()

    def count(self, size: int):
        self.total += size
        if self.total > self.limit:
            raise HTTPException(413, f'Upload exceeds the {self.limit // (1024 * 1024)} MiB limit')

    def open_file(self, name: str):
        relative = safe_relative_name(name)
        if relative in self.files:
            raise ValueError(f'Duplicate file in upload: {relative}')
        if len(self.files) >= self.max_files:
            raise HTTPException(413, f'Upload has more than {self.max_files} files')
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError('Upload file name escapes the upload folder')
        target.parent.mkdir(parents=True, exist_ok=True)
        self._handle, self._relative = target.open('wb'), relative
        self.files[relative] = 0
        self.hasher.update(relative.encode() + b'\0')

    def write_file(self, data: bytes):
        self.count(len(data))
        self.hasher.update(data)
        self.files[self._relative] += len(data)
        self._handle.write(data)

    def close_file(self):
        if self._handle:
            self._handle.close()
            self._handle = None

    # python-multipart callbacks -----------------------------------------------------------
    def on_part_begin(self):
        self._headers, self._field_name, self._field_bytes = {}, None, bytearray()

    def on_header_field(self, data, start, end):
        self._header_name += data[start:end]

    def on_header_value(self, data, start, end):
        self._header_value += data[start:end]

    def on_header_end(self):
        self._headers[self._header_name.decode('latin-1').lower()] = self._header_value.decode('utf-8', 'replace')
        self._header_name, self._header_value = bytearray(), bytearray()

    def on_headers_finished(self):
        _, options = parse_options_header(self._headers.get('content-disposition', ''))
        name = options.get(b'name', b'').decode('utf-8', 'replace')
        filename = options.get(b'filename')
        if filename is not None:
            self.open_file(filename.decode('utf-8', 'replace'))
        else:
            self._field_name = name

    def on_part_data(self, data, start, end):
        chunk = data[start:end]
        if self._handle:
            self.write_file(chunk)
        elif self._field_name is not None:
            self.count(len(chunk))
            self._field_bytes += chunk
            if len(self._field_bytes) > MAX_FIELD_BYTES:
                raise ValueError('Form field is too large')

    def on_part_end(self):
        if self._handle:
            self.close_file()
        elif self._field_name is not None:
            self.fields[self._field_name] = self._field_bytes.decode('utf-8', 'replace')


async def receive_upload(request: Request, directory: Path) -> UploadSink:
    """Stream the request body into `directory`. Multipart (one or many files) or one raw body."""
    declared = request.headers.get('content-length')
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f'Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit')
    sink = UploadSink(directory)
    content_type, options = parse_options_header(request.headers.get('content-type', ''))
    try:
        if content_type == b'multipart/form-data':
            boundary = options.get(b'boundary')
            if not boundary:
                raise ValueError('Multipart upload has no boundary')
            parser = MultipartParser(boundary, {
                'on_part_begin': sink.on_part_begin, 'on_header_field': sink.on_header_field, 'on_header_value': sink.on_header_value,
                'on_header_end': sink.on_header_end, 'on_headers_finished': sink.on_headers_finished,
                'on_part_data': sink.on_part_data, 'on_part_end': sink.on_part_end,
            })
            async for chunk in request.stream():
                parser.write(chunk)
            parser.finalize()
        elif content_type == b'application/x-www-form-urlencoded':
            raise ValueError('Send the board as multipart/form-data or a raw zip body')
        else:
            name = request.headers.get('x-filename') or request.query_params.get('filename') or 'upload.zip'
            sink.open_file(name)
            async for chunk in request.stream():
                if chunk:
                    sink.write_file(chunk)
            sink.close_file()
    except BaseException:
        sink.close_file()
        raise
    sink.close_file()
    return sink


def install_real_lab(store):
    """Give new reviews the real RF lab, unless a lab was already installed (tests) or TRUSTPCB_LAB=baseline.

    Jobs an earlier process left queued or running are marked interrupted. The lab checks Docker and the solver image
    (TRUSTPCB_EMS_IMAGE, default trustpcb-ems:dev) when it is asked, and reports RF as Not tested with the reason when
    either is missing, so starting the app never needs Docker."""
    from .review import service
    if service.CONFIG.lab is not None or os.getenv('TRUSTPCB_LAB', '').lower() == 'baseline':
        return None
    from .review.lab_real import RealLab
    lab = RealLab(store)
    if lab.queue.owns_data_dir():
        lab.queue.recover_interrupted()
    service.CONFIG.lab = lab
    return lab


async def uninstall_real_lab(lab):
    from .review import service
    await lab.queue.close()
    if service.CONFIG.lab is lab:
        service.CONFIG.lab = None


def create_app(data_dir: Path | None = None, token: str | None = None, runner_factory=None,
               poll: float = POLL_SECONDS, keepalive: float = KEEPALIVE_SECONDS) -> FastAPI:
    launch_token = token or os.getenv('TRUSTPCB_TOKEN') or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        folder = data_dir if data_dir is not None else default_data_dir()
        # One backend per data folder. The owner alone may mark unfinished work interrupted and stop leftover solver containers;
        # a second backend on the same folder is refused, so it can never stop the first one's solver or touch its running reviews.
        # The read-only demo only reads a finished folder, so it needs no lock and changes nothing.
        lock = None
        if not demo_readonly():
            lock = DataLock(folder, port=BACKEND_PORT)
            try:
                lock.acquire()
            except DataFolderInUse as exc:
                logger.error('%s', exc)
                raise
        store = Store(folder)
        if lock is not None:
            store.recover_interrupted()
        manager = SessionManager(store, runner_factory=runner_factory)
        app.state.store = store
        app.state.manager = manager
        app.state.lock = lock
        settings_store.configure(folder)
        lab = install_real_lab(store)
        if lab is not None and lock is not None:
            killed = await lab.queue.kill_orphans()
            if killed:
                logger.warning('Stopped %d solver container(s) an earlier run left behind.', killed)
        check = asyncio.create_task(warn_if_kicad_missing())
        try:
            yield
        finally:
            check.cancel()
            try:
                await manager.shutdown()        # runs end as interrupted (not stopped), without a report: the app stopping is not a user pressing Stop
                if lab is not None:
                    await uninstall_real_lab(lab)    # stops the running solver container; finished simulations stay cached
            finally:
                if lock is not None:
                    lock.release()

    app = FastAPI(title='TrustPCB', lifespan=lifespan)
    app.state.token = launch_token

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if request.headers.get('host', '') not in ALLOWED_HOSTS:
            return JSONResponse({'detail': 'Host is not allowed'}, status_code=403)
        origin = request.headers.get('origin')
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return JSONResponse({'detail': 'Origin is not allowed'}, status_code=403)
        if request.url.path not in {'/health', '/api/health'} and not token_matches(request.headers.get(TOKEN_HEADER, ''), launch_token):
            return JSONResponse({'detail': 'Authorization required'}, status_code=401)
        if request.method in MUTATING_METHODS and demo_readonly():
            return JSONResponse({'detail': DEMO_READONLY_MESSAGE, 'code': 'demo_readonly'}, status_code=403)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        errors = ['.'.join(str(part) for part in error['loc']) + ': ' + error['msg'] for error in exc.errors()]
        return JSONResponse({'detail': '; '.join(errors)}, status_code=422)

    @app.exception_handler(SessionConflict)
    async def conflict(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    # ---- helpers ----------------------------------------------------------------------

    def store() -> Store:
        return app.state.store

    def manager() -> SessionManager:
        return app.state.manager

    def found(row, what):
        if row is None:
            raise HTTPException(404, f'{what} not found')
        return row

    def hook(module: str, name: str):
        """The service function a route calls, looked up when the request arrives."""
        return getattr(importlib.import_module(module), name)

    async def call_hook(function, **kwargs):
        """Run a service function that may be sync (in a thread) or async."""
        if inspect.iscoroutinefunction(function):
            return await function(**kwargs)
        result = await asyncio.to_thread(function, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def review_row(id):
        return found(store().get_review(id), 'Review')

    def thread_row(id):
        return found(store().get_thread(id), 'Thread')

    def sse_response(chat_id, request: Request, after: int):
        header_cursor = request.headers.get('last-event-id')
        if header_cursor:
            try:
                after = max(after, int(header_cursor))
            except ValueError:
                raise HTTPException(400, 'Invalid event cursor') from None
        return StreamingResponse(
            sse_events(store(), chat_id, max(0, after), request.is_disconnected, poll=poll, keepalive=keepalive),
            media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    def review_summary(review):
        return {**review, 'counts': store().severity_counts(review['id']), 'chat_status': (store().get_chat(review['chat_id']) or {}).get('status'),
                'live': manager().run_info(review['chat_id'])}

    def live_runs() -> list[dict]:
        """Every review that is running or waiting for a review slot right now, with where to open it."""
        rows = []
        for turn in list(manager().turns.values()):
            review = store().get_review_by_chat(turn.chat_id)
            if review is None:               # a follow-up thread, not a review
                continue
            info = manager().run_info(turn.chat_id) or {}
            project, version = store().get_project(review['project_id']) or {}, store().get_version(review['version_id']) or {}
            rows.append({'review_id': review['id'], 'project_id': review['project_id'], 'project_name': project.get('name'), 'version_id': review['version_id'],
                         'version_label': version.get('label') or f"v{version.get('number', '')}", 'depth': review['depth'], 'extent': review.get('extent'), 'phase': review['phase'], 'started_at': review['created_at'],
                         'state': info.get('state', 'running'), 'position': info.get('position'), 'waiting_for_solver': bool(info.get('waiting_for_solver'))})
        rows.sort(key=lambda r: (r['state'] == 'queued', r['position'] or 0, r['started_at']))
        return rows

    # ---- health, bootstrap ------------------------------------------------------------

    @app.get('/health')
    @app.get('/api/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/api/bootstrap')
    async def bootstrap():
        from .runners import available, profile_names, resolve_profile
        try:
            profile = resolve_profile()
        except Exception as exc:  # an unusable default must not break the page
            logger.warning('The default runner profile cannot be used: %s', exc, exc_info=True)
            profile = {'name': None, 'error': str(exc)[:300]}
        return {'projects': store().list_projects(), 'profile': profile, 'max_reviews': manager().max_reviews,
                'runners': {name: available(name) for name in profile_names()}, 'max_upload_bytes': MAX_UPLOAD_BYTES,
                'kicad': await kicad_status(), 'demo_readonly': demo_readonly(), 'repo_url': os.getenv('TRUSTPCB_REPO_URL') or None}

    # ---- optional service keys (write-only: a value is never returned) -----------------

    def key_rows() -> list[dict]:
        status = settings_store.secret_status()
        return [{'name': name, 'service': info['service'], 'label': info['label'], 'get_url': info['url'], **status[name]}
                for name, info in settings_store.KEYS.items()]

    @app.get('/api/settings/keys')
    async def key_settings():
        if demo_readonly():                       # a public demo shows nothing about the owner's keys
            return {'read_only': True, 'keys': [], 'parts_available': False, 'model_available': False}
        from .parts import available
        from .runners.muse import configured
        return {'read_only': False, 'keys': key_rows(), 'parts_available': available(), 'model_available': configured()}

    @app.put('/api/settings/keys/{name}')
    async def put_key(name: str, body: KeyBody):
        if name not in settings_store.KEYS:
            raise HTTPException(404, 'Unknown key name')
        await asyncio.to_thread(settings_store.set_secret, name, body.value)
        return {'keys': key_rows()}

    @app.delete('/api/settings/keys/{name}')
    async def delete_key(name: str):
        if name not in settings_store.KEYS:
            raise HTTPException(404, 'Unknown key name')
        await asyncio.to_thread(settings_store.clear_secret, name)
        return {'keys': key_rows()}

    # ---- projects and versions --------------------------------------------------------

    @app.post('/api/projects')
    async def add_project(body: ProjectBody):
        return store().create_project(body.name)

    @app.get('/api/projects')
    async def list_projects():
        running: dict[str, int] = {}
        for run in live_runs():
            running[run['project_id']] = running.get(run['project_id'], 0) + 1
        return [{**project, 'version_count': len(store().list_versions(project['id'])), 'live_reviews': running.get(project['id'], 0)} for project in store().list_projects()]

    @app.get('/api/runs')
    async def runs():
        """Reviews running or waiting for a slot, for the sidebar, the Projects page and the Project page. Cheap to poll."""
        lab = getattr(importlib.import_module('trustpcb.review.service'), 'CONFIG').lab
        queue = getattr(lab, 'queue', None)
        solver = queue.snapshot() if queue is not None and hasattr(queue, 'snapshot') else None
        return {'limit': manager().max_reviews, 'working': manager().working_reviews(), 'waiting': len(manager().waiting), 'runs': live_runs(),
                'solver': solver and {'running_review': (solver['running'] or {}).get('review_id'), 'waiting': len(solver['waiting'])}}

    @app.get('/api/projects/{id}')
    async def get_project(id: str):
        project = found(store().get_project(id), 'Project')
        reviews = store().list_reviews(project_id=id)
        by_version: dict[str, list] = {}
        for review in reviews:
            by_version.setdefault(review['version_id'], []).append(review_summary(review))
        versions = [{**version, 'reviews': by_version.get(version['id'], [])} for version in store().list_versions(id)]
        trend = [{'version_id': v['id'], 'number': v['number'], 'counts': v['reviews'][-1]['counts']} for v in versions if v['reviews']]
        latest = reviews[-1] if reviews else None
        return {**project, 'versions': versions, 'dashboard': {
            'last_review': review_summary(latest) if latest else None,
            'open_by_severity': store().severity_counts(latest['id']) if latest else None,
            'trend': trend,
        }}

    @app.post('/api/projects/{id}/versions')
    async def upload_version(id: str, request: Request):
        found(store().get_project(id), 'Project')
        ingest = hook('trustpcb.intake.service', 'ingest')
        upload_dir = store().upload_dir(uuid.uuid4().hex)
        try:
            sink = await receive_upload(request, upload_dir)
            if not sink.files:
                raise ValueError('The upload contains no file')
            if sink.total == 0:
                raise ValueError('The upload is empty')
            names = list(sink.files)
            if len(names) == 1 and names[0].lower().endswith('.zip'):
                upload_path, filename = sink.root / names[0], names[0]
            else:
                upload_path, filename = sink.root, sink.fields.get('name') or (names[0] if len(names) == 1 else 'folder')
            digest = sink.hasher.hexdigest()
            duplicate = store().find_version_by_hash(id, digest)
            label = (sink.fields.get('label') or request.query_params.get('label') or '').strip()[:200] or None
            version = store().create_version(id, label=label, upload_hash=digest)
            try:
                result = await call_hook(ingest, store=store(), version=version, upload_path=upload_path, filename=filename)
            except ValueError as exc:
                store().update_version(version['id'], status='failed', error=str(exc)[:2000])
                return JSONResponse({'detail': str(exc), 'version_id': version['id']}, status_code=422)
            except Exception as exc:
                store().update_version(version['id'], status='failed', error=f'{type(exc).__name__}: {exc}'[:2000])
                raise
            if isinstance(result, dict):
                changes = {key: value for key, value in result.items() if key in {'status', 'stackup', 'detection', 'label'}}
                if changes:
                    store().update_version(version['id'], **changes)
            return {**store().get_version(version['id']), 'duplicate_of': duplicate['id'] if duplicate else None}
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)

    @app.get('/api/versions/{id}')
    async def get_version(id: str):
        version = found(store().get_version(id), 'Version')
        artifacts = store().list_artifacts('version', id)
        sims_available = {}
        if version.get('status') != 'failed':
            try:
                sims_available = await call_hook(hook('trustpcb.review.extent_api', 'sims_available'), store=store(), version_id=id)
            except Exception:
                logger.warning('Version %s: the board simulations could not be checked for the version page', id, exc_info=True)
        return {**version, 'sims_available': sims_available, 'artifacts': [{key: item[key] for key in ('id', 'type', 'mime', 'size', 'meta')} | {'url': f"/api/artifacts/{item['id']}"} for item in artifacts],
                'reviews': [review_summary(review) for review in store().list_reviews(version_id=id)]}

    @app.get('/api/versions/{id}/extent')
    async def review_extent_options(id: str):
        """What this board offers at review start: each check with whether it is available (and why not) and the default choice."""
        found(store().get_version(id), 'Version')
        function = hook('trustpcb.review.extent_api', 'extent_options')
        return await call_hook(function, store=store(), version_id=id)

    @app.post('/api/versions/{id}/reviews')
    async def start_review(id: str, body: ReviewBody):
        found(store().get_version(id), 'Version')
        function = hook('trustpcb.review.service', 'start_review')
        return await call_hook(function, store=store(), manager=manager(), version_id=id, goal=body.goal, depth=body.depth, extent=body.extent)

    # ---- reviews ----------------------------------------------------------------------

    @app.get('/api/reviews/{id}')
    async def get_review(id: str):
        review = review_row(id)
        latest = store().latest_report_revision(id)
        return {**review_summary(review), 'threads': store().list_threads(id), 'report_revision': latest['number'] if latest else None}

    @app.get('/api/reviews/{id}/sims')
    async def review_sims(id: str):
        review_row(id)
        function = hook('trustpcb.review.sims', 'list_sims')
        return await call_hook(function, store=store(), review_id=id)

    @app.get('/api/reviews/{id}/sims/{run}')
    async def sim_run(id: str, run: str):
        """One board-simulation run of this review with its result, by run id. Read only."""
        from .review.sims import RUN_HASH, board_run
        review_row(id)
        if not RUN_HASH.fullmatch(run):
            raise HTTPException(404, 'Not a simulation run')
        return found(board_run(store(), id, run), 'Simulation run')

    @app.get('/api/reviews/{id}/sims/{run}/overlay/{oid}')
    async def sim_overlay(id: str, run: str, oid: str):
        """One overlay raster of a board-simulation run of this review: float32 little-endian [ny, nx], NaN where nothing exists. Read only."""
        from .review.sims import board_run_folder, overlay_file
        review_row(id)
        folder = board_run_folder(store(), id, run)
        path = overlay_file(folder, oid) if folder else None
        if path is None:
            raise HTTPException(404, 'Overlay is not available')
        return FileResponse(path, media_type='application/octet-stream', headers={
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, max-age=3600', 'Content-Security-Policy': "sandbox; default-src 'none'"})

    @app.get('/api/reviews/{id}/sims/{run}/curve/{name}')
    async def sim_curve(id: str, run: str, name: str):
        """A curve file (JSON) that a board simulation stored next to its result."""
        from .review.sims import board_run_folder, curve_file
        review_row(id)
        folder = board_run_folder(store(), id, run)
        path = curve_file(folder, name) if folder else None
        if path is None:
            raise HTTPException(404, 'Curve is not available')
        return FileResponse(path, media_type='application/json', headers={
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, max-age=3600', 'Content-Security-Policy': "sandbox; default-src 'none'"})

    @app.get('/api/sim/{job_id}/files/{name}')
    async def sim_file(job_id: str, name: str):
        """One processed file of a simulation job: only the names in `review.sims.SIM_FILES`."""
        from .review.sims import MEDIA, SIM_FILES, job_folder
        if name not in SIM_FILES:
            raise HTTPException(404, 'Not a simulation file')
        job = found(store().get_sim_job(job_id), 'Simulation job')
        folder = job_folder(store(), job)
        path = folder / name if folder else None
        if path is None or not path.is_file():
            raise HTTPException(404, 'Simulation file is not available')
        cache = 'private, max-age=3600' if job['status'] == 'done' else 'no-store'
        return FileResponse(path, media_type=MEDIA.get(path.suffix, 'application/octet-stream'), headers={
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': cache, 'Content-Security-Policy': "sandbox; default-src 'none'"})

    @app.get('/api/reviews/{id}/events')
    async def review_events(id: str, after: int = 0):
        return store().events(review_row(id)['chat_id'], max(0, after))

    @app.get('/api/reviews/{id}/stream')
    async def review_stream(id: str, request: Request, after: int = 0):
        return sse_response(review_row(id)['chat_id'], request, after)

    @app.post('/api/reviews/{id}/cancel')
    async def cancel_review(id: str):
        return await manager().cancel(review_row(id)['chat_id'])

    @app.post('/api/reviews/{id}/restart')
    async def restart_review(id: str):
        """Run an interrupted review again: a new review of the same version, goal and depth, which reuses every stored simulation."""
        review_row(id)
        function = hook('trustpcb.review.service', 'restart_review')
        return await call_hook(function, store=store(), manager=manager(), review_id=id)

    @app.post('/api/reviews/{id}/threads')
    async def start_thread(id: str, body: ThreadBody):
        review_row(id)
        function = hook('trustpcb.review.followup', 'start_thread')
        return await call_hook(function, store=store(), manager=manager(), review_id=id, body=body.model_dump())

    @app.get('/api/reviews/{id}/findings')
    async def review_findings(id: str, status: str | None = None):
        review_row(id)
        return store().list_findings(id, status)

    @app.get('/api/reviews/{id}/assumptions')
    async def review_assumptions(id: str):
        review_row(id)
        return store().list_assumptions(id)

    @app.get('/api/reviews/{id}/report')
    async def latest_report(id: str):
        review_row(id)
        return found(store().latest_report_revision(id), 'Report')

    @app.get('/api/reviews/{id}/report/revisions')
    async def report_revisions(id: str):
        review_row(id)
        return store().list_report_revisions(id)

    @app.get('/api/reviews/{id}/report/{name}')
    async def report_file(id: str, name: str):
        review = review_row(id)
        match = re.fullmatch(r'(\d+)\.(html|pdf|json)', name)
        if not match:
            raise HTTPException(404, 'Report file must look like 3.json, 3.html or 3.pdf')
        revision = found(store().get_report_revision(id, int(match.group(1))), 'Report revision')
        fmt = match.group(2)
        if fmt == 'json':
            return JSONResponse(revision['snapshot'], headers={'Content-Disposition': f'inline; filename="report-r{revision["number"]}.json"'})
        function = hook('trustpcb.review.report', 'render_revision')
        body = await call_hook(function, store=store(), review=review, revision=revision, fmt=fmt)
        media = 'text/html; charset=utf-8' if fmt == 'html' else 'application/pdf'
        headers = {'X-Content-Type-Options': 'nosniff'}
        if fmt == 'html':
            headers['Content-Security-Policy'] = "sandbox allow-same-origin; default-src 'none'; img-src data: 'self'; style-src 'unsafe-inline'"
        return Response(body, media_type=media, headers=headers)

    # ---- findings ---------------------------------------------------------------------

    @app.get('/api/findings/{id}/history')
    async def finding_history(id: str):
        found(store().get_finding(id), 'Finding')
        return store().finding_history(id)

    @app.patch('/api/findings/{id}')
    async def patch_finding(id: str, body: FindingPatch):
        finding = found(store().get_finding(id), 'Finding')
        if body.status not in FINDING_STATUS_CHANGES:
            raise HTTPException(400, 'status must be dismissed or open')
        if body.status == 'dismissed':
            reason = (body.reason or '').strip()
            if not reason:
                raise HTTPException(400, 'A reason is required to dismiss a finding')
            updated = store().update_finding(id, 'user', reason, status='dismissed', dismissed_reason=reason, needs_recheck=0)
        else:
            updated = store().update_finding(id, 'user', body.reason or 'reopened', status='open', dismissed_reason=None)
        review = store().get_review(finding['review_id'])
        if review:
            store().append_event(review['chat_id'], None, 'finding_changed', {'finding_id': id, 'code': updated['code'], 'status': updated['status'], 'source': 'user'})
        return updated

    # ---- compare, artifacts -----------------------------------------------------------

    @app.get('/api/compare')
    async def compare(request: Request):
        from_id, to_id = request.query_params.get('from'), request.query_params.get('to')
        if not from_id or not to_id:
            raise HTTPException(400, 'Pass from=<version id>&to=<version id>')
        found(store().get_version(from_id), 'Version')
        found(store().get_version(to_id), 'Version')
        function = hook('trustpcb.review.compare', 'compare_versions')
        return await call_hook(function, store=store(), from_id=from_id, to_id=to_id)

    @app.get('/api/artifacts/{id}')
    async def get_artifact(id: str):
        artifact = found(store().get_artifact(id), 'Artifact')
        path = store().artifact_path(artifact)
        if not path.is_file():
            raise HTTPException(404, 'Artifact file is missing')
        media = artifact.get('mime') or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        return FileResponse(path, media_type=media, headers={
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, max-age=60',
            'Content-Security-Policy': "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"})

    # ---- threads ----------------------------------------------------------------------

    @app.post('/api/threads/{id}/messages')
    async def thread_message(id: str, body: MessageBody):
        thread_row(id)
        function = hook('trustpcb.review.followup', 'send_message')
        return await call_hook(function, store=store(), manager=manager(), thread_id=id, text=body.text,
                               selected=body.selected, request_id=body.request_id)

    @app.get('/api/threads/{id}/events')
    async def thread_events(id: str, after: int = 0):
        return store().events(thread_row(id)['chat_id'], max(0, after))

    @app.get('/api/threads/{id}/stream')
    async def thread_stream(id: str, request: Request, after: int = 0):
        return sse_response(thread_row(id)['chat_id'], request, after)

    @app.post('/api/threads/{id}/cancel')
    async def cancel_thread(id: str):
        return await manager().cancel(thread_row(id)['chat_id'])

    @app.post('/api/requests/{id}/respond')
    async def respond(id: str, body: ResponseBody):
        return await manager().respond(id, allow=body.allow, answers=body.answers)

    return app


app = create_app()
