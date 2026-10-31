"""Durable local metadata (SQLite) and the app-owned data folder.

Every table has simple typed methods here. Rows come back as
plain dicts with JSON columns already decoded. Ids are opaque hex strings unless noted.
Findings and evidence also carry a per-review display `code` ("F-007", "E-112").
"""
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _id():
    return uuid.uuid4().hex


def _now():
    return datetime.now(timezone.utc).isoformat()


FINDING_SEVERITIES = ('critical', 'high', 'medium', 'low', 'info')
ASSUMPTION_SOURCES = ('default', 'detected', 'user_provided')
CHAT_KINDS = ('review', 'thread')
TERMINAL_TURN_STATES = ('completed', 'failed', 'canceled', 'interrupted')

# Columns holding JSON, per table. Decoded on read, encoded on write.
JSON_COLUMNS = {
    'versions': ('stackup', 'detection'),
    'artifacts': ('meta',),
    'reviews': ('profile', 'goal', 'budget', 'summary'),
    'findings': ('evidence_ids', 'marks', 'assumptions', 'not_tested'),
    'evidence': ('values', 'artifact_ids', 'assumption_ids'),
    'candidates': ('data',),
    'investigations': ('outcome',),
    'suspects': ('data', 'conclusion'),
    'sim_jobs': ('scenario', 'result'),
    'threads': ('digest',),
    'finding_history': ('before', 'after'),
    'assumptions': ('value', 'dependent_evidence_ids'),
    'report_revisions': ('snapshot',),
    'events': ('payload',),
    'chats': (),
    'turns': ('meta',),
}

# Fields a caller may change through update_<thing>(); anything else is rejected.
UPDATABLE = {
    'versions': {'label', 'upload_hash', 'stackup', 'detection', 'status', 'error'},
    'chats': {'title', 'session_id', 'status'},
    'reviews': {'profile', 'goal', 'depth', 'phase', 'budget', 'end_state', 'summary', 'status'},
    'findings': {'title', 'severity', 'class', 'status', 'what', 'why_it_matters', 'recommendation',
                 'evidence_ids', 'marks', 'assumptions', 'not_tested', 'superseded_by',
                 'dismissed_reason', 'needs_recheck', 'source', 'thread_id'},
    'investigations': {'question', 'status', 'outcome'},
    'suspects': {'rank', 'title', 'status', 'conclusion', 'data'},
    'sim_jobs': {'status', 'progress', 'trust', 'result', 'job_dir', 'investigation_id', 'error'},
    'threads': {'session_id', 'level', 'tokens_used', 'status', 'digest'},
    'assumptions': {'value', 'source', 'dependent_evidence_ids', 'note'},
}


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ('uploads', 'versions', 'scenarios'):
            (self.data_dir / name).mkdir(exist_ok=True, mode=0o700)
        self.database = self.data_dir / 'trustpcb.sqlite3'
        with self._db() as db:
            db.executescript(SCHEMA)

    # ---- plumbing ---------------------------------------------------------------------

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _decode(table, row):
        if row is None:
            return None
        row = dict(row)
        for column in JSON_COLUMNS.get(table, ()):
            if column in row and isinstance(row[column], str):
                row[column] = json.loads(row[column])
        return row

    @staticmethod
    def _encode(table, fields):
        out = {}
        for key, value in fields.items():
            out[key] = json.dumps(value) if key in JSON_COLUMNS.get(table, ()) else value
        return out

    def _one(self, table, sql, args=()):
        with self._db() as db:
            return self._decode(table, db.execute(sql, args).fetchone())

    def _all(self, table, sql, args=()):
        with self._db() as db:
            return [self._decode(table, row) for row in db.execute(sql, args)]

    def _insert(self, db, table, values):
        values = self._encode(table, values)
        columns = ','.join(f'"{key}"' for key in values)
        db.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" * len(values))})', tuple(values.values()))

    def _update(self, table, id, fields, extra_set=None, key='id'):
        allowed = UPDATABLE[table]
        if not fields or not fields.keys() <= allowed:
            raise ValueError(f'Unsupported {table} fields: {sorted(set(fields) - allowed)}')
        values = self._encode(table, fields)
        if table in HAS_UPDATED_AT:
            values['updated_at'] = _now()
        with self._db() as db:
            assignments = ','.join(f'"{name}"=?' for name in values)
            db.execute(f'UPDATE {table} SET {assignments} WHERE {key}=?', (*values.values(), id))

    # ---- paths ------------------------------------------------------------------------

    def upload_dir(self, upload_id: str) -> Path:
        path = self.data_dir / 'uploads' / upload_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def version_dir(self, version_id: str) -> Path:
        path = self.data_dir / 'versions' / version_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def scenario_dir(self, scenario_hash: str) -> Path:
        path = self.data_dir / 'scenarios' / scenario_hash
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def relative(self, path) -> str:
        """Path as stored in artifacts: relative to the data folder, never escaping it."""
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = self.data_dir / resolved
        resolved = resolved.resolve()
        if not resolved.is_relative_to(self.data_dir):
            raise ValueError('Path is outside the data folder')
        return resolved.relative_to(self.data_dir).as_posix()

    # ---- projects ---------------------------------------------------------------------

    def create_project(self, name: str):
        id = _id()
        with self._db() as db:
            self._insert(db, 'projects', {'id': id, 'name': name.strip()[:200] or 'Untitled', 'created_at': _now()})
        return self.get_project(id)

    def list_projects(self):
        return self._all('projects', 'SELECT * FROM projects ORDER BY created_at, rowid')

    def get_project(self, id):
        return self._one('projects', 'SELECT * FROM projects WHERE id=?', (id,))

    # ---- versions ---------------------------------------------------------------------

    def create_version(self, project_id, label=None, upload_hash=None, stackup=None, detection=None, status='uploaded'):
        if not self.get_project(project_id):
            raise ValueError('Project not found')
        id = _id()
        with self._db() as db:
            number = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM versions WHERE project_id=?', (project_id,)).fetchone()[0]
            self._insert(db, 'versions', {
                'id': id, 'project_id': project_id, 'number': number, 'label': label, 'upload_hash': upload_hash,
                'stackup': stackup, 'detection': detection, 'status': status, 'error': None, 'created_at': _now(),
            })
        return self.get_version(id)

    def get_version(self, id):
        return self._one('versions', 'SELECT * FROM versions WHERE id=?', (id,))

    def list_versions(self, project_id):
        return self._all('versions', 'SELECT * FROM versions WHERE project_id=? ORDER BY number', (project_id,))

    def find_version_by_hash(self, project_id, upload_hash):
        return self._one('versions', 'SELECT * FROM versions WHERE project_id=? AND upload_hash=? ORDER BY number DESC', (project_id, upload_hash))

    def update_version(self, id, **fields):
        self._update('versions', id, fields)
        return self.get_version(id)

    def delete_version(self, id):
        """Only for a failed upload that never produced anything else."""
        with self._db() as db:
            db.execute('DELETE FROM artifacts WHERE owner_kind=? AND owner_id=?', ('version', id))
            db.execute('DELETE FROM versions WHERE id=?', (id,))

    # ---- artifacts --------------------------------------------------------------------

    def add_artifact(self, owner_kind, owner_id, type, path, mime=None, meta=None):
        if owner_kind not in ('version', 'scenario', 'review'):
            raise ValueError('owner_kind must be version, scenario or review')
        id = _id()
        stored = self.relative(path)
        target = self.data_dir / stored
        size = target.stat().st_size if target.is_file() else None
        with self._db() as db:
            self._insert(db, 'artifacts', {
                'id': id, 'owner_kind': owner_kind, 'owner_id': owner_id, 'type': type, 'path': stored,
                'mime': mime, 'size': size, 'meta': meta or {}, 'created_at': _now(),
            })
        return self.get_artifact(id)

    def get_artifact(self, id):
        return self._one('artifacts', 'SELECT * FROM artifacts WHERE id=?', (id,))

    def list_artifacts(self, owner_kind=None, owner_id=None, type=None):
        clauses, args = [], []
        for column, value in (('owner_kind', owner_kind), ('owner_id', owner_id), ('type', type)):
            if value is not None:
                clauses.append(f'{column}=?')
                args.append(value)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        return self._all('artifacts', f'SELECT * FROM artifacts{where} ORDER BY created_at, rowid', tuple(args))

    def artifact_path(self, artifact) -> Path:
        """Absolute file path of an artifact, refusing anything outside the data folder."""
        path = (self.data_dir / artifact['path']).resolve()
        if not path.is_relative_to(self.data_dir):
            raise ValueError('Artifact path is outside the data folder')
        return path

    # ---- chats, turns, events ---------------------------------------------------------

    def create_chat(self, project_id, kind='review', title=None):
        if kind not in CHAT_KINDS:
            raise ValueError('kind must be review or thread')
        if not self.get_project(project_id):
            raise ValueError('Project not found')
        id = _id()
        with self._db() as db:
            self._insert(db, 'chats', {'id': id, 'project_id': project_id, 'kind': kind, 'title': title or ('Review' if kind == 'review' else 'Follow-up'),
                                       'session_id': None, 'status': 'idle', 'created_at': _now(), 'runner': None, 'model': None, 'effort': None})
        return self.get_chat(id)

    def get_chat(self, id):
        return self._one('chats', 'SELECT * FROM chats WHERE id=?', (id,))

    def bind_chat_profile(self, id, profile):
        """Bind once; later calls never change the profile of a started chat."""
        with self._db() as db:
            db.execute('UPDATE chats SET runner=?,model=?,effort=? WHERE id=? AND runner IS NULL',
                       (profile['runner'], profile.get('model'), profile.get('effort'), id))
        return self.get_chat(id)

    def update_chat(self, id, **fields):
        self._update('chats', id, fields)

    def create_turn(self, chat_id, request_id, text='', meta=None):
        id = _id()
        try:
            with self._db() as db:
                self._insert(db, 'turns', {'id': id, 'chat_id': chat_id, 'request_id': request_id, 'text': text, 'status': 'running', 'meta': meta or {}, 'created_at': _now()})
        except sqlite3.IntegrityError as exc:
            raise ValueError('Duplicate request or unknown chat') from exc
        return self.find_turn(chat_id, request_id)

    def find_turn(self, chat_id, request_id):
        return self._one('turns', 'SELECT * FROM turns WHERE chat_id=? AND request_id=?', (chat_id, request_id))

    def get_turn(self, id):
        return self._one('turns', 'SELECT * FROM turns WHERE id=?', (id,))

    def finish_turn(self, id, status):
        with self._db() as db:
            db.execute('UPDATE turns SET status=? WHERE id=?', (status, id))

    def recover_interrupted(self):
        """After a restart, no turn can still be running: mark them and say so in the stream."""
        turns = self._all('turns', "SELECT * FROM turns WHERE status IN ('running','waiting','waiting_permission','waiting_question','waiting_test')")
        for turn in turns:
            self.finish_turn(turn['id'], 'interrupted')
            self.update_chat(turn['chat_id'], status='interrupted')
            with self._db() as db:
                db.execute("UPDATE reviews SET status='interrupted',updated_at=? WHERE chat_id=? AND status IN ('queued','running')", (_now(), turn['chat_id']))
                db.execute("UPDATE threads SET status='interrupted',updated_at=? WHERE chat_id=? AND status IN ('queued','running')", (_now(), turn['chat_id']))
            self.append_event(turn['chat_id'], turn['id'], 'failed', {'message': 'Backend restarted; this run was interrupted.', 'interrupted': True})
        return turns

    def append_event(self, chat_id, turn_id, type, payload):
        chat = self.get_chat(chat_id)
        if not chat:
            raise ValueError('Chat not found')
        id = _id()
        with self._db() as db:
            cursor = db.execute('INSERT INTO events(id,project_id,chat_id,turn_id,type,payload,created_at) VALUES(?,?,?,?,?,?,?)',
                                (id, chat['project_id'], chat_id, turn_id, type, json.dumps(payload), _now()))
            sequence = cursor.lastrowid
            row = db.execute('SELECT * FROM events WHERE sequence=?', (sequence,)).fetchone()
        return self._decode('events', row)

    def events(self, chat_id, after=0, limit=None):
        sql, args = 'SELECT * FROM events WHERE chat_id=? AND sequence>? ORDER BY sequence', [chat_id, after]
        if limit:
            sql += ' LIMIT ?'
            args.append(limit)
        return self._all('events', sql, tuple(args))

    # ---- reviews ----------------------------------------------------------------------

    def create_review(self, version_id, goal=None, depth='standard', profile=None):
        """Creates the review and its event chat together."""
        version = self.get_version(version_id)
        if not version:
            raise ValueError('Version not found')
        chat = self.create_chat(version['project_id'], 'review', f"Review of v{version['number']}")
        id = _id()
        with self._db() as db:
            self._insert(db, 'reviews', {
                'id': id, 'project_id': version['project_id'], 'version_id': version_id, 'chat_id': chat['id'],
                'profile': profile, 'depth': depth, 'goal': goal or {}, 'phase': None, 'budget': {}, 'end_state': None,
                'summary': None, 'status': 'queued', 'created_at': _now(), 'updated_at': _now(),
            })
        if profile:
            self.bind_chat_profile(chat['id'], profile)
        return self.get_review(id)

    def get_review(self, id):
        return self._one('reviews', 'SELECT * FROM reviews WHERE id=?', (id,))

    def get_review_by_chat(self, chat_id):
        return self._one('reviews', 'SELECT * FROM reviews WHERE chat_id=?', (chat_id,))

    def list_reviews(self, project_id=None, version_id=None):
        clauses, args = [], []
        if project_id:
            clauses.append('project_id=?')
            args.append(project_id)
        if version_id:
            clauses.append('version_id=?')
            args.append(version_id)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        return self._all('reviews', f'SELECT * FROM reviews{where} ORDER BY created_at, rowid', tuple(args))

    def update_review(self, id, **fields):
        self._update('reviews', id, fields)
        return self.get_review(id)

    # ---- findings ---------------------------------------------------------------------

    def _record_history(self, db, finding_id, source, reason, before, after):
        self._insert(db, 'finding_history', {'finding_id': finding_id, 'source': source, 'reason': reason, 'before': before, 'after': after, 'created_at': _now()})

    def create_finding(self, review_id, fingerprint, title, severity, class_='opinion', source='review', reason='created', thread_id=None, **fields):
        """Insert a finding with the next F-nnn code. The honesty checks live in `trustpcb.review.findings`."""
        review = self.get_review(review_id)
        if not review:
            raise ValueError('Review not found')
        if severity not in FINDING_SEVERITIES:
            raise ValueError('Unknown severity')
        id = _id()
        row = {
            'id': id, 'review_id': review_id, 'version_id': review['version_id'], 'fingerprint': fingerprint, 'title': title,
            'severity': severity, 'class': class_, 'status': 'open', 'what': None, 'why_it_matters': None, 'recommendation': None,
            'evidence_ids': [], 'marks': [], 'assumptions': [], 'not_tested': [], 'superseded_by': None, 'dismissed_reason': None,
            'needs_recheck': 0, 'source': source, 'thread_id': thread_id, 'created_at': _now(), 'updated_at': _now(),
        }
        unknown = set(fields) - UPDATABLE['findings']
        if unknown:
            raise ValueError(f'Unsupported finding fields: {sorted(unknown)}')
        row.update(fields)
        with self._db() as db:
            number = db.execute('SELECT COUNT(*)+1 FROM findings WHERE review_id=?', (review_id,)).fetchone()[0]
            row['code'] = f'F-{number:03d}'
            self._insert(db, 'findings', row)
            self._record_history(db, id, source, reason, None, self._decode('findings', db.execute('SELECT * FROM findings WHERE id=?', (id,)).fetchone()))
        return self.get_finding(id)

    def get_finding(self, id):
        return self._one('findings', 'SELECT * FROM findings WHERE id=?', (id,))

    def get_finding_by_code(self, review_id, code):
        return self._one('findings', 'SELECT * FROM findings WHERE review_id=? AND code=?', (review_id, code))

    def find_finding(self, review_id, fingerprint):
        return self._one('findings', 'SELECT * FROM findings WHERE review_id=? AND fingerprint=? ORDER BY created_at DESC, rowid DESC', (review_id, fingerprint))

    def list_findings(self, review_id, status=None):
        sql, args = 'SELECT * FROM findings WHERE review_id=?', [review_id]
        if status:
            sql += ' AND status=?'
            args.append(status)
        return self._all('findings', sql + ' ORDER BY rowid', tuple(args))

    def update_finding(self, id, source, reason=None, **fields):
        """Change a finding and append the before/after pair to finding_history."""
        before = self.get_finding(id)
        if not before:
            raise ValueError('Finding not found')
        if 'class_' in fields:
            fields['class'] = fields.pop('class_')
        if 'severity' in fields and fields['severity'] not in FINDING_SEVERITIES:
            raise ValueError('Unknown severity')
        self._update('findings', id, fields)
        after = self.get_finding(id)
        with self._db() as db:
            self._record_history(db, id, source, reason, before, after)
        return after

    def finding_history(self, finding_id):
        return self._all('finding_history', 'SELECT * FROM finding_history WHERE finding_id=? ORDER BY id', (finding_id,))

    def severity_counts(self, review_id, open_only=True):
        counts = {name: 0 for name in FINDING_SEVERITIES}
        sql = 'SELECT severity, COUNT(*) AS n FROM findings WHERE review_id=?' + (" AND status IN ('open','needs_recheck')" if open_only else '') + ' GROUP BY severity'
        for row in self._all('findings', sql, (review_id,)):
            counts[row['severity']] = row['n']
        return counts

    # ---- evidence ---------------------------------------------------------------------

    def add_evidence(self, review_id, type, source_tool, values, artifact_ids=None, assumption_ids=None):
        if not self.get_review(review_id):
            raise ValueError('Review not found')
        id = _id()
        with self._db() as db:
            number = db.execute('SELECT COUNT(*)+1 FROM evidence WHERE review_id=?', (review_id,)).fetchone()[0]
            self._insert(db, 'evidence', {
                'id': id, 'review_id': review_id, 'code': f'E-{number:03d}', 'type': type, 'source_tool': source_tool,
                'values': values, 'artifact_ids': artifact_ids or [], 'assumption_ids': assumption_ids or [], 'created_at': _now(),
            })
        return self.get_evidence(id)

    def get_evidence(self, id):
        return self._one('evidence', 'SELECT * FROM evidence WHERE id=?', (id,))

    def list_evidence(self, review_id):
        return self._all('evidence', 'SELECT * FROM evidence WHERE review_id=? ORDER BY rowid', (review_id,))

    # ---- candidates -------------------------------------------------------------------

    def add_candidate(self, review_id, candidate: dict, review_state='new'):
        """`candidate` is the analyzers/base.py Candidate dict; its own id is "candidate:<rule>:<n>"."""
        id = _id()
        with self._db() as db:
            self._insert(db, 'candidates', {'id': id, 'review_id': review_id, 'candidate_id': candidate['id'], 'rule': candidate.get('rule'),
                                            'data': candidate, 'review_state': review_state, 'created_at': _now()})
        return self.get_candidate(review_id, candidate['id'])

    def get_candidate(self, review_id, candidate_id):
        return self._one('candidates', 'SELECT * FROM candidates WHERE review_id=? AND candidate_id=?', (review_id, candidate_id))

    def list_candidates(self, review_id, review_state=None):
        sql, args = 'SELECT * FROM candidates WHERE review_id=?', [review_id]
        if review_state:
            sql += ' AND review_state=?'
            args.append(review_state)
        return self._all('candidates', sql + ' ORDER BY rowid', tuple(args))

    def set_candidate_state(self, review_id, candidate_id, review_state):
        with self._db() as db:
            db.execute('UPDATE candidates SET review_state=? WHERE review_id=? AND candidate_id=?', (review_state, review_id, candidate_id))
        return self.get_candidate(review_id, candidate_id)

    # ---- investigations and suspects --------------------------------------------------

    def create_investigation(self, review_id, question, status='open'):
        id = _id()
        with self._db() as db:
            self._insert(db, 'investigations', {'id': id, 'review_id': review_id, 'question': question, 'status': status, 'outcome': None, 'created_at': _now(), 'updated_at': _now()})
        return self.get_investigation(id)

    def get_investigation(self, id):
        return self._one('investigations', 'SELECT * FROM investigations WHERE id=?', (id,))

    def list_investigations(self, review_id):
        return self._all('investigations', 'SELECT * FROM investigations WHERE review_id=? ORDER BY rowid', (review_id,))

    def update_investigation(self, id, **fields):
        self._update('investigations', id, fields)
        return self.get_investigation(id)

    def add_suspect(self, investigation_id, title, rank=None, status='open', data=None):
        id = _id()
        with self._db() as db:
            if rank is None:
                rank = db.execute('SELECT COUNT(*)+1 FROM suspects WHERE investigation_id=?', (investigation_id,)).fetchone()[0]
            self._insert(db, 'suspects', {'id': id, 'investigation_id': investigation_id, 'rank': rank, 'title': title, 'status': status, 'conclusion': None, 'data': data or {}, 'created_at': _now(), 'updated_at': _now()})
        return self.get_suspect(id)

    def get_suspect(self, id):
        return self._one('suspects', 'SELECT * FROM suspects WHERE id=?', (id,))

    def list_suspects(self, investigation_id):
        return self._all('suspects', 'SELECT * FROM suspects WHERE investigation_id=? ORDER BY rank, rowid', (investigation_id,))

    def update_suspect(self, id, **fields):
        self._update('suspects', id, fields)
        return self.get_suspect(id)

    # ---- sim jobs ---------------------------------------------------------------------

    def create_sim_job(self, scenario_hash, review_id=None, investigation_id=None, scenario=None, job_dir=None, status='queued'):
        id = _id()
        with self._db() as db:
            self._insert(db, 'sim_jobs', {'id': id, 'scenario_hash': scenario_hash, 'review_id': review_id, 'investigation_id': investigation_id,
                                          'scenario': scenario, 'job_dir': str(job_dir) if job_dir else None, 'status': status, 'progress': 0.0,
                                          'trust': None, 'result': None, 'error': None, 'created_at': _now(), 'updated_at': _now()})
        return self.get_sim_job(id)

    def get_sim_job(self, id):
        return self._one('sim_jobs', 'SELECT * FROM sim_jobs WHERE id=?', (id,))

    def find_sim_job(self, scenario_hash, review_id=None):
        """Most recent job for a scenario hash; with review_id, only inside that review."""
        if review_id:
            return self._one('sim_jobs', 'SELECT * FROM sim_jobs WHERE scenario_hash=? AND review_id=? ORDER BY rowid DESC', (scenario_hash, review_id))
        return self._one('sim_jobs', 'SELECT * FROM sim_jobs WHERE scenario_hash=? ORDER BY rowid DESC', (scenario_hash,))

    def list_sim_jobs(self, review_id=None, status=None):
        clauses, args = [], []
        if review_id:
            clauses.append('review_id=?')
            args.append(review_id)
        if status:
            clauses.append('status=?')
            args.append(status)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        return self._all('sim_jobs', f'SELECT * FROM sim_jobs{where} ORDER BY rowid', tuple(args))

    def update_sim_job(self, id, **fields):
        self._update('sim_jobs', id, fields)
        return self.get_sim_job(id)

    # ---- threads (follow-up conversations) --------------------------------------------

    def create_thread(self, review_id, level='answer'):
        """Creates the thread and its event chat together. Each thread belongs to one review."""
        review = self.get_review(review_id)
        if not review:
            raise ValueError('Review not found')
        chat = self.create_chat(review['project_id'], 'thread', 'Follow-up')
        id = _id()
        with self._db() as db:
            self._insert(db, 'threads', {'id': id, 'review_id': review_id, 'chat_id': chat['id'], 'session_id': None, 'level': level,
                                         'tokens_used': 0, 'status': 'idle', 'digest': None, 'created_at': _now(), 'updated_at': _now()})
        return self.get_thread(id)

    def get_thread(self, id):
        return self._one('threads', 'SELECT * FROM threads WHERE id=?', (id,))

    def get_thread_by_chat(self, chat_id):
        return self._one('threads', 'SELECT * FROM threads WHERE chat_id=?', (chat_id,))

    def list_threads(self, review_id):
        return self._all('threads', 'SELECT * FROM threads WHERE review_id=? ORDER BY rowid', (review_id,))

    def update_thread(self, id, **fields):
        self._update('threads', id, fields)
        return self.get_thread(id)

    def add_thread_tokens(self, id, tokens: int):
        with self._db() as db:
            db.execute('UPDATE threads SET tokens_used=tokens_used+?,updated_at=? WHERE id=?', (int(tokens), _now(), id))
        return self.get_thread(id)

    # ---- assumptions ------------------------------------------------------------------

    def add_assumption(self, review_id, name, value, source='default', dependent_evidence_ids=None, note=None, thread_id=None):
        if source not in ASSUMPTION_SOURCES:
            raise ValueError('source must be default, detected or user_provided')
        id = _id()
        with self._db() as db:
            self._insert(db, 'assumptions', {'id': id, 'review_id': review_id, 'name': name, 'value': value, 'source': source,
                                             'dependent_evidence_ids': dependent_evidence_ids or [], 'note': note, 'thread_id': thread_id,
                                             'created_at': _now(), 'updated_at': _now()})
        return self.get_assumption(id)

    def get_assumption(self, id):
        return self._one('assumptions', 'SELECT * FROM assumptions WHERE id=?', (id,))

    def list_assumptions(self, review_id):
        return self._all('assumptions', 'SELECT * FROM assumptions WHERE review_id=? ORDER BY rowid', (review_id,))

    def update_assumption(self, id, **fields):
        if fields.get('source') is not None and fields['source'] not in ASSUMPTION_SOURCES:
            raise ValueError('source must be default, detected or user_provided')
        self._update('assumptions', id, fields)
        return self.get_assumption(id)

    # ---- report revisions -------------------------------------------------------------

    def add_report_revision(self, review_id, cause, snapshot):
        """Revision numbers start at 1 and only grow; snapshots are never edited afterwards."""
        id = _id()
        with self._db() as db:
            number = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM report_revisions WHERE review_id=?', (review_id,)).fetchone()[0]
            self._insert(db, 'report_revisions', {'id': id, 'review_id': review_id, 'number': number, 'cause': cause, 'snapshot': snapshot, 'created_at': _now()})
        return self.get_report_revision(review_id, number)

    def get_report_revision(self, review_id, number):
        return self._one('report_revisions', 'SELECT * FROM report_revisions WHERE review_id=? AND number=?', (review_id, number))

    def latest_report_revision(self, review_id):
        return self._one('report_revisions', 'SELECT * FROM report_revisions WHERE review_id=? ORDER BY number DESC', (review_id,))

    def list_report_revisions(self, review_id):
        """Listing omits snapshots (they can be large); fetch one revision for its snapshot."""
        rows = self._all('report_revisions', 'SELECT * FROM report_revisions WHERE review_id=? ORDER BY number', (review_id,))
        for row in rows:
            row.pop('snapshot', None)
        return rows


SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), number INTEGER NOT NULL, label TEXT,
  upload_hash TEXT, stackup TEXT, detection TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
  UNIQUE(project_id, number));
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, type TEXT NOT NULL, path TEXT NOT NULL,
  mime TEXT, size INTEGER, meta TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS artifacts_owner ON artifacts(owner_kind, owner_id, type);
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL DEFAULT 'review', title TEXT,
  session_id TEXT, status TEXT, created_at TEXT NOT NULL, runner TEXT, model TEXT, effort TEXT);
CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id), request_id TEXT NOT NULL, text TEXT, status TEXT NOT NULL,
  meta TEXT, created_at TEXT, UNIQUE(chat_id, request_id));
CREATE TABLE IF NOT EXISTS events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, project_id TEXT, chat_id TEXT NOT NULL, turn_id TEXT,
  type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_chat ON events(chat_id, sequence);
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), version_id TEXT NOT NULL REFERENCES versions(id),
  chat_id TEXT NOT NULL REFERENCES chats(id), profile TEXT, depth TEXT, goal TEXT, phase TEXT, budget TEXT, end_state TEXT,
  summary TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), version_id TEXT, code TEXT NOT NULL, fingerprint TEXT NOT NULL,
  title TEXT NOT NULL, severity TEXT NOT NULL, class TEXT NOT NULL, status TEXT NOT NULL, what TEXT, why_it_matters TEXT,
  recommendation TEXT, evidence_ids TEXT, marks TEXT, assumptions TEXT, not_tested TEXT, superseded_by TEXT, dismissed_reason TEXT,
  needs_recheck INTEGER NOT NULL DEFAULT 0, source TEXT, thread_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(review_id, code));
CREATE INDEX IF NOT EXISTS findings_fingerprint ON findings(review_id, fingerprint);
CREATE TABLE IF NOT EXISTS finding_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id TEXT NOT NULL REFERENCES findings(id), source TEXT, reason TEXT,
  before TEXT, after TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), code TEXT NOT NULL, type TEXT, source_tool TEXT,
  "values" TEXT, artifact_ids TEXT, assumption_ids TEXT, created_at TEXT NOT NULL, UNIQUE(review_id, code));
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), candidate_id TEXT NOT NULL, rule TEXT, data TEXT,
  review_state TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(review_id, candidate_id));
CREATE TABLE IF NOT EXISTS investigations (
  id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), question TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS suspects (
  id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL REFERENCES investigations(id), rank INTEGER, title TEXT, status TEXT NOT NULL,
  conclusion TEXT, data TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sim_jobs (
  id TEXT PRIMARY KEY, scenario_hash TEXT NOT NULL, review_id TEXT, investigation_id TEXT, scenario TEXT, job_dir TEXT,
  status TEXT NOT NULL, progress REAL, trust TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sim_jobs_hash ON sim_jobs(scenario_hash);
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), chat_id TEXT NOT NULL REFERENCES chats(id), session_id TEXT,
  level TEXT, tokens_used INTEGER NOT NULL DEFAULT 0, status TEXT, digest TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assumptions (
  id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), name TEXT NOT NULL, value TEXT,
  source TEXT NOT NULL CHECK (source IN ('default','detected','user_provided')), dependent_evidence_ids TEXT, note TEXT, thread_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_revisions (
  id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), number INTEGER NOT NULL, cause TEXT, snapshot TEXT,
  created_at TEXT NOT NULL, UNIQUE(review_id, number));
'''

HAS_UPDATED_AT = {m.group(1) for m in re.finditer(r'CREATE TABLE IF NOT EXISTS (\w+) \(.*?\);', SCHEMA, re.S) if 'updated_at' in m.group(0)}
