from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from .agent import RaceAgent
from .audit import AuditLog
from .config import load, preflight
from .gateway import McpGateway
from .monitor import TrackMonitor


def main() -> int:
    parser = argparse.ArgumentParser(description="AGP Track Race Agent")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--execute", action="store_true", help="実MCPを呼ぶ。省略時は安全な設定確認のみ")
    parser.add_argument("--dry-run", action="store_true", help="設定確認のみ")
    parser.add_argument("--track-id")
    parser.add_argument("--watch", action="store_true", help="list_tracks のみを定期監視（自動参加なし）")
    parser.add_argument("--auto-join", action="store_true", help="--watch と併用し、新規候補に一度だけ自動参加")
    args = parser.parse_args()
    if args.auto_join and not args.watch:
        parser.error("--auto-join には --watch が必要です")
    if args.watch and args.track_id:
        parser.error("--watch と --track-id は併用できません")
    if not args.config.exists():
        print(f"設定ファイルがありません: {args.config}（config.example.toml をコピーしてください）")
        return 2
    try:
        settings = load(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print("設定を検証できません。設定ファイルの形式を確認してください。")
        return 2
    missing = preflight(settings, monitor=args.watch)
    if args.auto_join:
        missing.extend(preflight(settings))
    if missing:
        print("実行前に必要な設定・情報:")
        print("\n".join(f"- {item}" for item in missing))
        return 2
    if not args.execute or args.dry_run:
        print("設定は有効です。ドライランのため MCP ツールは実行しません。")
        return 0
    if args.watch:
        gateway = None
        try:
            gateway = McpGateway(settings, allow_writes=args.auto_join)
            print(TrackMonitor(settings, gateway).run(auto_join=True) if args.auto_join
                  else TrackMonitor(settings, gateway).run())
        except (OSError, RuntimeError, ValueError):
            print("MCP接続を確認できないため監視を停止しました。")
        finally:
            if gateway is not None:
                gateway.close()
        return 0
    audit = AuditLog(settings.log_dir)
    gateway = McpGateway(settings, allow_writes=True)
    try:
        print(RaceAgent(settings, gateway, audit).run(args.track_id))
        print(f"監査ログ: {audit.path}")
    finally:
        gateway.close()
    return 0
