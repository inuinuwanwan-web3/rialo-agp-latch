from __future__ import annotations

import os
import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mcp_command: str
    mcp_args: list[str]
    required_env: list[str]
    mcp_env: dict[str, str]
    mcp_source: str
    mcp_cwd: Path | None
    mcp_protocol_version: str
    max_usd: float
    max_questions: int
    max_guesses: int
    solver_command: str
    log_dir: Path
    monitor_interval_seconds: float = 60.0
    monitor_seen_file: Path = Path("monitor-seen.sqlite3")


_ENV_REF = re.compile(r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")


def reject_secret_argv(values, environment):
    secrets = [v for k, v in environment.items() if v and (
        k.upper() == 'AUTH' or re.search(r'TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY', k, re.I))]
    for value in values:
        if (any(secret in value for secret in secrets)
                or re.search(r'(?i)bearer\s+\S+|sk-[a-z0-9_-]+|(?:token|password|api[_-]?key)\s*=', value)):
            raise ValueError('Credential-bearing process argument blocked')


def _expand(value: str, environment: dict[str, str]) -> str:
    """Expand only $VAR / ${VAR}; no shell, command substitution, or globbing."""
    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain")
        if name not in environment:
            raise ValueError(f"MCP 起動設定に未解決の環境変数があります: {name}")
        return environment[name]
    return _ENV_REF.sub(replace, value)


def resolve_launch(settings: Settings) -> tuple[str, list[str], dict[str, str]]:
    """Prepare an MCP launch spec in memory without emitting sensitive values."""
    environment = {**os.environ, **settings.mcp_env}
    command = _expand(settings.mcp_command, environment)
    args = [_expand(arg, environment) for arg in settings.mcp_args]
    # mcp-remote expands header values itself. Keep credentials out of argv,
    # including process listings and systemctl status output.
    if Path(command).name == "mcp-remote" or any(
        arg == "mcp-remote" or arg.startswith("mcp-remote@") for arg in args
    ):
        for index in range(1, len(args)):
            if args[index - 1] == "--header" and ":" in args[index]:
                name, value = args[index].split(":", 1)
                variable = f"AGP_MCP_PRIVATE_HEADER_{index}"
                environment[variable] = value
                args[index] = name + ":${" + variable + "}"
    reject_secret_argv([command, *args], environment)
    return (
        command,
        args,
        environment,
    )


def _load_codex_agp(path: Path) -> dict[str, object]:
    """Parse only the agp-track-race table; never load another MCP server."""
    if not path.exists():
        raise ValueError(f"Codex 設定ファイルがありません: {path}")
    target = "[mcp_servers.agp-track-race]"
    section: list[str] = []
    found = False
    with path.open(encoding="utf-8") as config:
        for line in config:
            if not found:
                found = line.strip() == target
                continue
            if line.strip() == "[mcp_servers.agp-track-race.env]":
                section.append("[server.env]\n")
                continue
            if line.lstrip().startswith("["):
                break
            section.append(line)
    if not found:
        raise ValueError("Codex に agp-track-race MCP 設定がありません")
    # The synthetic table prevents unrelated config tables from entering memory.
    parsed = tomllib.loads("[server]\n" + "".join(section)).get("server", {})
    allowed = {"command", "args", "env"}
    return {key: parsed[key] for key in allowed & parsed.keys()}


def load(path: Path) -> Settings:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    base = path.parent
    mcp = raw.get("mcp", {})
    source = mcp.get("source", "direct")
    if source == "codex":
        codex_path = Path(mcp.get("codex_config", "~/.codex/config.toml")).expanduser()
        agp = _load_codex_agp(codex_path)
        command, args, mcp_env, required_env = agp.get("command", ""), agp.get("args", []), agp.get("env", {}), []
    elif source == "direct":
        command, args, mcp_env, required_env = mcp.get("command", ""), mcp.get("args", []), {}, mcp.get("required_env", [])
    else:
        raise ValueError("mcp.source は codex または direct を指定してください")
    cwd_value = mcp.get("working_directory", "")
    cwd = (base / cwd_value).resolve() if cwd_value and not Path(cwd_value).is_absolute() else (Path(cwd_value).expanduser() if cwd_value else None)
    protocol_version = mcp.get("protocol_version", "2025-03-26")
    return Settings(
        mcp_command=command,
        mcp_args=args,
        required_env=required_env,
        mcp_env=mcp_env,
        mcp_source=source,
        mcp_cwd=cwd,
        mcp_protocol_version=protocol_version,
        max_usd=float(raw.get("budget", {}).get("max_usd", 1.0)),
        max_questions=int(raw.get("limits", {}).get("max_questions_per_point", 12)),
        max_guesses=int(raw.get("limits", {}).get("max_guesses_per_point", 3)),
        solver_command=raw.get("solver", {}).get("command", ""),
        log_dir=base / raw.get("logging", {}).get("directory", "logs"),
        monitor_interval_seconds=float(raw.get("monitor", {}).get("interval_seconds", 60)),
        monitor_seen_file=base / raw.get("monitor", {}).get("seen_file", "monitor-seen.sqlite3"),
    )


def preflight(settings: Settings, *, monitor: bool = False) -> list[str]:
    missing: list[str] = []
    if not settings.mcp_command:
        missing.append("mcp.command: AGP Track Race MCP を起動するコマンド")
    missing.extend(f"環境変数 {name}" for name in settings.required_env if not os.getenv(name))
    if not isinstance(settings.mcp_env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in settings.mcp_env.items()):
        missing.append("Codex の agp-track-race env: キーと値の対応表")
    if settings.mcp_cwd and not settings.mcp_cwd.is_dir():
        missing.append("mcp.working_directory: 存在するディレクトリ")
    if settings.mcp_protocol_version not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
        missing.append("mcp.protocol_version: 2024-11-05、2025-03-26、2025-06-18 のいずれか")
    try:
        resolve_launch(settings)
    except ValueError as error:
        missing.append(str(error))
    if monitor:
        if not math.isfinite(settings.monitor_interval_seconds) or settings.monitor_interval_seconds < 60:
            missing.append("monitor.interval_seconds: 60 以上の有限の秒数")
        return missing
    if not settings.solver_command:
        missing.append("solver.command: JSON stdin/stdout を使う推論プロセス")
    if not 0 < settings.max_usd <= 1.0:
        missing.append("budget.max_usd: 0 より大きく $1.00 以下")
    if settings.max_questions < 0 or settings.max_guesses < 1:
        missing.append("limits: 質問数は 0 以上、回答回数は 1 以上")
    return missing
