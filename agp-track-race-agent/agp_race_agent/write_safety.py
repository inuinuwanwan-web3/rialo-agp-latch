"""Fail-closed, account-local write barrier shared by every MCP entry point.

No positive AGP allowance contract has been established in this repository.
The production contract therefore authorizes no writes. Tests may explicitly
inject a Fake contract; there is no CLI/config switch for bypassing this gate.
"""
from __future__ import annotations

import math
import os
import pwd
import sqlite3
from pathlib import Path
from uuid import uuid4

READ_TOOLS = frozenset({'my_race', 'list_tracks', 'sigil_balance', 'track_state'})
WRITE_TOOLS = frozenset({'start_track', 'ask', 'guess'})
SAFETY_DB = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/agp-track-race-agent/write-safety.sqlite3'


class WriteBlocked(RuntimeError):
    def __init__(self):
        super().__init__('Track write blocked')


class AgpWriteContract:
    def authorized(self, tool, balance, state):
        # The observed sigilBalance fields do not establish allowance or a
        # verified positive credit state. Never invent such a field or value.
        return False


def valid_response(tool, response, track_id=None):
    if not isinstance(response, dict) or not response or response.get('isError') or 'error' in response or response.get('success') is False:
        return False
    if tool == 'start_track':
        run = response.get('run')
        if not isinstance(run, dict) or not isinstance(run.get('id'), str) or not run['id'].strip():
            return False
        if run.get('trackId') != track_id:
            return False
        status = run.get('status')
        if status is not None and status not in {'seated', 'started', 'running'}:
            return False
        return run.get('finished') in (None, False) and type(run.get('finished')) in (type(None), bool)
    if tool == 'ask':
        return isinstance(response.get('answer'), str) and bool(response['answer'].strip())
    if tool == 'guess':
        return type(response.get('correct')) is bool
    return False


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class WriteStore:
    def __init__(self):
        # Canonical path is independent of monitor DB/config/working directory.
        path = SAFETY_DB
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS writes (track TEXT PRIMARY KEY, state TEXT NOT NULL, owner TEXT NOT NULL, permit TEXT, run TEXT, spent REAL NOT NULL DEFAULT 0)')
        self.db.commit()

    def close(self):
        self.db.close()

    def status(self, track):
        row = self.db.execute('SELECT state FROM writes WHERE track=?', (track,)).fetchone()
        return row[0] if row else None

    def block(self, track):
        with self.db:
            self.db.execute("INSERT INTO writes(track,state,owner) VALUES (?,'blocked','') ON CONFLICT(track) DO UPDATE SET state='blocked',permit=NULL", (track,))

    def reserve(self, track, tool, owner, run, spent):
        # Commit blocked BEFORE sending. A crash/kill/failed DB completion leaves
        # the durable state blocked. Only this live request owns its completion.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT state,owner,run,spent FROM writes WHERE track=?', (track,)).fetchone()
            if tool == 'start_track':
                if row is not None:
                    raise WriteBlocked()
                if self.db.execute("SELECT 1 FROM writes WHERE state='registered' OR permit IS NOT NULL LIMIT 1").fetchone():
                    raise WriteBlocked()
            elif row is None or row[0] != 'registered' or row[1] != owner or row[2] != run:
                raise WriteBlocked()
            nonce = uuid4().hex
            total = max(spent, row[3] if row else 0)
            self.db.execute("INSERT INTO writes(track,state,owner,permit,run,spent) VALUES (?,'blocked',?,?,?,?) ON CONFLICT(track) DO UPDATE SET state='blocked',permit=excluded.permit,spent=excluded.spent", (track, owner, nonce, run, total))
            self.db.commit()
            return nonce, total
        except BaseException:
            self.db.rollback()
            raise

    def complete(self, track, permit, run, spent):
        with self.db:
            result = self.db.execute("UPDATE writes SET state='registered',permit=NULL,run=?,spent=? WHERE track=? AND state='blocked' AND permit=?", (run, spent, track, permit))
            if result.rowcount != 1:
                raise WriteBlocked()

    def finish(self, track, owner):
        with self.db:
            self.db.execute("UPDATE writes SET state='completed' WHERE track=? AND owner=? AND state='registered'", (track, owner))


class WriteSafety:
    def __init__(self, settings, contract=None):
        self.settings = settings
        self.contract = contract if contract is not None else AgpWriteContract()
        self.owner = uuid4().hex
        self.track = self.run = None
        self.balance = self.state = self.race = None

    def import_legacy(self):
        # Run before a new registration can be prepared. Existing attempts,
        # including completed entries, must not become fresh registrations.
        path = self.settings.monitor_seen_file
        if not path.exists():
            return
        old = store = None
        try:
            old = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tracks = set()
            if 'registration_state' in tables:
                tracks.update(row[0] for row in old.execute("SELECT id FROM registration_state WHERE status NOT IN ('detected','failed_retryable')"))
            if 'participation' in tables:
                tracks.update(row[0] for row in old.execute('SELECT id FROM participation'))
            if tracks:
                store = WriteStore()
                for track in tracks:
                    store.block(track)
        except Exception:
            raise WriteBlocked() from None
        finally:
            if old is not None:
                old.close()
            if store is not None:
                store.close()

    def observe(self, tool, response):
        if tool == 'sigil_balance':
            self.balance = response
        elif tool == 'my_race':
            self.race = response
        elif tool == 'track_state':
            self.state = response
            if (self.track and isinstance(response, dict) and response.get('trackId') == self.track
                    and response.get('finished') is True and not response.get('isError') and 'error' not in response
                    and response.get('success') is not False
                    and response.get('status') not in {'started', 'running'}):
                store = WriteStore()
                try:
                    store.finish(self.track, self.owner)
                finally:
                    store.close()

    def perform(self, tool, arguments, send, verify_run):
        track = arguments.get('trackId') if tool == 'start_track' else self.track
        if not isinstance(track, str) or not track.strip():
            raise WriteBlocked()
        store = None
        try:
            store = WriteStore()
            if store.status(track) in {'blocked', 'completed'}:
                raise WriteBlocked()
            if tool == 'start_track':
                if set(arguments) != {'trackId'}:
                    raise WriteBlocked()
                # Fresh pre-send read; an old cached no-race result is not proof.
                race = verify_run()
                if not isinstance(race, dict) or race.get('isError') or 'error' in race or race.get('success') is False or 'run' not in race or race['run'] is not None or race.get('registration') is not None:
                    raise WriteBlocked()
                cost = spent = 0.0
            else:
                key = 'question' if tool == 'ask' else 'guess'
                if set(arguments) != {key} or not isinstance(arguments[key], str) or not arguments[key].strip():
                    raise WriteBlocked()
                race = verify_run()
                run = race.get('run') if isinstance(race, dict) else None
                if (not isinstance(race, dict) or race.get('isError') or 'error' in race or race.get('success') is False
                        or not isinstance(run, dict) or run.get('id') != self.run or run.get('trackId') != track
                        or run.get('finished') is not False or run.get('status') not in (None, 'seated', 'started', 'running')):
                    raise WriteBlocked()
                state = self.state
                if not isinstance(state, dict) or state.get('trackId') != track or state.get('finished') is not False or state.get('started') is not True or state.get('isError') or 'error' in state or state.get('success') is False:
                    raise WriteBlocked()
                if state.get('status') not in (None, 'started', 'running') or state.get('phase') not in (None, 'started', 'running'):
                    raise WriteBlocked()
                cost = state.get('questionCostUsd' if tool == 'ask' else 'guessCostUsd')
                spent = state.get('spentUsd')
                remaining = state.get('remainingUsd')
                if not all(finite(v) for v in (cost, spent, remaining)) or remaining <= 0 or cost > remaining:
                    raise WriteBlocked()
            if self.contract.authorized(tool, self.balance, self.state) is not True:
                raise WriteBlocked()
            if not finite(self.settings.max_usd) or not 0 < self.settings.max_usd <= 1:
                raise WriteBlocked()
            permit, spent = store.reserve(track, tool, self.owner, self.run, spent)
            if spent + cost > self.settings.max_usd:
                raise WriteBlocked()
            if tool == 'start_track':
                response = self.confirm_start(track, send, verify_run)
            else:
                response = send()
            if not valid_response(tool, response, track):
                raise WriteBlocked()
            run = response['run']['id'] if tool == 'start_track' else self.run
            store.complete(track, permit, run, spent + cost)
            self.track, self.run = track, run
            if tool != 'start_track':
                self.state = {**self.state, 'remainingUsd': self.state['remainingUsd'] - cost, 'spentUsd': spent + cost}
            return response
        except BaseException as error:
            if store is not None:
                try:
                    store.block(track)
                except Exception:
                    pass  # Pre-send durable blocked remains if post-send DB I/O fails.
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise WriteBlocked() from None
        finally:
            if store is not None:
                store.close()

    def confirm_start(self, track, send, verify_run):
        """Reconcile this live attempt by reads only, never by a second send.

        The durable pre-send block and request nonce remain held throughout.
        Crashes and failed reconciliation cannot be adopted by a new process.
        Existing run fields are conservative adapters, not a verified live grid
        schema; unfamiliar grid shapes fail closed. Production funding remains
        denied until its contract is established.
        """
        response = None
        try:
            response = send()
        except (OSError, RuntimeError, ValueError):
            pass  # Outcome unknown. Only my_race may resolve this attempt.
        confirmed = verify_run()
        if not valid_response('start_track', confirmed, track):
            raise WriteBlocked()
        if confirmed['run'].get('finished') is not False:
            raise WriteBlocked()
        if (valid_response('start_track', response, track)
                and response['run']['id'] != confirmed['run']['id']):
            raise WriteBlocked()
        return confirmed
