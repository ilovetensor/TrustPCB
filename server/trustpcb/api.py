"""Loopback-only HTTP boundary for TrustPCB (spec section 18).

Routes that need work owned by another lane call one clearly named function in that lane's
module and answer 501 until the module and function exist. The hooks are:

  trustpcb.intake.service.ingest(*, store, version, upload_path, filename) -> dict | None
      Called by POST /api/projects/{id}/versions after the upload was streamed to disk. `version`
      is the freshly created versions row (status "uploaded"); unpack into store.version_dir(version["id"]).
      `upload_path` is the zip file, or a directory holding the uploaded files (folder drag, or a
      single non-zip file). May be sync or async. Raise ValueError for a bad upload (HTTP 422).
      Return None, or a dict of versions fields to store (status, stackup, detection, label).
      The temporary upload folder is deleted after the call, so copy what you keep.
  trustpcb.review.service.start_review(*, store, manager, version_id, goal, depth) -> dict
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
import importlib.util
import inspect
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

from .events import KEEPALIVE_SECONDS, POLL_SECONDS, sse_events
from .sessions import SessionConflict, SessionManager
from .storage import Store

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


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ReviewBody(BaseModel):
    goal: dict = Field(default_factory=dict)
    depth: str = Field(default='standard', max_length=40)


class ThreadBody(BaseModel):
    level: str | None = Field(default=None, max_length=40)


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    selected: list[dict] = Field(default_factory=list, max_length=50)
    request_id: str = Field(min_length=1, max_length=200)


class ResponseBody(BaseModel):
    answers: dict[str, str] | None = None
    allow: bool | None = None


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


def create_app(data_dir: Path | None = None, token: str | None = None, runner_factory=None,
               poll: float = POLL_SECONDS, keepalive: float = KEEPALIVE_SECONDS) -> FastAPI:
    launch_token = token or os.getenv('TRUSTPCB_TOKEN') or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        store = Store(data_dir if data_dir is not None else default_data_dir())
        store.recover_interrupted()
        manager = SessionManager(store, runner_factory=runner_factory)
        app.state.store = store
        app.state.manager = manager
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(title='TrustPCB', lifespan=lifespan)
    app.state.token = launch_token

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if request.headers.get('host', '') not in ALLOWED_HOSTS:
            return JSONResponse({'detail': 'Host is not allowed'}, status_code=403)
        origin = request.headers.get('origin')
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return JSONResponse({'detail': 'Origin is not allowed'}, status_code=403)
        if request.url.path not in {'/health', '/api/health'} and not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ''), launch_token):
            return JSONResponse({'detail': 'Authorization required'}, status_code=401)
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

    def lane_function(module: str, name: str):
        """The function another lane owns, or HTTP 501 while it does not exist yet."""
        try:
            present = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            present = False
        function = getattr(importlib.import_module(module), name, None) if present else None
        if function is None:
            raise HTTPException(501, f'Not implemented yet: needs {module}.{name}')
        return function

    async def call_lane(function, **kwargs):
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
                raise HTTPException(400, 'Invalid event cursor')
        return StreamingResponse(
            sse_events(store(), chat_id, max(0, after), request.is_disconnected, poll=poll, keepalive=keepalive),
            media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    def review_summary(review):
        return {**review, 'counts': store().severity_counts(review['id']), 'chat_status': (store().get_chat(review['chat_id']) or {}).get('status')}

    # ---- health, bootstrap ------------------------------------------------------------

    @app.get('/health')
    @app.get('/api/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/api/bootstrap')
    async def bootstrap():
        from .runners import PROFILES, available, resolve_profile
        try:
            profile = resolve_profile()
        except Exception as exc:  # an unusable default must not break the page
            profile = {'name': None, 'error': str(exc)[:300]}
        return {'projects': store().list_projects(), 'profile': profile,
                'runners': {name: available(name) for name in PROFILES}, 'max_upload_bytes': MAX_UPLOAD_BYTES}

    # ---- projects and versions --------------------------------------------------------

    @app.post('/api/projects')
    async def add_project(body: ProjectBody):
        return store().create_project(body.name)

    @app.get('/api/projects')
    async def list_projects():
        return [{**project, 'version_count': len(store().list_versions(project['id']))} for project in store().list_projects()]

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
        ingest = lane_function('trustpcb.intake.service', 'ingest')  # refuse before reading a large body
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
                result = await call_lane(ingest, store=store(), version=version, upload_path=upload_path, filename=filename)
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
        return {**version, 'artifacts': [{key: item[key] for key in ('id', 'type', 'mime', 'size', 'meta')} | {'url': f"/api/artifacts/{item['id']}"} for item in artifacts],
                'reviews': [review_summary(review) for review in store().list_reviews(version_id=id)]}

    @app.post('/api/versions/{id}/reviews')
    async def start_review(id: str, body: ReviewBody):
        found(store().get_version(id), 'Version')
        function = lane_function('trustpcb.review.service', 'start_review')
        return await call_lane(function, store=store(), manager=manager(), version_id=id, goal=body.goal, depth=body.depth)

    # ---- reviews ----------------------------------------------------------------------

    @app.get('/api/reviews/{id}')
    async def get_review(id: str):
        review = review_row(id)
        latest = store().latest_report_revision(id)
        return {**review_summary(review), 'threads': store().list_threads(id), 'report_revision': latest['number'] if latest else None}

    @app.get('/api/reviews/{id}/events')
    async def review_events(id: str, after: int = 0):
        return store().events(review_row(id)['chat_id'], max(0, after))

    @app.get('/api/reviews/{id}/stream')
    async def review_stream(id: str, request: Request, after: int = 0):
        return sse_response(review_row(id)['chat_id'], request, after)

    @app.post('/api/reviews/{id}/cancel')
    async def cancel_review(id: str):
        return await manager().cancel(review_row(id)['chat_id'])

    @app.post('/api/reviews/{id}/threads')
    async def start_thread(id: str, body: ThreadBody):
        review_row(id)
        function = lane_function('trustpcb.review.followup', 'start_thread')
        return await call_lane(function, store=store(), manager=manager(), review_id=id, body=body.model_dump())

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
        function = lane_function('trustpcb.review.report', 'render_revision')
        body = await call_lane(function, store=store(), review=review, revision=revision, fmt=fmt)
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
        function = lane_function('trustpcb.review.compare', 'compare_versions')
        return await call_lane(function, store=store(), from_id=from_id, to_id=to_id)

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
        function = lane_function('trustpcb.review.followup', 'send_message')
        return await call_lane(function, store=store(), manager=manager(), thread_id=id, text=body.text,
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
