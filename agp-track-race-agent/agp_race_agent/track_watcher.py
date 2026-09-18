"""Read-only evidence collection; never evaluates eligibility or joins a race.

Run explicitly with python -m agp_race_agent.track_watcher --execute.
SQLite is the canonical snapshot archive (not a second JSONL copy).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import signal
import sqlite3
import threading
import time
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .config import load
from .gateway import McpResponseError
from .observation_gateway import ObservationReader, credential_values
from .watch_audit import CURRENT, WatchAudit, report


@dataclass(frozen=True)
class WatchOptions:
    directory: Path = Path('data/track_observations')
    interval: float = 30
    backoff_max: float = 300

    def __post_init__(self):
        if (not all(math.isfinite(x) and x > 0 for x in (self.interval, self.backoff_max))
                or self.backoff_max < self.interval):
            raise ValueError('Invalid observation timing')

    @classmethod
    def load(cls, path):
        with path.open('rb') as stream:
            data = tomllib.load(stream).get('observations', {})
        return cls(path.parent / data.get('directory', 'data/track_observations'),
                   float(data.get('interval_seconds', 30)),
                   float(data.get('backoff_max_seconds', 300)))


class Redactor:
    sensitive = re.compile(r'token|secret|password|authorization|authentication|credential|private.?key|api.?key|cookie|^headers?$|^auth$', re.I)

    def __init__(self, secrets=()):
        self.secrets = sorted(set(v for v in secrets if v), key=len, reverse=True)

    def text(self, text):
        for secret in self.secrets:
            text = text.replace(secret, '<redacted>')
        text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '<redacted>', text, flags=re.S)
        text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s"\x27,;]+', '<redacted>', text)
        text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '<redacted>', text)
        text = re.sub(r'(?i)((?:token|secret|password|authorization|api[_-]?key)\s*[:=]\s*)[^\s,;]+', r'\1<redacted>', text)
        return text

    def __call__(self, value):
        if isinstance(value, dict):
            return {self.text(key): '<redacted>' if self.sensitive.search(key) else self(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, str):
            # JSON serialized inside text is still subject to recursive masking.
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                return json.dumps(self(parsed), ensure_ascii=False)
            return self.text(value)
        return value


def changed_fields(old, new, prefix=''):
    """JSON Pointer paths; includes added/deleted fields and unknown schemas."""
    if isinstance(old, dict) and isinstance(new, dict):
        changed = []
        for key in sorted(old.keys() | new.keys()):
            path = prefix + '/' + key.replace('~', '~0').replace('/', '~1')
            if key not in old or key not in new:
                changed.append(path)
            else:
                changed.extend(changed_fields(old[key], new[key], path))
        return changed
    return [] if type(old) is type(new) and old == new else [prefix or '/']


def private_file(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd


class SnapshotStore:
    """Atomic known set, last snapshots, event claims, and append-only history.

    Commit precedes notification. A crash can lose a hook delivery but cannot
    duplicate it; the event remains queryable in snapshots. Poll lock prevents
    concurrent processes from persisting out-of-order observations.
    """
    def __init__(self, directory):
        self.pending_approvals = 0
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = private_file(directory / 'watcher.lock')
        self.path = directory / 'observations.sqlite3'
        os.close(private_file(self.path))
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS latest (track TEXT PRIMARY KEY, comparison TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY, track TEXT NOT NULL, observed_at_utc TEXT NOT NULL,
                event TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS snapshots_track ON snapshots(track,id);
            CREATE TABLE IF NOT EXISTS start_candidates (
                track TEXT PRIMARY KEY, snapshot INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING_USER_APPROVAL');
        ''')

    def close(self):
        self.db.close()
        os.close(self.lock)

    def save(self, observations):
        events = []
        self.pending_approvals = 0
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            initialized = self.db.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone()
            for track, comparison, payload in observations:
                row = self.db.execute('SELECT comparison FROM latest WHERE track=?', (track,)).fetchone()
                diff = changed_fields(json.loads(row[0]), comparison) if row else ['/']
                if not diff:
                    continue
                event = 'SNAPSHOT_CHANGED' if row else ('NEW_TRACK_DETECTED' if initialized else 'BASELINE_TRACK')
                payload = {**payload, 'changed_fields': diff, 'event': event, 'auto_join': 'BLOCKED'}
                from .manual_start import registration_candidate
                candidate = registration_candidate(comparison['list_tracks'])
                if candidate:
                    payload['registration_review'] = 'PENDING_USER_APPROVAL'
                    payload['eligibility'] = 'UNCONFIRMED'
                saved = self.db.execute('INSERT INTO snapshots(track,observed_at_utc,event,payload) VALUES (?,?,?,?)',
                    (track, payload['observed_at_utc'], event, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
                if candidate:
                    if not self.db.execute('SELECT 1 FROM start_candidates WHERE track=?', (track,)).fetchone():
                        self.pending_approvals += 1
                    self.db.execute('INSERT INTO start_candidates(track,snapshot) VALUES (?,?) '
                        'ON CONFLICT(track) DO UPDATE SET snapshot=excluded.snapshot', (track, saved.lastrowid))
                else:
                    self.db.execute('DELETE FROM start_candidates WHERE track=?', (track,))
                self.db.execute('INSERT INTO latest VALUES (?,?) ON CONFLICT(track) DO UPDATE SET comparison=excluded.comparison',
                    (track, json.dumps(comparison, ensure_ascii=False, allow_nan=False)))
                events.append(event)
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('initialized')")
        return events


def checked(response):
    if not isinstance(response, dict):
        raise ValueError('Invalid observation response')
    if response.get('isError') or 'error' in response or response.get('success') is False:
        raise McpResponseError(response)
    return response


class TrackWatcher:
    def __init__(self, reader_factory, options, redactor=None):
        self.factory, self.options = reader_factory, options
        self.redact = redactor or Redactor()
        self.auto_join_state = 'BLOCKED'

    @property
    def auto_join_state(self):
        return self._auto_join_state

    @auto_join_state.setter
    def auto_join_state(self, value):
        audit = CURRENT.get()
        if audit is not None:
            audit.auto_join(value)
        if value != 'BLOCKED':
            raise RuntimeError('AUTO-JOIN must remain blocked')
        self._auto_join_state = value

    def poll(self, reader, store):
        self.poll_error = None
        store.pending_approvals = 0
        try:
            fcntl.flock(store.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if CURRENT.get() is not None:
                CURRENT.get().anomaly('lock')
                raise RuntimeError('Observation poll lock unavailable')
            return []
        try:
            listing = checked(reader.list_tracks())
            tracks = listing.get('tracks')
            if not isinstance(tracks, list):
                raise ValueError('Invalid track list')
            ids = [t.get('id') if isinstance(t, dict) else None for t in tracks]
            if any(not isinstance(t, str) or not t for t in ids) or len(set(ids)) != len(ids):
                raise ValueError('Invalid track identity')
            self.observed_track_count = len(tracks)
            # Capture a newly listed Track even if a supplementary read fails.
            # Raw fields are never filled with invented server values.
            errors = {}
            def read(name, fetch):
                if self.poll_error is not None:
                    errors[name] = 'NOT_REQUESTED_AFTER_READ_ERROR'
                    return None
                try:
                    response = fetch()
                    try:
                        checked(response)
                    except (RuntimeError, ValueError) as error:
                        self.poll_error = error
                        errors[name] = 'READ_ERROR'
                    return self.redact(response)
                except (OSError, RuntimeError, ValueError) as error:
                    self.poll_error = error
                    errors[name] = 'READ_ERROR'
                    return None
            race = read('my_race', reader.my_race)
            balance = read('sigil_balance', reader.sigil_balance)
            safe_listing = self.redact(listing)
            observations = []
            for track in tracks:
                track_id = track['id']
                state_key = 'track_state/' + self.redact(track_id)
                state = read(state_key, lambda: reader.track_state(track_id))
                safe_id = self.redact(track_id)
                payload = dict(observed_at_utc=datetime.now(timezone.utc).isoformat(), trackId=safe_id,
                    list_tracks=safe_listing, track_state=state, my_race=race, sigil_balance=balance,
                    read_errors={k: v for k, v in errors.items() if k in {'my_race', 'sigil_balance', state_key}})
                # Keep the complete list in history, but unrelated track changes
                # must not create duplicate snapshots for this Track.
                comparison = dict(list_tracks=self.redact(track), track_state=state,
                                  list_metadata={k: v for k, v in safe_listing.items() if k != 'tracks'},
                                  my_race=race, sigil_balance=balance, read_errors=payload['read_errors'])
                observations.append((safe_id, comparison, payload))
            return store.save(observations)
        finally:
            fcntl.flock(store.lock, fcntl.LOCK_UN)

    def run(self, *, sleep=time.sleep, notify=print, max_polls=None, should_stop=lambda: False):
        reader = None
        store = SnapshotStore(self.options.directory)
        delay, polls = self.options.interval, 0
        audit = CURRENT.get()
        try:
            while not should_stop() and (max_polls is None or polls < max_polls):
                polls += 1
                if audit is not None:
                    audit.begin_poll()
                    audit.auto_join(self.auto_join_state)
                try:
                    if reader is None:
                        reader = self.factory()
                    events = self.poll(reader, store)
                    events += ['PENDING_USER_APPROVAL'] * store.pending_approvals
                    for index, event in enumerate(events):
                        if audit is None or audit.notification(polls, index, event):
                            notify(event)
                    if getattr(self, 'poll_error', None) is not None:
                        raise self.poll_error
                    if audit is not None:
                        audit.end_poll(track_count=self.observed_track_count)
                except (OSError, RuntimeError, ValueError) as error:
                    if audit is not None:
                        audit.end_poll(error)
                    # No response, exception text, IDs or subprocess diagnostics.
                    notify('READ_ERROR_BACKOFF')
                    retry = getattr(error, 'retry_after', 0)
                    wait = max(delay, retry if type(retry) in (int, float) and math.isfinite(retry) else 0)
                    if reader is not None:
                        reader.close()
                        reader = None
                    if max_polls is None or polls < max_polls:
                        if audit is not None:
                            audit.backoff(wait)
                        sleep(wait)
                        if audit is not None:
                            audit.backoff_done()
                    else:
                        return 1
                    delay = min(delay * 2, self.options.backoff_max)
                    continue
                delay = self.options.interval
                if max_polls is None or polls < max_polls:
                    sleep(delay)
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            try:
                if reader is not None:
                    reader.close()
            finally:
                store.close()


def show_status(directory):
    """Inspect only local state, without connecting or creating files."""
    active = False
    lock = directory / 'session.lock'
    if lock.exists():
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active = True
        finally:
            os.close(fd)
    result = {'watcher_active': active, 'auto_join': 'BLOCKED'}
    path = directory / 'observations.sqlite3'
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            result['known_tracks'] = db.execute('SELECT count(*) FROM latest').fetchone()[0]
            result['snapshots'], result['last_snapshot_utc'] = db.execute('SELECT count(*),max(observed_at_utc) FROM snapshots').fetchone()
            result['new_track_events'] = db.execute("SELECT count(*) FROM snapshots WHERE event='NEW_TRACK_DETECTED'").fetchone()[0]
    print(json.dumps(result))
    return 0


def manual_session(watcher, *, hours=None, once=False):
    """Signals request stop after the active poll; waits are interruptible."""
    event = threading.Event()
    deadline = time.monotonic() + hours * 3600 if hours is not None else None
    audit = None
    def stopped():
        return event.is_set() or (deadline is not None and time.monotonic() >= deadline)
    def wait(seconds):
        until = time.monotonic() + seconds
        if deadline is not None:
            until = min(until, deadline)
        while not event.is_set() and time.monotonic() < until:
            if audit is not None:
                audit.heartbeat()
            event.wait(min(30, max(0, until-time.monotonic())))
        if audit is not None:
            audit.heartbeat()
    previous = {}
    directory = watcher.options.directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = None
    binding = None
    try:
        audit = WatchAudit(directory, hours*3600 if hours is not None else None, watcher.options.interval)
        deadline = audit.start + hours*3600 if hours is not None else None
        binding = CURRENT.set(audit)
        try:
            fd = private_file(directory / 'session.lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            audit.anomaly('lock')
            raise
        audit.acquired_lock()
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda number, frame: event.set())
        print('READ_ONLY_WATCHER_STARTED; AUTO_JOIN_BLOCKED', flush=True)
        print(json.dumps({'event': 'audit_session_started', 'session_id': audit.id}), flush=True)
        code = watcher.run(sleep=wait, should_stop=stopped, max_polls=1 if once else None)
        reason = ('manual_stop' if event.is_set() else 'error' if code else
                  'duration_completed' if deadline is not None and time.monotonic() >= deadline else 'poll_limit' if once else 'other')
        audit.finish(reason)
        print(json.dumps({'event': 'audit_session_ended', 'session_id': audit.id, 'reason': reason}), flush=True)
        return code
    except BaseException as error:
        if audit is not None:
            try:
                audit.anomaly('db' if isinstance(error, sqlite3.Error) else 'other')
                audit.finish('manual_stop' if isinstance(error, KeyboardInterrupt) else 'error')
            except (OSError, sqlite3.Error):
                pass  # An unfinished durable session cannot PASS.
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                if audit is not None:
                    audit.anomaly('lock')
                    audit.finish('error')
                raise
        if binding is not None:
            CURRENT.reset(binding)
        if audit is not None:
            audit.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only Track observation archive')
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval-seconds', type=float)
    parser.add_argument('--duration-hours', type=float)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--audit-report', action='store_true')
    parser.add_argument('--session-id')
    args = parser.parse_args(argv)
    try:
        options = WatchOptions.load(args.config)
        if args.audit_report:
            if args.execute:
                raise ValueError('Report cannot execute')
            print(json.dumps(report(options.directory, args.session_id)))
            return 0
        if args.status:
            if args.execute:
                raise ValueError('Status cannot execute')
            return show_status(options.directory)
        if args.interval_seconds is not None:
            options = replace(options, interval=args.interval_seconds)
        if args.duration_hours is not None and (not math.isfinite(args.duration_hours) or args.duration_hours <= 0):
            raise ValueError('Invalid duration')
        settings = load(args.config)
        redactor = Redactor(credential_values(settings))
        if not args.execute:
            print('READ_ONLY_WATCHER_CONFIG_OK; AUTO_JOIN_BLOCKED; NOT_STARTED')
            return 0
        return manual_session(TrackWatcher(lambda: ObservationReader(settings), options, redactor),
                              hours=args.duration_hours, once=args.once)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        print('READ_ONLY_WATCHER_STOPPED')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
