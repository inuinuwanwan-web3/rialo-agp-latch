"""Durable, fixed-field evidence for a single read-only watch session.

Missing evidence never constitutes a successful test. No payloads, arguments,
exception text, credentials, or user-provided event names enter this database.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import time
from uuid import uuid4

from .diagnostics import failure_details

CURRENT = ContextVar('watch_audit', default=None)
READS = frozenset({'list_tracks', 'my_race', 'sigil_balance', 'track_state'})
WRITES = ('start_track', 'ask', 'guess', 'practice_guess', 'other_write')


def utc():
    return datetime.now(timezone.utc).isoformat()


def boot_clock():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def fingerprint():
    root = Path(__file__).parent
    return hashlib.sha256(b''.join((root / name).read_bytes() for name in (
        'track_watcher.py', 'observation_gateway.py', 'gateway.py', 'watch_audit.py'))).hexdigest()


def observation_state(directory):
    path = directory / 'observations.sqlite3'
    if not path.exists():
        return {'known': 0, 'new': 0, 'duplicates': 0, 'integrity': 'not_created'}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        return {'known': db.execute('SELECT count(*) FROM latest').fetchone()[0],
                'new': db.execute("SELECT count(*) FROM snapshots WHERE event='NEW_TRACK_DETECTED'").fetchone()[0],
                'duplicates': len(db.execute("SELECT track FROM snapshots WHERE event='NEW_TRACK_DETECTED' GROUP BY track HAVING count(*)>1").fetchall()),
                'integrity': 'ok' if db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' else 'failed'}


class WatchAudit:
    def __init__(self, directory, duration, interval):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / 'watch_audit.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, start_utc TEXT NOT NULL, end_utc TEXT,
              start_mono REAL NOT NULL, duration REAL, interval REAL NOT NULL,
              reason TEXT, elapsed REAL, start_state TEXT NOT NULL, end_state TEXT,
              source_hash TEXT NOT NULL, source_unchanged INTEGER,
              interrupted_detected_utc TEXT);
            CREATE TABLE IF NOT EXISTS evidence (
              id INTEGER PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL,
              key TEXT NOT NULL, utc TEXT NOT NULL, mono REAL NOT NULL, boot REAL NOT NULL,
              poll INTEGER NOT NULL, data TEXT NOT NULL,
              UNIQUE(session,kind,key));
        ''')
        self.id = uuid4().hex
        self.poll = 0
        self.serial = 0
        self.labels = set()
        self.seen_errors = []
        self.source_hash = fingerprint()
        self.start = time.monotonic()
        state = observation_state(directory)
        with self.db:
            self.db.execute('INSERT INTO sessions(id,start_utc,start_mono,duration,interval,start_state,source_hash) VALUES (?,?,?,?,?,?,?)',
                (self.id, utc(), self.start, duration, interval, json.dumps(state), self.source_hash))
        self._record('session_start', 'start', {'auto_join': 'BLOCKED'})

    def _record(self, kind, key, data):
        # Internal fixed producers only. Unknown public responses never reach here.
        with self.db:
            result = self.db.execute('INSERT OR IGNORE INTO evidence(session,kind,key,utc,mono,boot,poll,data) VALUES (?,?,?,?,?,?,?,?)',
                (self.id, kind, str(key), utc(), time.monotonic(), boot_clock(), self.poll, json.dumps(data, allow_nan=False)))
        return result.rowcount == 1

    def acquired_lock(self):
        # Only after exclusive session lock: older unfinished sessions cannot be
        # active. Keep their actual end time UNKNOWN, never invent crash time.
        with self.db:
            self.db.execute("UPDATE sessions SET reason='abnormal_exit',interrupted_detected_utc=? WHERE id!=? AND end_utc IS NULL AND reason IS NULL", (utc(), self.id))
        self._record('lock_acquired', 'lock', {})

    def anomaly(self, category):
        self.serial += 1
        self._record('anomaly', self.serial, {'category': category if category in {'db', 'lock', 'boundary', 'other'} else 'other'})

    def auto_join(self, value):
        self.serial += 1
        blocked = value == 'BLOCKED'
        self._record('auto_join', self.serial, {'state': 'BLOCKED' if blocked else 'NOT_BLOCKED'})
        if not blocked:
            raise RuntimeError('AUTO-JOIN audit blocked operation')

    def heartbeat(self):
        self.serial += 1
        self._record('heartbeat', self.serial, {'auto_join': 'BLOCKED'})

    def begin_poll(self):
        self.poll += 1
        self.labels = set()
        self.seen_errors = []
        self._record('poll_start', self.poll, {'auto_join': 'BLOCKED'})

    def transport_error(self, error):
        if any(error is old for old in self.seen_errors):
            return
        self.seen_errors.append(error)
        labels = set(failure_details(error)['categories'])
        if isinstance(error, TimeoutError):
            labels.add('timeout')
        self.labels.update(labels)
        self.serial += 1
        self._record('read_exception', self.serial, {
            'timeout':bool(labels & {'timeout','connect_timeout'}), 'rate_limit':'rate_limit' in labels})

    def end_poll(self, error=None, track_count=None):
        if error is not None:
            self.transport_error(error)
        self._record('poll_end', self.poll, {'success': error is None,
            'timeout': bool(self.labels & {'timeout', 'connect_timeout'}),
            'rate_limit': 'rate_limit' in self.labels, 'auto_join': 'BLOCKED', 'track_count': track_count})

    def backoff(self, seconds):
        self._record('backoff', self.poll, {'seconds': seconds})

    def backoff_done(self):
        self._record('backoff_end', self.poll, {})

    def notification(self, poll, index, event):
        if event not in {'NEW_TRACK_DETECTED', 'SNAPSHOT_CHANGED', 'BASELINE_TRACK', 'PENDING_USER_APPROVAL'}:
            raise RuntimeError('Unknown audit notification')
        return self._record('notification', f'{poll}/{index}', {'event': event})

    def violation(self, method, params):
        name = params.get('name') if isinstance(params, dict) else None
        self.serial += 1
        self._record('boundary_violation', self.serial, {'tool': name if name in WRITES else 'other_write'})
        raise RuntimeError('Read-only observation operation blocked')

    def before_send(self, method, params):
        self.auto_join('BLOCKED')
        name = params.get('name') if method == 'tools/call' else method
        if name not in READS | {'initialize', 'notifications/initialized'}:
            self.violation(method, params)
        key = uuid4().hex
        self._record('wire_before', key, {'tool': name, 'auto_join': 'BLOCKED'})
        return key

    def after_send(self, key):
        self._record('wire_after', key, {})

    def finish(self, reason):
        reason = reason if reason in {'duration_completed', 'manual_stop', 'error', 'poll_limit'} else 'other'
        state = observation_state(self.directory)
        self._record('session_end', 'end', {'auto_join': 'BLOCKED'})
        with self.db:
            self.db.execute('UPDATE sessions SET end_utc=?,reason=?,elapsed=?,end_state=?,source_unchanged=? WHERE id=?',
                (utc(), reason, time.monotonic()-self.start, json.dumps(state), fingerprint()==self.source_hash, self.id))

    def close(self):
        self.db.close()


def report(directory, session_id=None, *, required_seconds=86400):
    """Read-only proof report. Incomplete sessions and gaps cannot PASS."""
    path = directory / 'watch_audit.sqlite3'
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        session = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone() if session_id else db.execute('SELECT * FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone()
        if session is None:
            return {'verdict': 'UNVERIFIED', 'reason': 'no_session'}
        s = dict(session)
        evidence = [dict(r) for r in db.execute('SELECT * FROM evidence WHERE session=? ORDER BY id', (s['id'],))]
        for e in evidence:
            e['data'] = json.loads(e['data'])
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    starts = [e for e in evidence if e['kind']=='poll_start']
    ends = [e for e in evidence if e['kind']=='poll_end']
    failures = [e for e in ends if not e['data']['success']]
    exceptions = [e for e in evidence if e['kind']=='read_exception']
    inline_errors = sorted({e['poll'] for e in exceptions} & {e['poll'] for e in ends if e['data']['success']})
    backoffs = {e['poll'] for e in evidence if e['kind']=='backoff'}
    completed_backoffs = {e['poll'] for e in evidence if e['kind']=='backoff_end'}
    backoff_seconds = {e['poll']: e['data']['seconds'] for e in evidence if e['kind']=='backoff'}
    pending = []; recovered = []; consecutive = maximum = 0
    for e in ends:
        if e['data']['success']:
            recovered.extend(pending); pending=[]; consecutive=0
        else:
            pending.append(e['poll']); consecutive+=1; maximum=max(maximum,consecutive)
    wire = [e for e in evidence if e['kind']=='wire_before']
    reads = {tool:sum(e['data']['tool']==tool for e in wire) for tool in READS}
    writes = {tool:sum(e['data']['tool']==tool for e in wire) for tool in WRITES}
    violations = sum(e['kind']=='boundary_violation' for e in evidence)
    blocked_writes = {tool:sum(e['kind']=='boundary_violation' and e['data']['tool']==tool for e in evidence) for tool in WRITES}
    anomalies = [e['data']['category'] for e in evidence if e['kind']=='anomaly']
    auto_ok = all(e['data'].get('auto_join','BLOCKED')=='BLOCKED' and e['data'].get('state','BLOCKED')=='BLOCKED' for e in evidence)
    first, last = json.loads(s['start_state']), json.loads(s['end_state']) if s['end_state'] else None
    max_gap = max((b['mono']-a['mono'] for a,b in zip(evidence,evidence[1:])), default=float('inf'))
    suspend = any(abs((b['boot']-a['boot'])-(b['mono']-a['mono']))>2 for a,b in zip(evidence,evidence[1:]))
    polls_complete = bool(starts) and [e['poll'] for e in starts]==list(range(1,len(starts)+1)) and [e['poll'] for e in ends]==[e['poll'] for e in starts]
    cadence = polls_complete
    if cadence:
        cadence &= starts[0]['mono']-evidence[0]['mono'] <= 10
        for previous, following in zip(ends, starts[1:]):
            expected = s['interval'] if previous['data']['success'] else backoff_seconds.get(previous['poll'], 0)
            gap = following['mono']-previous['mono']
            cadence &= expected-.5 <= gap <= expected+10
        final_wait = s['interval'] if ends[-1]['data']['success'] else backoff_seconds.get(ends[-1]['poll'],0)
        cadence &= evidence[-1]['mono']-ends[-1]['mono'] <= final_wait+10
    # A success requires actual transport evidence, not inferred poll counts.
    sent = {e['key'] for e in evidence if e['kind']=='wire_after'}
    wire_proof = all(all(any(w['poll']==e['poll'] and w['data']['tool']==tool and w['key'] in sent for w in wire) for tool in ('list_tracks','my_race','sigil_balance')) for e in ends if e['data']['success'])
    wire_proof &= all(type(e['data'].get('track_count')) is int and
        sum(w['poll']==e['poll'] and w['data']['tool']=='track_state' and w['key'] in sent for w in wire)==e['data']['track_count']
        for e in ends if e['data']['success'])
    complete = (s['end_utc'] is not None and s['reason']=='duration_completed' and (s['elapsed'] or 0)>=required_seconds
        and (s['duration'] or 0)>=required_seconds and polls_complete and bool(ends) and any(e['data']['success'] for e in ends)
        and not pending and not inline_errors and all(p in backoffs & completed_backoffs for p in recovered) and not violations and not any(writes.values())
        and not anomalies and auto_ok and integrity and last is not None and last['integrity']=='ok'
        and first['integrity'] in {'ok','not_created'} and cadence
        and last['duplicates']==0 and s['source_unchanged']==1 and max_gap<=120 and not suspend and wire_proof
        and bool(evidence) and evidence[0]['kind']=='session_start' and evidence[-1]['kind']=='session_end'
        and any(e['kind']=='lock_acquired' for e in evidence))
    return {'session_id':s['id'],'start_utc':s['start_utc'],'end_utc':s['end_utc'],'reason':s['reason'],
        'elapsed_seconds':s['elapsed'],'poll_total':len(starts),'poll_success':len(ends)-len(failures),
        'poll_failed':len(failures),'max_consecutive_failed':maximum,'read_error':len(failures),
        'timeout':sum(e['data']['timeout'] for e in exceptions),'429':sum(e['data']['rate_limit'] for e in exceptions),
        'backoff':len(backoffs),'recovered_error_polls':recovered,'unrecovered_error_polls':pending,
        'known_start':first['known'],'known_end':last['known'] if last else None,
        'new_tracks':last['new']-first['new'] if last else None,'duplicate_events':last['duplicates'] if last else None,
        'integrity':last['integrity'] if last else 'UNVERIFIED','audit_integrity':integrity,
        'anomalies':anomalies,'boundary_violations':violations,'blocked_write_attempts':blocked_writes,
        'read_attempts':reads,'write_attempts_at_wire':writes,
        'auto_join_all_blocked':auto_ok,'max_evidence_gap_seconds':max_gap,'suspend_detected':suspend,
        'poll_records_complete':polls_complete,'poll_cadence_verified':bool(cadence),'wire_evidence_complete':wire_proof,
        'source_unchanged':s['source_unchanged']==1,'unbacked_transport_error_polls':inline_errors,
        'polls':[{'number':e['poll'],'start_utc':e['utc'],
                 'end_utc':next((p['utc'] for p in ends if p['poll']==e['poll']),None),
                 'result':next((p['data'] for p in ends if p['poll']==e['poll']),None)} for e in starts],
        'verdict':'PASS' if complete else ('UNVERIFIED' if s['end_utc'] is None else 'FAIL')}
