"""Human-approved, at-most-once start observation. Never enables race solving."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import load
from .gateway import McpGateway
from .observation_gateway import ObservationReader, credential_values
from .track_watcher import Redactor, WatchOptions
from .write_safety import WriteBlocked, WriteSafety, WriteStore

PENDING = 'PENDING_USER_APPROVAL'
UNKNOWN = 'UNKNOWN_START_RESPONSE'


class StartRedactor(Redactor):
    """Also redact aliases of response credentials before any disk write."""
    def __call__(self, value):
        discovered = []
        def visit(item, sensitive=False):
            if isinstance(item, dict):
                for key, child in item.items():
                    visit(child, sensitive or bool(self.sensitive.search(key)))
            elif isinstance(item, list):
                for child in item:
                    visit(child, sensitive)
            elif isinstance(item, str):
                if sensitive and item:
                    discovered.append(item)
                try:
                    parsed = json.loads(item)
                except ValueError:
                    return
                if isinstance(parsed, (dict, list)):
                    visit(parsed, sensitive)
        visit(value)
        return Redactor([*self.secrets, *discovered])(value)


def registration_candidate(track, now=None):
    """Review shortlist only. No claim about phase, eligibility or funding."""
    if not isinstance(track, dict) or not isinstance(track.get('id'), str) or not track['id'].strip():
        return False
    if track.get('started') is not False or track.get('over') is not False or track.get('timedOut') is True:
        return False
    deadline = track.get('registrationClosesAt')
    if deadline is not None:
        try:
            bound = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
            if bound.tzinfo is None or bound <= (now or datetime.now(timezone.utc)):
                return False
        except (ValueError, TypeError, AttributeError):
            return False
    return True


def candidates(directory):
    path = directory / 'observations.sqlite3'
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        # Existing archives need no migration until the watcher next saves.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='start_candidates'").fetchone():
            return []
        rows = db.execute('SELECT c.track,s.payload FROM start_candidates c JOIN snapshots s ON s.id=c.snapshot').fetchall()
    from .write_safety import SAFETY_DB
    attempted = set()
    if SAFETY_DB.exists():
        with sqlite3.connect(SAFETY_DB.resolve().as_uri() + '?mode=ro', uri=True) as db:
            attempted = {row[0] for row in db.execute('SELECT track FROM writes')}
    return [{'trackId': track, 'status': 'START_BLOCKED_PRIOR_ATTEMPT' if track in attempted else PENDING,
             'evidence': json.loads(payload)} for track, payload in rows]


class StartArchive(WriteStore):
    """Same account-wide DB as all existing write paths; no alternate DB flag."""
    def __init__(self):
        super().__init__()
        self.db.execute('CREATE TABLE IF NOT EXISTS manual_starts ('
            'track TEXT PRIMARY KEY, nonce TEXT NOT NULL, status TEXT NOT NULL, '
            'evidence TEXT NOT NULL, approval TEXT, response TEXT, error_type TEXT)')
        self.db.commit()

    def prepare(self, track, evidence, redact):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO manual_starts(track,nonce,status,evidence) VALUES (?,?,?,?)',
                (track, uuid4().hex, PENDING, json.dumps(redact(evidence), ensure_ascii=False)))
        row = self.db.execute('SELECT nonce,status FROM manual_starts WHERE track=?', (track,)).fetchone()
        if row[1] != PENDING or self.status(track) is not None:
            raise WriteBlocked()
        return f'MAX APPROVE START {track} {row[0]}'

    def consume(self, track, approval, evidence, redact):
        row = self.db.execute('SELECT nonce,status FROM manual_starts WHERE track=?', (track,)).fetchone()
        if row is None or row[1] != PENDING or approval != f'MAX APPROVE START {track} {row[0]}':
            raise WriteBlocked()
        # Atomic reservation commits before any start byte can be sent. The
        # blocked row is intentionally never rehabilitated, even on timeout.
        self.reserve(track, 'start_track', 'manual-start', None, 0)
        with self.db:
            self.db.execute('UPDATE manual_starts SET status=?,approval=?,evidence=? WHERE track=?',
                (UNKNOWN, approval, json.dumps(redact(evidence), ensure_ascii=False), track))

    def capture(self, track, envelope, redact):
        with self.db:
            self.db.execute('UPDATE manual_starts SET response=? WHERE track=?',
                (json.dumps(redact(envelope), ensure_ascii=False, allow_nan=False), track))

    def failure(self, track, error):
        with self.db:
            self.db.execute('UPDATE manual_starts SET error_type=? WHERE track=?', (type(error).__name__, track))

    def report(self, track):
        row = self.db.execute('SELECT status,evidence,response,error_type FROM manual_starts WHERE track=?', (track,)).fetchone()
        return None if row is None else dict(status=row[0], evidence=json.loads(row[1]),
            response=json.loads(row[2]) if row[2] is not None else None, error_type=row[3], auto_join='BLOCKED')


class StartTransport(McpGateway):
    """Existing JSON-RPC transport with one consumable start-only wire permit."""
    def __init__(self, settings):
        self._start_permit = None
        self._capture_response = None
        super().__init__(settings, allow_writes=False)

    def request(self, method, params):
        if method != 'initialize' and not (
                method == 'tools/call' and params == self._start_permit):
            raise WriteBlocked()
        return super().request(method, params)

    def _write(self, message, deadline):
        method = message.get('method')
        if method == 'tools/call':
            if self._start_permit is None or message.get('params') != self._start_permit:
                raise WriteBlocked()
            self._start_permit = None  # Consume before even a partial pipe write.
        elif method not in {'initialize', 'notifications/initialized'}:
            raise WriteBlocked()
        return super()._write(message, deadline)

    def _readline(self, deadline):
        line = super()._readline(deadline)
        if self._capture_response is not None:
            envelope = json.loads(line)
            if envelope.get('id') == self._id:
                self._capture_response(envelope)  # Preserve before decoder/error handling.
        return line

    def start_once(self, archive, track, approval, evidence, redact):
        archive.consume(track, approval, evidence, redact)
        params = {'name': 'start_track', 'arguments': {'trackId': track}}
        self._start_permit = params
        self._write_authorized = ('start_track', params['arguments'])
        self._capture_response = lambda envelope: archive.capture(track, envelope, redact)
        try:
            # Deliberately no _call: no retries, decoding, my_race reconciliation,
            # success-shape assumptions, or transition to ask/guess.
            self.request('tools/call', params)
        except (OSError, RuntimeError, ValueError) as error:
            archive.failure(track, error)
        finally:
            self._start_permit = self._capture_response = None
            self._write_authorized = False
        return UNKNOWN


def run_approved(settings, archive, track, approval, evidence, redact, factory=StartTransport):
    # Validate before opening even an MCP connection. Approval is track-specific.
    row = archive.db.execute('SELECT nonce,status FROM manual_starts WHERE track=?', (track,)).fetchone()
    if row is None or row[1] != PENDING or approval != f'MAX APPROVE START {track} {row[0]}':
        return PENDING
    if archive.status(track) is not None:
        raise WriteBlocked()
    WriteSafety(settings).import_legacy()
    gateway = factory(settings)
    try:
        return gateway.start_once(archive, track, approval, evidence, redact)
    finally:
        gateway.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='MAX-approved single start; AUTO-JOIN BLOCKED')
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    parser.add_argument('--track-id')
    parser.add_argument('--review', action='store_true', help='Read fresh evidence and prompt MAX on a terminal')
    parser.add_argument('--response', action='store_true', help='Show saved redacted response; no MCP connection')
    args = parser.parse_args(argv)
    archive = reader = None
    try:
        if args.review and args.response:
            raise ValueError('Choose review or response')
        if not args.review and not args.response:
            print(json.dumps({'auto_join': 'BLOCKED', 'candidates': candidates(WatchOptions.load(args.config).directory)}, ensure_ascii=False))
            return 0
        if not args.track_id:
            raise ValueError('Track required')
        archive = StartArchive()
        if args.response:
            print(json.dumps(archive.report(args.track_id), ensure_ascii=False))
            return 0
        # No unattended --approve flag, environment variable or piped approval.
        if not sys.stdin.isatty():
            raise WriteBlocked()
        settings = load(args.config)
        redact = StartRedactor(credential_values(settings))
        reader = ObservationReader(settings)
        listing, race, balance = reader.list_tracks(), reader.my_race(), reader.sigil_balance()
        track = next((t for t in listing.get('tracks', []) if t.get('id') == args.track_id), None)
        if not registration_candidate(track) or race.get('run', 'unknown') is not None or race.get('registration') is not None:
            raise WriteBlocked()
        evidence = {'track': track, 'my_race': race, 'sigil_balance': balance}
        phrase = archive.prepare(args.track_id, evidence, redact)
        print(json.dumps({'status': PENDING, 'eligibility': 'UNCONFIRMED', 'auto_join': 'BLOCKED', 'evidence': redact(evidence)}, ensure_ascii=False))
        print('MAX: registration eligibility/fee/credit are unconfirmed. Approve ONE start observation only. No ask/guess.')
        print('To approve, type exactly: ' + phrase)
        approval = input('MAX approval (anything else cancels): ')
        if approval != phrase:
            print(PENDING)
            return 0
        # Re-read before sending: approval of stale/changed evidence is not reused.
        fresh_list, fresh_race, fresh_balance = reader.list_tracks(), reader.my_race(), reader.sigil_balance()
        fresh_track = next((t for t in fresh_list.get('tracks', []) if t.get('id') == args.track_id), None)
        if (not registration_candidate(fresh_track) or fresh_track != track or fresh_race != race or fresh_balance != balance):
            print('EVIDENCE_CHANGED; PENDING_USER_APPROVAL; NOTHING_SENT')
            return 0
        print(run_approved(settings, archive, args.track_id, approval, evidence, redact))
        print('Response: python -m agp_race_agent.manual_start --response --track-id ' + args.track_id)
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, EOFError, KeyboardInterrupt):
        print('MANUAL_START_STOPPED; AUTO_JOIN_BLOCKED; DO_NOT_RETRY_IF_ATTEMPTED')
        return 1
    finally:
        if reader is not None:
            reader.close()
        if archive is not None:
            archive.close()


if __name__ == '__main__':
    raise SystemExit(main())
