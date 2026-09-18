from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .audit import AuditLog
from .config import Settings
from .gateway import McpGateway
from .solver import Solver, SolverError
from .write_safety import READ_TOOLS, WRITE_TOOLS, valid_response


class McpCallError(RuntimeError):
    """A failed MCP call, with no server diagnostics exposed."""


def join_window_confirmed(track: Any, now: datetime) -> bool:
    """No official positive Join Window contract is established yet.

    No phase/status value, timestamp, missing field or null value constitutes
    permission. Keep production denied; only tests may mock this function.
    There is deliberately no configuration or CLI override.
    """
    return False


def is_participable_track(track: Any, now: datetime) -> bool:
    """Reject known exclusions, then require affirmative Join Window proof.

    The structural checks below are necessary filters, never authorization.
    In particular null deadlines/capacity and an unfamiliar nonempty phase
    cannot establish permission: the final independent contract denies them.
    """
    if not isinstance(track, dict):
        return False
    if not isinstance(track.get("id"), str) or not track["id"].strip():
        return False
    for field in ("over", "timedOut", "invitedOnly", "teamOnly", "started"):
        if track.get(field) is not False:
            return False
    phase = track.get("phase")
    if not isinstance(phase, str) or not phase.strip() or phase.strip().lower() in {"over", "closed"}:
        return False
    for field in ("startsAt", "registrationClosesAt", "endsAt"):
        if field not in track:
            return False
        value = track[field]
        if value is None and field != "startsAt":
            continue
        if not isinstance(value, str):
            return False
        try:
            bound = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if bound.tzinfo is None or bound <= now:
            return False
    count = track.get("racerCount")
    if type(count) is not int or count < 0 or "maxRacers" not in track:
        return False
    capacity = track["maxRacers"]
    if capacity is not None and (type(capacity) is not int or capacity <= count):
        return False
    return join_window_confirmed(track, now) is True


class RaceAgent:
    def __init__(self, settings: Settings, gateway: McpGateway, audit: AuditLog) -> None:
        self.settings, self.gateway, self.audit = settings, gateway, audit
        self.solver = Solver(settings.solver_command)
        self.spent = 0.0
        self.completed = False
        self.require_economics = False
        self.track_id = None
        self.write_blocked = False

    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if tool not in READ_TOOLS | WRITE_TOOLS or (tool in WRITE_TOOLS and self.write_blocked):
            raise McpCallError('MCP operation blocked')
        self.audit.write("tool_call", tool=tool)
        try:
            result = self.gateway.call(tool, args)
            if tool in WRITE_TOOLS and not valid_response(tool, result, (args or {}).get('trackId', self.track_id)):
                raise McpCallError('MCP response unconfirmed')
        except (OSError, RuntimeError, ValueError):
            if tool in WRITE_TOOLS:
                self.write_blocked = True
            self.audit.write("safe_exit", reason="mcp_call_failed", tool=tool)
            raise McpCallError("MCP operation failed") from None
        if isinstance(result, dict) and (result.get("isError") or "error" in result):
            raise McpCallError("MCP operation failed")
        # Log operation metadata only; never persist MCP response values or credentials.
        self.audit.write("tool_result", tool=tool, result_type=type(result).__name__, )
        return result

    def run(self, preferred_track: str | None = None, *, require_no_existing=False,
            before_start=None, after_start=None) -> str:
        try:
            return self._run(preferred_track, require_no_existing=require_no_existing,
                             before_start=before_start, after_start=after_start)
        except (McpCallError, ValueError, TypeError, AttributeError):
            return "MCP操作の応答を確認できないため終了しました。副作用のある操作は再送していません。"

    def _run(self, preferred_track: str | None = None, *, require_no_existing=False,
             before_start=None, after_start=None) -> str:
        race = self.call("my_race")
        if (require_no_existing and isinstance(race, dict)
                and isinstance(race.get("run"), dict)
                and race["run"].get("finished") is True):
            race = {"run": None}
        if require_no_existing and (not isinstance(race, dict) or "run" not in race or race["run"] is not None):
            return "既存レースがないことを確認できないため終了しました。"
        run = race.get("run")
        if not run:
            tracks = self.call("list_tracks").get("tracks", [])
            now = datetime.now(timezone.utc)
            candidates = [t for t in tracks if is_participable_track(t, now)]
            track = next((t for t in candidates if t.get("id") == preferred_track), None) if preferred_track else next(iter(candidates), None)
            if not track:
                self.audit.write("safe_exit", reason="no_participable_track")
                return "参加可能なTrackがないため終了しました。"
            self.track_id = track['id']
            from .registration import sigil_balance
            self.balance = sigil_balance(self.call("sigil_balance"))
            # Tokens are granted on seating; pre-seat funding is not permission.
            if not is_participable_track(track, datetime.now(timezone.utc)):
                return "参加可能なTrackがないため終了しました。"
            if before_start is not None and not before_start(track["id"]):
                return "参加試行を安全に記録できないため終了しました。"
            try:
                registration = self.call("start_track", {"trackId": track["id"]})
            except (OSError, RuntimeError, ValueError):
                # Registration may have reached the server: never retry blindly.
                self.audit.write("safe_exit", reason="registration_error")
                return "参加登録の応答を確認できないため終了しました。"
            # The existing start_track fixture establishes only run.id. There
            # is no verified waiting/failure schema, so do not infer a status
            # from messages or from a merely non-empty response.
            run = registration.get("run") if isinstance(registration, dict) else None
            run_id = run.get("id") if isinstance(run, dict) else None
            if not isinstance(run_id, str) or not run_id.strip():
                self.audit.write("safe_exit", reason="registration_unconfirmed")
                return "参加成功を確認できないため終了しました。"
            if after_start is not None:
                after_start(track["id"])
        return self.solve()

    def solve(self) -> str:
        try:
            return self._solve()
        except (McpCallError, ValueError, TypeError, AttributeError):
            return "MCP操作の応答を確認できないため終了しました。副作用のある操作は再送していません。"

    def _solve(self) -> str:
        history: list[dict[str, Any]] = []
        initial_race_spent: float | None = None
        questions = guesses = 0
        while True:
            state = getattr(self, "initial_state", None)
            self.initial_state = None
            if state is None:
                state = self.call("track_state")
            if self.track_id is not None and (not isinstance(state, dict) or state.get('trackId') != self.track_id):
                self.write_blocked = True
                return '対象Raceを確認できないため停止しました。'
            if state.get("finished") is True:
                if state.get('status') in {'started', 'running'} or state.get('phase') in {'started', 'running'}:
                    self.write_blocked = True
                    return '競技状態が矛盾しているため停止しました。'
                self.completed = True
                return "レースを完走しました。"
            if self.track_id is not None and (state.get('started') is not True or state.get('status') not in (None, 'started', 'running') or state.get('phase') not in (None, 'started', 'running')):
                self.write_blocked = True
                return '競技状態を確認できないため停止しました。'
            if self.require_economics and not all(type(state.get(k)) in (int, float) for k in ("remainingUsd", "spentUsd")):
                return "着席後の予算情報を確認できないため終了しました。"
            remaining = float(state.get("remainingUsd", self.settings.max_usd - self.spent))
            race_spent = float(state.get("spentUsd", 0))
            if not all(math.isfinite(value) and value >= 0 for value in (remaining, race_spent)):
                return "予算情報が不正なため終了しました。"
            if initial_race_spent is None:
                initial_race_spent = race_spent
            accounted_spent = max(race_spent, initial_race_spent + self.spent)
            if remaining <= 0 or accounted_spent >= self.settings.max_usd:
                self.audit.write("safe_exit", reason="budget_limit")
                return "予算上限に達したため終了しました。"
            while questions < self.settings.max_questions and guesses < self.settings.max_guesses:
                try:
                    decision = self.solver.decide(state, history)
                except SolverError:
                    self.audit.write("safe_exit", reason="solver_failure")
                    return "Solverの安全な出力を取得できないため終了しました。"
                if decision.get("question"):
                    if "questionCostUsd" not in state or state["questionCostUsd"] is None:
                        self.audit.write("safe_exit", reason="missing_question_cost")
                        return "質問費用を確認できないため終了しました。"
                    try:
                        cost = float(state["questionCostUsd"])
                    except (TypeError, ValueError):
                        self.audit.write("safe_exit", reason="missing_question_cost")
                        return "質問費用を確認できないため終了しました。"
                    if not math.isfinite(cost) or cost < 0:
                        self.audit.write("safe_exit", reason="missing_question_cost")
                        return "質問費用を確認できないため終了しました。"
                    if accounted_spent + cost > self.settings.max_usd or cost > remaining:
                        self.audit.write("safe_exit", reason="budget_limit")
                        return "予算上限に達したため終了しました。"
                    answer = self.call("ask", {"question": decision["question"]})
                    self.spent += cost; accounted_spent += cost; remaining -= cost
                    questions += 1; history.append({"question": decision["question"], "answer": answer})
                    continue
                cost = float(state.get("guessCostUsd", float("nan")))
                if not math.isfinite(cost) or cost < 0:
                    return "回答費用を確認できないため終了しました。"
                if accounted_spent + cost > self.settings.max_usd or cost > remaining:
                    self.audit.write("safe_exit", reason="budget_limit")
                    return "予算上限に達したため終了しました。"
                result = self.call("guess", {"guess": decision["guess"]})
                self.spent += cost; guesses += 1
                history.append({"guess": decision["guess"], "result": result})
                if result.get("correct") is True:
                    questions = guesses = 0
                    history = []
                break
            else:
                self.audit.write("safe_exit", reason="point_limits")
                return "ポイントごとの操作上限に達したため終了しました。"
