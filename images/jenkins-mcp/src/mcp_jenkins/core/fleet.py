"""Fleet configuration: the masters this server can reach and their read-only credentials.

The server holds the Jenkins credentials (one read-only token per master), so MCP clients
never supply them. Tools select a master by NAME; only names present in this config are
reachable, which is the allowlist. The config is sourced from MCP_JENKINS_FLEET_FILE (a JSON
file, in production the ESO-mounted secret) or MCP_JENKINS_FLEET (inline JSON for local dev).
"""

import json
import os
import threading
import time
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from fastmcp.exceptions import ToolError
from loguru import logger
from pydantic import BaseModel, SecretStr

from mcp_jenkins.jenkins import Jenkins


class Master(BaseModel):
    name: str
    url: str
    username: str
    token: SecretStr
    verify_ssl: bool = True


class Fleet(BaseModel):
    masters: list[Master] = []
    timeout: int = 30

    def names(self) -> list[str]:
        return [m.name for m in self.masters]

    def get(self, name: str) -> Master | None:
        return next((m for m in self.masters if m.name == name), None)


def _load_raw() -> dict:
    file_path = os.getenv('MCP_JENKINS_FLEET_FILE')
    if file_path:
        return json.loads(Path(file_path).read_text())
    inline = os.getenv('MCP_JENKINS_FLEET')
    if inline:
        return json.loads(inline)
    return {'masters': []}


@lru_cache(maxsize=1)
def get_fleet() -> Fleet:
    fleet = Fleet.model_validate(_load_raw())
    logger.info(f'Loaded fleet config with {len(fleet.masters)} master(s): {fleet.names()}')
    return fleet


def client_for(name: str) -> Jenkins:
    """Build a Jenkins client for a configured master, injecting the server-held read-only token.

    Raises ValueError if the name is not in the fleet config (the allowlist).
    """
    fleet = get_fleet()
    master = fleet.get(name)
    if master is None:
        msg = f'Unknown master {name!r}. Configured masters: {fleet.names()}'
        raise ValueError(msg)
    return Jenkins(
        url=master.url,
        username=master.username,
        password=master.token.get_secret_value(),
        timeout=fleet.timeout,
        verify_ssl=master.verify_ssl,
    )


# Probes of the operate preflight. Each GET answers 200 only when the service identity holds the
# permission, and 403 when it does not. Any other answer leaves the master not verified.
_PREFLIGHT_CHECKS = (('script', 'RunScripts'), ('view/all/newJob', 'Create'))
# A not-verified master is probed again on an operate or manage call, at most once per this interval.
REPROBE_SECONDS = 60.0

VERIFIED = 'verified'
NOT_VERIFIED = 'not verified'
REFUSED = 'refused'
# Shown by list_masters when the gateway runs read-only and no preflight ran.
OPERATE_OFF = 'not enabled'

# True while an operate or manage tool runs. The audit middleware sets it from the tags of the called
# tool, and core.lifespan.jenkins() checks the selected master against the preflight state.
write_call: ContextVar[bool] = ContextVar('write_call', default=False)


class OperateNotVerifiedError(ToolError):
    """The operate preflight has not verified the selected master, so write calls to it are refused."""


class _OperateState:
    """The preflight result per master. The tools run in worker threads, so a lock guards it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ran = False
        self.state: dict[str, str] = {}
        self.last_probe: dict[str, float] = {}


_operate = _OperateState()


def _probe_master(name: str, timeout: int) -> tuple[str, list[str]]:
    """Probe one master. Returns its state and the permissions it holds that operate mode forbids."""
    client = client_for(name)
    violations: list[str] = []
    verified = True
    for path, perm in _PREFLIGHT_CHECKS:
        try:
            resp = client._session.get(client.endpoint_url(path), timeout=timeout, allow_redirects=False)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'operate preflight: {name} probe /{path} errored: {e}')
            verified = False
            continue
        if resp.status_code == 200:
            violations.append(f'{name}: identity holds {perm} (GET /{path} -> 200)')
        elif resp.status_code != 403:
            logger.warning(f'operate preflight: {name} probe /{path} answered {resp.status_code}, not 403')
            verified = False
    if violations:
        return REFUSED, violations
    return (VERIFIED if verified else NOT_VERIFIED), []


def write_preflight() -> list[str]:
    """Probe each master's identity for dangerous perms; return violations (empty = safe to write).

    A probe that answers 200 is a violation and refuses operate mode for the whole gateway. A master
    whose probes did not all answer 403 is marked not verified: its operate and manage calls are
    refused until a later probe passes (check_operate), while every other master keeps serving.
    """
    fleet = get_fleet()
    violations: list[str] = []
    now = time.monotonic()
    for master in fleet.masters:
        state, found = _probe_master(master.name, fleet.timeout)
        violations.extend(found)
        with _operate.lock:
            _operate.state[master.name] = state
            _operate.last_probe[master.name] = now
        logger.info(f'operate preflight: {master.name} {state}')
    with _operate.lock:
        _operate.ran = True
    return violations


def operate_status(name: str) -> str:
    """The preflight state of a master for display: verified, not verified, refused or not enabled."""
    with _operate.lock:
        if not _operate.ran:
            return OPERATE_OFF
        return _operate.state.get(name, NOT_VERIFIED)


def _refusal(name: str, state: str, wait: float) -> OperateNotVerifiedError:
    if state == REFUSED:
        reason = 'its service identity holds a permission that operate mode forbids'
        retry = 'Ask in #opensource-jenkins.'
    else:
        reason = 'the permission preflight could not verify it (unreachable or an unexpected answer)'
        retry = f'The next check runs on a call in {max(1, int(wait) + 1)} s.'
    return OperateNotVerifiedError(
        f'Operate and manage calls to master {name!r} are refused: {reason}. Read tools still work. {retry}'
    )


def check_operate(name: str) -> None:
    """Raise OperateNotVerifiedError unless the preflight verified the master.

    A not-verified master is probed again when its last probe is REPROBE_SECONDS old. The slot is
    claimed under the lock, so concurrent calls send at most one probe per interval per master.
    """
    with _operate.lock:
        state = _operate.state.get(name, NOT_VERIFIED)
        if state == VERIFIED:
            return
        now = time.monotonic()
        last = _operate.last_probe.get(name)
        wait = 0.0 if last is None else REPROBE_SECONDS - (now - last)
        if state == REFUSED or wait > 0:
            raise _refusal(name, state, wait)
        _operate.last_probe[name] = now
    state, violations = _probe_master(name, get_fleet().timeout)
    with _operate.lock:
        _operate.state[name] = state
    for v in violations:
        logger.error(f'operate preflight violation: {v}')
    logger.info(f'operate preflight: {name} {state} (probed again on a write call)')
    if state != VERIFIED:
        raise _refusal(name, state, REPROBE_SECONDS)
