"""Behaviour tests for engineer-keys-sync.sh (docs/adr/0046).

The script runs against a scratch root with stubbed aws, curl, sshd and
systemctl binaries on PATH. Real ssh-keygen validates the generated keys.
Nothing here touches AWS: the fixture asserts the stubs resolve first.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "engineer-keys-sync.sh"

STUBS = {
    "aws": """#!/usr/bin/env bash
echo "aws $*" >> "$CALLS"
[[ -f "$FIX/aws_fail" ]] && exit 254
cat "$FIX/param.json"
""",
    "curl": """#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
out=""; url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  */latest/api/token) echo tok ;;
  */openssh-key) [[ -f "$FIX/imds_fail" ]] && exit 22; cat "$FIX/imds_key" ;;
  http://keys.test/*.pub)
    slug="${url##*/}"; slug="${slug%.pub}"
    [[ -f "$FIX/keys/$slug.pub" ]] || exit 22
    if [[ -n "$out" ]]; then cp "$FIX/keys/$slug.pub" "$out"; else cat "$FIX/keys/$slug.pub"; fi ;;
  *) exit 22 ;;
esac
""",
    "sshd": """#!/usr/bin/env bash
echo "sshd $*" >> "$CALLS"
case "$1" in
  -t) [[ -f "$FIX/sshd_reject" ]] && exit 1; exit 0 ;;
  -T) dropin="$SSH_CONFIG_DIR/sshd_config.d/40-engineer-keys.conf"
      if [[ -f "$FIX/sshd_wrong_path" ]]; then echo "authorizedkeysfile .ssh/authorized_keys /etc/ssh/authorized_keys.d/WRONG"
      elif [[ -f "$dropin" ]]; then tr 'A-Z' 'a-z' < "$dropin"
      else echo "authorizedkeysfile .ssh/authorized_keys"; fi ;;
esac
""",
    "systemctl": """#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
[[ -f "$FIX/reload_fail" ]] && exit 1
exit 0
""",
    "logger": """#!/usr/bin/env bash
exit 0
""",
}


def _keygen(path: Path, key_type: str = "ed25519") -> str:
    subprocess.run(
        ["ssh-keygen", "-q", "-N", "", "-t", key_type, "-C", "", "-f", str(path)],
        check=True,
    )
    return path.with_suffix(".pub").read_text().strip()


@dataclass
class Outcome:
    returncode: int
    out: str


@dataclass
class Harness:
    root: Path
    fix: Path
    calls_log: Path
    env: dict[str, str]
    keypair_line: str
    keys_file: Path
    dropin: Path
    legacy: Path
    marker: Path
    live_marker: Path

    def set_roster(self, engineers: list[str], version: int = 1, schema: int = 1) -> None:
        value = json.dumps({"schema": schema, "engineers": engineers})
        (self.fix / "param.json").write_text(
            json.dumps({"Parameter": {"Name": "/t/p", "Type": "String", "Value": value, "Version": version}})
        )

    def serve_key(self, slug: str, *lines: str) -> None:
        (self.fix / "keys" / f"{slug}.pub").write_text("".join(f"{line}\n" for line in lines))

    def run(self) -> "Outcome":
        completed = subprocess.run(
            ["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True, check=False
        )
        return Outcome(completed.returncode, completed.stdout + completed.stderr)

    def calls(self, prefix: str) -> list[str]:
        if not self.calls_log.exists():
            return []
        return [line for line in self.calls_log.read_text().splitlines() if line.startswith(prefix)]


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    root = tmp_path / "root"
    fix = tmp_path / "fix"
    stubs = tmp_path / "bin"
    keygen_dir = tmp_path / "gen"
    for directory in (root / "etc/ssh/sshd_config.d", root / "home/ec2-user/.ssh", fix / "keys", stubs, keygen_dir, tmp_path / "lock"):
        directory.mkdir(parents=True)
    for name, body in STUBS.items():
        stub = stubs / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    keypair_line = _keygen(keygen_dir / "keypair", "rsa") + " percona-jenkins"
    old_engineer_1 = _keygen(keygen_dir / "old1")
    old_engineer_2 = _keygen(keygen_dir / "old2")
    legacy = root / "home/ec2-user/.ssh/authorized_keys"
    legacy.write_text(f"{keypair_line}\n{old_engineer_1}\n{old_engineer_2}\n")
    (fix / "imds_key").write_text(f"{keypair_line}\n")
    calls = tmp_path / "calls.log"

    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FIX": str(fix),
        "CALLS": str(calls),
        "ROSTER_PARAMETER": "/t/p",
        "ROSTER_REGION": "us-east-1",
        "KEY_URL_TEMPLATE": "http://keys.test/%s.pub",
        "LOGIN_USER": "ec2-user",
        "SSH_CONFIG_DIR": str(root / "etc/ssh"),
        "LOGIN_HOME": str(root / "home/ec2-user"),
        "IMDS_BASE": "http://imds.test",
        "LOCK_FILE": str(tmp_path / "lock/sync.lock"),
    }
    # The stubs must win over any real binary, and no AWS credential may leak
    # into the run even if the aws stub were bypassed.
    assert shutil.which("aws", path=env["PATH"]) == str(stubs / "aws")
    assert shutil.which("curl", path=env["PATH"]) == str(stubs / "curl")
    assert not any(key.startswith("AWS_") for key in env)

    return Harness(
        root=root,
        fix=fix,
        calls_log=calls,
        env=env,
        keypair_line=keypair_line,
        keys_file=root / "etc/ssh/authorized_keys.d/ec2-user.engineers",
        dropin=root / "etc/ssh/sshd_config.d/40-engineer-keys.conf",
        legacy=legacy,
        marker=root / "etc/ssh/authorized_keys.d/.legacy-pruned",
        live_marker=root / "etc/ssh/authorized_keys.d/.dropin-live",
    )


def _two_engineers(harness: Harness, gen: Path) -> dict[str, list[str]]:
    alice = _keygen(gen / "alice")
    bob_1 = _keygen(gen / "bob1")
    bob_2 = _keygen(gen / "bob2", "rsa")
    harness.serve_key("alice", alice)
    harness.serve_key("bob", bob_1, "", bob_2)
    harness.set_roster(["bob", "alice"])
    return {"alice": [alice], "bob": [bob_1, bob_2]}


def test_happy_path_installs_roster_dropin_and_prunes_legacy(harness: Harness, tmp_path: Path) -> None:
    served = _two_engineers(harness, tmp_path / "gen")

    result = harness.run()

    assert result.returncode == 0, result.out
    lines = harness.keys_file.read_text().splitlines()
    assert lines == [
        f"{' '.join(served['alice'][0].split()[:2])} alice",
        f"{' '.join(served['bob'][0].split()[:2])} bob",
        f"{' '.join(served['bob'][1].split()[:2])} bob",
    ]
    assert harness.dropin.read_text().strip() == (
        f"AuthorizedKeysFile .ssh/authorized_keys {harness.root}/etc/ssh/authorized_keys.d/%u.engineers"
    )
    assert harness.calls("systemctl reload sshd") == ["systemctl reload sshd"]
    assert harness.legacy.read_text() == f"{harness.keypair_line}\n"
    backups = list(harness.legacy.parent.glob("authorized_keys.pre-engineers.*"))
    assert len(backups) == 1
    assert backups[0].read_text().count("\n") == 3
    assert harness.marker.exists()
    assert "result=ok parameter_version=1 engineers=2 keys=3" in result.out
    assert "file=changed sshd=reloaded prune=done" in result.out


def test_second_run_is_idempotent(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    harness.calls_log.unlink()

    result = harness.run()

    assert result.returncode == 0
    assert "file=unchanged sshd=kept prune=already" in result.out
    assert harness.calls("systemctl") == []
    assert len(list(harness.legacy.parent.glob("authorized_keys.pre-engineers.*"))) == 1


def test_missing_slug_keeps_last_good_file(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    before = harness.keys_file.read_bytes()
    harness.set_roster(["alice", "bob", "carol"], version=2)

    result = harness.run()

    assert result.returncode == 1
    assert "fetch failed for carol" in result.out
    assert "version 2 not applied, last good file kept" in result.out
    assert harness.keys_file.read_bytes() == before


def test_malformed_key_line_is_rejected(harness: Harness) -> None:
    harness.serve_key("dave", "<html><body>404 Not Found</body></html>")
    harness.set_roster(["dave"])

    result = harness.run()

    assert result.returncode == 1
    assert "malformed key line for dave" in result.out
    assert not harness.keys_file.exists()
    assert harness.legacy.read_text().count("\n") == 3


def test_ssm_failure_keeps_file(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    before = harness.keys_file.read_bytes()
    (harness.fix / "aws_fail").touch()

    result = harness.run()

    assert result.returncode == 1
    assert "cannot read /t/p in us-east-1" in result.out
    assert harness.keys_file.read_bytes() == before


def test_empty_roster_writes_empty_file_without_fetching(harness: Harness) -> None:
    harness.set_roster([])

    result = harness.run()

    assert result.returncode == 0, result.out
    assert harness.keys_file.exists()
    assert harness.keys_file.read_text() == ""
    assert not any("keys.test" in call for call in harness.calls("curl"))
    assert "engineers=0 keys=0" in result.out


@pytest.mark.parametrize("roster", [{"schema": 2, "engineers": ["alice"]}, {"schema": 1, "engineers": ["Alice B"]}, {"schema": 1, "engineers": "alice"}])
def test_invalid_roster_is_rejected_before_any_fetch(harness: Harness, roster: dict) -> None:
    (harness.fix / "param.json").write_text(
        json.dumps({"Parameter": {"Name": "/t/p", "Type": "String", "Value": json.dumps(roster), "Version": 3}})
    )

    result = harness.run()

    assert result.returncode == 1
    assert "not a valid schema 1 roster" in result.out
    assert harness.calls("curl") == []
    assert not harness.keys_file.exists()


def test_prune_refuses_to_leave_no_key_pair(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    legacy_without_keypair = "\n".join(harness.legacy.read_text().splitlines()[1:]) + "\n"
    harness.legacy.write_text(legacy_without_keypair)

    result = harness.run()

    assert result.returncode == 1
    assert "no key pair line found" in result.out
    assert harness.keys_file.exists(), "the roster sync itself must still land"
    assert harness.legacy.read_text() == legacy_without_keypair
    assert not harness.marker.exists()


def test_prune_blocked_when_instance_metadata_is_unavailable(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    (harness.fix / "imds_fail").touch()
    before = harness.legacy.read_text()

    result = harness.run()

    assert result.returncode == 1
    assert "instance metadata unavailable" in result.out
    assert harness.legacy.read_text() == before
    assert not harness.marker.exists()
    (harness.fix / "imds_fail").unlink()
    assert harness.run().returncode == 0
    assert harness.marker.exists()


def test_sshd_rejecting_the_dropin_removes_it_and_skips_reload(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    (harness.fix / "sshd_reject").touch()

    result = harness.run()

    assert result.returncode == 1
    assert "sshd rejected the configuration" in result.out
    assert not harness.dropin.exists()
    assert harness.calls("systemctl") == []
    assert harness.keys_file.exists()


def test_interrupted_install_revalidates_and_reloads_before_pruning(harness: Harness, tmp_path: Path) -> None:
    """A run that died between the drop-in rename and the reload leaves the file
    on disk but no live marker. The next run must not trust the file."""
    _two_engineers(harness, tmp_path / "gen")
    harness.dropin.write_text(
        f"AuthorizedKeysFile .ssh/authorized_keys {harness.root}/etc/ssh/authorized_keys.d/%u.engineers\n"
    )
    assert not harness.live_marker.exists()

    result = harness.run()

    assert result.returncode == 0, result.out
    assert harness.calls("sshd -t") == ["sshd -t"]
    assert harness.calls("systemctl reload sshd") == ["systemctl reload sshd"]
    assert harness.live_marker.exists()
    assert "sshd=reloaded prune=done" in result.out


def test_reload_failure_restores_previous_state_and_blocks_prune(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    (harness.fix / "reload_fail").touch()
    before = harness.legacy.read_text()

    result = harness.run()

    assert result.returncode == 1
    assert "sshd reload failed, previous state restored" in result.out
    assert harness.keys_file.exists(), "the roster file is published before the drop-in"
    assert not harness.dropin.exists(), "no previous drop-in, so ours is removed"
    assert not harness.live_marker.exists()
    assert not harness.marker.exists()
    assert harness.legacy.read_text() == before


def test_prune_requires_sshd_to_resolve_the_exact_key_file(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    (harness.fix / "sshd_wrong_path").touch()
    before = harness.legacy.read_text()

    result = harness.run()

    assert result.returncode == 1
    assert "sshd has not confirmed" in result.out
    assert harness.live_marker.exists(), "the reload itself succeeded"
    assert harness.legacy.read_text() == before
    assert not harness.marker.exists()


def test_failed_rename_is_reported_not_swallowed(harness: Harness, tmp_path: Path) -> None:
    """mv into a path occupied by a non-empty directory fails. The script must
    report it and exit non-zero instead of claiming the file changed."""
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    harness.keys_file.unlink()
    harness.keys_file.mkdir()
    (harness.keys_file / "occupied").write_text("x")
    harness.set_roster(["alice"], version=2)

    result = harness.run()

    assert result.returncode == 1
    assert "published file does not match staging" in result.out
    assert "not published, last good file kept" in result.out
    assert harness.keys_file.is_dir()
