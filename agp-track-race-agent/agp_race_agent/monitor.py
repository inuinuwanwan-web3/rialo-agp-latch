from __future__ import annotations

import fcntl
import math
import os
import sqlite3
import time
from datetime import datetime, timezone

from .agent import is_participable_track
from .config import Settings, preflight
from .gateway import McpGateway


class TrackMonitor:
    """Continuous monitor. Persist before emitting for at-most-once notification.

    A crash between commit and output can lose a notification, but cannot cause
    a duplicate. SQLite serializes claims across monitors sharing the same file.
    """

    def __init__(self, settings: Settings, gateway: McpGateway) -> None:
        self.settings, self.gateway = settings, gateway

    def run(self, *, sleep=time.sleep, notify=print, auto_join=False) -> str:
        interval = self.settings.monitor_interval_seconds
        if not math.isfinite(interval) or interval < 60:
            return "監視間隔が不正なため停止しました。"
        database = None
        lock_fd = None
        try:
            if auto_join and preflight(self.settings):
                return "自動参加の設定を確認できないため停止しました。"
            path = self.settings.monitor_seen_file
            path.parent.mkdir(parents=True, exist_ok=True)
            if auto_join:
                lock_fd = os.open(str(path) + ".auto.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Private file from creation; do not follow a final-component symlink.
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            database = sqlite3.connect(path, timeout=5)
            database.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
            database.commit()
            from .read_recovery import ReadRecovery
            gateway = ReadRecovery(self.gateway, sleep)
            lifecycle = None
            if auto_join:
                from .registration import Registration
                lifecycle = Registration(self.settings, gateway, database, sleep, notify)
                result = lifecycle.recover()
                if result:
                    return result
            while True:
                response = gateway.call("list_tracks")
                if not isinstance(response, dict) or not isinstance(response.get("tracks"), list):
                    return "Track一覧を確認できないため監視を停止しました。"
                now = datetime.now(timezone.utc)
                for track in response["tracks"]:
                    if not is_participable_track(track, now):
                        continue
                    if auto_join:
                        if lifecycle.status(track["id"]) in {None, "detected", "failed_retryable"}:
                            notify("参加候補Trackを検知しました。")
                            result = lifecycle.join(track["id"])
                            if result:
                                return result
                            break
                    else:
                        # Notification history never controls auto-registration.
                        with database:
                            inserted = database.execute("INSERT OR IGNORE INTO seen (id) VALUES (?)", (track["id"],)).rowcount
                        if inserted:
                            notify("参加候補Trackを検知しました。")
                sleep(interval)
        except KeyboardInterrupt:
            return "監視を停止しました。"
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return "通信または検知履歴の処理を確認できないため監視を停止しました。"
        finally:
            if database is not None:
                database.close()
            if lock_fd is not None:
                os.close(lock_fd)
