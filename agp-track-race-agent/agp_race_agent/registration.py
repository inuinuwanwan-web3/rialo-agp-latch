"""Durable registration lifecycle. No write is ever automatically resent."""
from __future__ import annotations

import time
import math

from .agent import RaceAgent, is_participable_track
from .audit import AuditLog
from .gateway import McpResponseError
from datetime import datetime, timezone


def sigil_balance(response):
    """Only the real nested response is authoritative; never log its contents."""
    data = response.get("sigilBalance") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return None
    keys = ("creditStatus", "creditRemainingUsd", "creditUsedUsd", "creditCapUsd", "balanceUsd")
    return {key: data.get(key) for key in keys}


def registration_state(response, track_id, *, direct=False):
    """Unknown schemas remain unknown. Recovery must identify the target track.

    Waiting/rejection fields are conservative adapters covered by synthetic
    tests, not a claim that their live AGP schemas have been verified.
    """
    if not isinstance(response, dict):
        return None
    if response.get("isError") or "error" in response or response.get("success") is False:
        return None
    for key in ("data", "state"):
        if isinstance(response.get(key), dict) and "run" in response[key]:
            return registration_state(response[key], track_id, direct=direct)
    run = response.get("run")
    if isinstance(run, dict) and isinstance(run.get("id"), str) and run["id"].strip():
        identity = run.get("trackId", response.get("trackId"))
        if identity is None and isinstance(response.get("track"), dict):
            identity = response["track"].get("id")
        status = run.get("status", response.get("status"))
        if status is not None and status not in {"seated", "started", "running", "finished", "completed"}:
            return None
        if identity != track_id:
            return None
        if ((run.get('finished') is True and status in {'seated', 'started', 'running'})
                or (run.get('finished') is False and status in {'finished', 'completed'})):
            return None
        return "completed" if run.get("finished") is True or status in {"finished", "completed"} else "seated"
    registration = response.get("registration", response.get("queue", response))
    if isinstance(registration, dict):
        identity = registration.get("trackId")
        status = registration.get("status")
        if identity == track_id and status in {"registered", "waiting", "waitlisted", "signup"}:
            return "registered_waiting"
    return None


class Registration:
    def __init__(self, settings, gateway, database, sleep, notify):
        self.settings, self.gateway, self.db = settings, gateway, database
        self.sleep, self.notify = sleep, notify
        self.db.execute("CREATE TABLE IF NOT EXISTS registration_state (id TEXT PRIMARY KEY, status TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0)")
        # Old notification-only 'seen' entries must not prevent registration.
        # Legacy joined runs may already have an uncertain paid operation.
        self.db.execute("CREATE TABLE IF NOT EXISTS participation (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO registration_state (id,status) SELECT id, CASE status WHEN 'completed' THEN 'completed' WHEN 'joined' THEN 'solving' ELSE 'registration_attempted' END FROM participation")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(registration_state)")}
        if "deadline" not in columns:
            self.db.execute("ALTER TABLE registration_state ADD COLUMN deadline REAL NOT NULL DEFAULT 0")
        self.db.commit()

    def status(self, track_id):
        row = self.db.execute("SELECT status FROM registration_state WHERE id=?", (track_id,)).fetchone()
        return row[0] if row else None

    def set(self, track_id, status, retry_at=0):
        with self.db:
            self.db.execute("INSERT INTO registration_state(id,status,retry_at) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,retry_at=excluded.retry_at WHERE registration_state.status != 'blocked'", (track_id, status, retry_at))

    def block(self, track_id):
        self.set(track_id, 'blocked')
        return '書き込み結果を確認できないためTrackをblockedにして停止しました。'

    def recover(self):
        rows = self.db.execute("SELECT id FROM registration_state WHERE status IN ('blocked','registration_attempted','registered_waiting','seated','token_waiting','solving')").fetchall()
        if rows:
            for (track_id,) in rows:
                self.set(track_id, 'blocked')
            return '書き込み結果未確認のTrackがあるためblockedのまま安全停止しました。'
        return None

    def reconcile(self, track_id):
        # Reconciliation must never rehabilitate an uncertain write.
        return self.block(track_id)

    def advance(self, track_id, state):
        if self.status(track_id) == "blocked":
            return self.block(track_id)
        while state == "registered_waiting":
            self.sleep(max(60, self.settings.monitor_interval_seconds))
            response = self.gateway.call("my_race")
            state = registration_state(response, track_id)
            if state is None:
                return "待機中の参加状態を確認できないため停止しました。"
            self.set(track_id, state)
        if state == "completed":
            return None
        if state not in {"seated", "token_waiting"}:
            return "着席を確認できないため停止しました。"
        result, initial = self.wait_ready(track_id)
        if result is not None:
            return result
        if initial.get("finished") is True:
            self.set(track_id, "completed")
            return None
        # Persist before Solver or any paid call. Never resume uncertain solving.
        self.set(track_id, "solving")
        from .read_recovery import ReadRecovery
        if isinstance(self.gateway, ReadRecovery):
            self.gateway.deadline = None
        agent = RaceAgent(self.settings, self.gateway, AuditLog(self.settings.log_dir))
        agent.require_economics = True
        agent.track_id = track_id
        agent.initial_state = initial
        result = agent.solve()
        self.set(track_id, "completed" if agent.completed else "blocked")
        if not agent.completed:
            return result
        self.notify("レース完了。新Track監視を継続します。")
        return None

    def wait_ready(self, track_id):
        if self.status(track_id) == "blocked":
            return self.block(track_id), None
        from .read_recovery import ReadDeadline
        self.set(track_id, "token_waiting")
        row = self.db.execute("SELECT deadline FROM registration_state WHERE id=?", (track_id,)).fetchone()
        deadline = row[0] or time.time() + 600
        with self.db:
            self.db.execute("UPDATE registration_state SET deadline=? WHERE id=?", (deadline, track_id))
        previous = getattr(self.gateway, "deadline", None)
        # Only our wrapper owns a request deadline; no attributes on raw Fakes.
        from .read_recovery import ReadRecovery
        wrapped = isinstance(self.gateway, ReadRecovery)
        if wrapped:
            self.gateway.deadline = deadline
        try:
            for attempt in range(11):
                if time.time() >= deadline:
                    break
                state = self.gateway.call("track_state")
                if not isinstance(state, dict) or state.get("isError") or "error" in state or state.get('success') is False:
                    return self.block(track_id), None
                identity = state.get("trackId")
                if identity != track_id:
                    return self.block(track_id), None
                if state.get("finished") is True:
                    if state.get('status') in {'started', 'running'} or state.get('phase') in {'started', 'running'}:
                        return self.block(track_id), None
                    return None, state
                status = state.get("status", state.get("phase"))
                if status is not None and status not in {"started", "running", "waiting", "registered", "seated", "pending"}:
                    return self.block(track_id), None
                started = state.get("started") is True or status in {"started", "running"}
                if state.get("started") is False:
                    started = False
                if not started and state.get("started") is not False and status not in {"waiting", "registered", "seated", "pending"}:
                    return self.block(track_id), None
                values = [state.get(k) for k in ("remainingUsd", "spentUsd")]
                valid = all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values)
                if valid and values[1] >= self.settings.max_usd:
                    self.set(track_id, "failed_terminal")
                    return "予算上限に達したため安全停止しました。", None
                if started and valid and values[0] > 0:
                    return None, state
                if attempt == 10:
                    break
                self.sleep(min(max(60, self.settings.monitor_interval_seconds), max(0, deadline - time.time())))
        except ReadDeadline:
            pass
        finally:
            if wrapped:
                self.gateway.deadline = previous
        self.set(track_id, "failed_terminal")
        return "Token付与・競技開始の待機期限に達したため安全停止しました。", None

    def join(self, track_id):
        if self.status(track_id) not in {None, "detected", "failed_retryable"}:
            return None
        self.set(track_id, "detected")
        try:
            race = self.gateway.call("my_race")
            if (not isinstance(race, dict) or "run" not in race or
                (race["run"] is not None and not (isinstance(race["run"], dict) and race["run"].get("finished") is True)) or
                race.get("registration") is not None):
                self.set(track_id, "failed_retryable")
                return "既存レース・登録がないことを確認できないため停止しました。"
            # Balance is observational before seating, never a funding gate.
            self.balance = sigil_balance(self.gateway.call("sigil_balance"))
            response = self.gateway.call("list_tracks")
            tracks = response.get("tracks") if isinstance(response, dict) else None
            if not isinstance(tracks, list):
                raise ValueError("Invalid track list")
            track = next((t for t in tracks if isinstance(t, dict) and t.get("id") == track_id), None)
            if not is_participable_track(track, datetime.now(timezone.utc)):
                self.set(track_id, "failed_retryable")
                return None
        except (OSError, RuntimeError, ValueError):
            self.set(track_id, "failed_retryable")
            return "登録前の読み取りを確認できないため停止しました。"
        self.set(track_id, "registration_attempted")
        try:
            response = self.gateway.call("start_track", {"trackId": track_id})
            if isinstance(response, dict) and (response.get("isError") or "error" in response or response.get("success") is False):
                raise McpResponseError(response)
        except (OSError, RuntimeError, ValueError):
            return self.block(track_id)
        from .write_safety import valid_response
        if not valid_response("start_track", response, track_id):
            return self.block(track_id)
        state = registration_state(response, track_id, direct=True)
        if state is None:
            return self.block(track_id)
        self.set(track_id, state)
        return self.advance(track_id, state)
