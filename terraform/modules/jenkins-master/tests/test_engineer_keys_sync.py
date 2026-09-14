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
out=""; url=""; want_code=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) want_code=1; shift 2 ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  */latest/api/token) echo tok ;;
  */openssh-key) [[ -f "$FIX/imds_fail" ]] && exit 22; cat "$FIX/imds_key" ;;
  http://keys.test/*.pub)
    slug="${url##*/}"; slug="${slug%.pub}"
    if [[ -f "$FIX/keys/$slug.500" ]]; then
      [[ $want_code -eq 1 ]] && printf 500; exit 0
    fi
    if [[ ! -f "$FIX/keys/$slug.pub" ]]; then
      [[ $want_code -eq 1 ]] && { printf 404; exit 0; }
      exit 22
    fi
    if [[ -n "$out" ]]; then cp "$FIX/keys/$slug.pub" "$out"; else cat "$FIX/keys/$slug.pub"; fi
    [[ $want_code -eq 1 ]] && printf 200 ;;
  *) exit 22 ;;
esac
exit 0
""",
    "sshd": """#!/usr/bin/env bash
echo "sshd $*" >> "$CALLS"
case "$1" in
  -t) [[ -f "$FIX/sshd_reject" ]] && exit 1; exit 0 ;;
  -T) dropin="$SSH_CONFIG_DIR/sshd_config.d/40-engineer-keys.conf"
      if [[ -f "$FIX/sshd_wrong_path" ]]; then echo "authorizedkeysfile .ssh/authorized_keys /etc/ssh/authorized_keys.d/WRONG"
      elif [[ -f "$dropin" ]]; then awk '{ $1 = tolower($1); print }' "$dropin"
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
    legacy_keys: list[str]
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
        "METRICS_DIR": str(tmp_path / "textfile"),
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
        legacy_keys=[old_engineer_1, old_engineer_2],
        keys_file=root / "etc/ssh/authorized_keys.d/ec2-user.engineers",
        dropin=root / "etc/ssh/sshd_config.d/40-engineer-keys.conf",
        legacy=legacy,
        marker=root / "etc/ssh/authorized_keys.d/.legacy-pruned",
        live_marker=root / "etc/ssh/authorized_keys.d/.dropin-live",
    )


def _two_engineers(harness: Harness, gen: Path) -> dict[str, list[str]]:
    """The legacy file holds the same keys the roster serves, as the real
    fleet did before the prune: alice's key and bob's first key were installed
    at boot, bob's second key is new."""
    alice = harness.legacy_keys[0]
    bob_1 = harness.legacy_keys[1]
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
    assert "result=ok parameter_version=1 applied_version=1 engineers=2 keys=3" in result.out
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


def test_feed_404_drops_the_slug_and_stays_green(harness: Harness, tmp_path: Path) -> None:
    """A 404 is IT's revocation: no keys for that slug, run green, slug reported."""
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    before = harness.keys_file.read_bytes()
    harness.set_roster(["alice", "bob", "carol"], version=2)

    result = harness.run()

    assert result.returncode == 0, result.out
    assert "feed has no key for carol (404)" in result.out
    assert "applied_version=2" in result.out and "missing_slugs=carol" in result.out
    assert harness.keys_file.read_bytes() == before
    metrics = _metrics(harness)
    assert metrics["engineer_keys_sync_slugs_missing"] == "1"
    assert metrics["engineer_keys_sync_degraded_slugs"] == "0"
    assert metrics["engineer_keys_sync_roster_version"] == "2"


def test_transient_feed_failure_degrades_the_run(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    before = harness.keys_file.read_bytes()
    (harness.fix / "keys" / "carol.500").touch()
    harness.set_roster(["alice", "bob", "carol"], version=2)

    result = harness.run()

    assert result.returncode == 1, "a degraded run exits 1 so the alert fires"
    assert "fetch failed for carol (http 500)" in result.out
    assert "degraded: carol keeps its 0 previous key(s)" in result.out
    assert "result=degraded parameter_version=2" in result.out and "degraded_slugs=carol" in result.out
    assert harness.keys_file.read_bytes() == before, "alice and bob converge, carol contributes nothing"
    metrics = _metrics(harness)
    assert metrics["engineer_keys_sync_last_run_success"] == "0"
    assert metrics["engineer_keys_sync_degraded_slugs"] == "1"
    assert metrics["engineer_keys_sync_roster_version"] == "1", "the last fully applied version is kept"


def test_every_slug_404_changes_nothing(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    before = harness.keys_file.read_bytes()
    (harness.fix / "keys" / "alice.pub").unlink()
    (harness.fix / "keys" / "bob.pub").unlink()
    harness.set_roster(["alice", "bob"], version=2)

    result = harness.run()

    assert result.returncode == 1
    assert "answered 404 for every slug, last good file kept" in result.out
    assert harness.keys_file.read_bytes() == before


def test_malformed_key_line_is_rejected(harness: Harness) -> None:
    harness.serve_key("dave", "<html><body>404 Not Found</body></html>")
    harness.set_roster(["dave"])

    result = harness.run()

    assert result.returncode == 1
    assert "malformed key line for dave" in result.out
    assert "degraded: dave keeps its 0 previous key(s)" in result.out
    assert harness.keys_file.read_text() == "", "nothing valid to install, the file is published empty"
    assert harness.legacy.read_text().count("\n") == 3, "a degraded run never prunes the legacy keys"
    assert not harness.marker.exists()
    assert "prune=deferred" in result.out


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
    """Revoke-all on a master whose legacy file was already pruned (the whole
    fleet today): the roster file empties, nothing is fetched, the run is green."""
    harness.marker.parent.mkdir(parents=True)
    harness.marker.write_text("2026-09-14T00:00:00Z\n")
    harness.set_roster([])

    result = harness.run()

    assert result.returncode == 0, result.out
    assert harness.keys_file.exists()
    assert harness.keys_file.read_text() == ""
    assert not any("keys.test" in call for call in harness.calls("curl"))
    assert "engineers=0 keys=0" in result.out


def test_empty_roster_on_an_unpruned_master_empties_the_roster_file_but_keeps_legacy(harness: Harness) -> None:
    """Revoke-all before the one-time prune ran: the roster file empties (so the
    roster path grants nothing) but the legacy keys are not the roster's to
    remove, so the prune blocks, names them, and the run stays red until an
    operator decides."""
    harness.set_roster([])
    before = harness.legacy.read_bytes()

    result = harness.run()

    assert result.returncode == 1
    assert harness.keys_file.read_text() == ""
    assert harness.legacy.read_bytes() == before
    assert "prune blocked" in result.out and "holds a key the roster does not serve" in result.out
    assert _metrics(harness)["engineer_keys_sync_legacy_pruned"] == "0"


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
    """mv -f onto a path that is a directory moves the file inside it, so the
    published path never becomes a file. The post-rename sha check must catch
    that and the script must exit non-zero instead of claiming the file
    changed."""
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


def _metrics(harness: Harness) -> dict[str, str]:
    text = (Path(harness.env["METRICS_DIR"]) / "engineer_keys_sync.prom").read_text()
    return dict(line.split(" ", 1) for line in text.splitlines() if line and not line.startswith("#"))


def test_metrics_report_success_then_failure_and_keep_last_success(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    after_success = _metrics(harness)
    assert after_success["engineer_keys_sync_last_run_success"] == "1"
    assert after_success["engineer_keys_sync_roster_version"] == "1"
    assert after_success["engineer_keys_sync_keys"] == "3"
    last_success = after_success["engineer_keys_sync_last_success_timestamp_seconds"]
    assert last_success == after_success["engineer_keys_sync_last_run_timestamp_seconds"]

    (harness.fix / "aws_fail").touch()
    assert harness.run().returncode == 1
    after_failure = _metrics(harness)
    assert after_failure["engineer_keys_sync_last_run_success"] == "0"
    assert after_failure["engineer_keys_sync_last_success_timestamp_seconds"] == last_success
    assert int(after_failure["engineer_keys_sync_last_run_timestamp_seconds"]) >= int(last_success)
    assert after_failure["engineer_keys_sync_roster_version"] == "1", "the last fully applied version survives the failed read"
    assert after_failure["engineer_keys_sync_degraded_slugs"] == "0"
    assert after_success["engineer_keys_sync_keyset_info{keyset_sha256=\"" + __import__("hashlib").sha256(harness.keys_file.read_bytes()).hexdigest() + "\"}"] == "1"


def test_roster_removal_lands_even_when_another_slug_fetch_fails(harness: Harness, tmp_path: Path) -> None:
    """The revocation contract: removing bob must not wait for alice's feed."""
    served = _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    (harness.fix / "keys" / "alice.500").touch()
    harness.set_roster(["alice"], version=2)

    result = harness.run()

    assert result.returncode == 1
    assert "degraded: alice keeps its 1 previous key(s)" in result.out
    lines = harness.keys_file.read_text().splitlines()
    assert lines == [f"{' '.join(served['alice'][0].split()[:2])} alice"], "bob is gone, alice's previous key is carried"
    assert "degraded_slugs=alice" in result.out
    metrics = _metrics(harness)
    assert metrics["engineer_keys_sync_keys"] == "1"
    assert metrics["engineer_keys_sync_degraded_slugs"] == "1"
    assert metrics["engineer_keys_sync_roster_version"] == "1"

    (harness.fix / "keys" / "alice.500").unlink()
    recovered = harness.run()
    assert recovered.returncode == 0
    assert "applied_version=2" in recovered.out
    assert _metrics(harness)["engineer_keys_sync_roster_version"] == "2"
    assert _metrics(harness)["engineer_keys_sync_degraded_slugs"] == "0"


def test_unreadable_roster_changes_nothing_and_reports(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    before = harness.keys_file.read_bytes()
    harness.set_roster(["alice"], schema=2, version=3)

    result = harness.run()

    assert result.returncode == 1
    assert "not a valid schema 1 roster" in result.out
    assert harness.keys_file.read_bytes() == before, "bob is not removed on an unparseable roster"


def test_prune_with_nothing_to_remove_completes_without_a_backup(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    harness.legacy.write_text(f"{harness.keypair_line}\n")

    result = harness.run()

    assert result.returncode == 0, result.out
    assert "prune=nothing" in result.out
    assert harness.marker.exists()
    assert list(harness.legacy.parent.glob("authorized_keys.pre-engineers.*")) == []
    assert _metrics(harness)["engineer_keys_sync_legacy_pruned"] == "1"


def test_missing_legacy_file_counts_as_pruned(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    harness.legacy.unlink()

    result = harness.run()

    assert result.returncode == 0, result.out
    assert "prune=nothing" in result.out
    assert harness.marker.exists()


def test_multi_key_slug_failing_on_its_second_key_carries_cleanly(harness: Harness, tmp_path: Path) -> None:
    """bob's first key validates and his second does not. Nothing of bob's new
    feed may reach the staging file, so the carry path keeps exactly his two
    previous keys and the published file stays byte-identical."""
    served = _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    before = harness.keys_file.read_bytes()
    harness.serve_key("bob", served["bob"][0], "ssh-ed25519 not-base64!!! bob")

    result = harness.run()

    assert result.returncode == 1
    assert "malformed key line for bob" in result.out
    assert "degraded: bob keeps its 2 previous key(s)" in result.out
    assert "staging holds" not in result.out, "no partial append poisoned the count"
    assert harness.keys_file.read_bytes() == before
    assert not list(Path(tmp_path / "root/etc/ssh/authorized_keys.d").glob("tmp.*")), "no staging file left behind"


def test_lock_held_by_another_run_exits_zero_and_touches_nothing(harness: Harness, tmp_path: Path) -> None:
    import fcntl

    _two_engineers(harness, tmp_path / "gen")
    harness.env["LOCK_WAIT"] = "1"
    with open(harness.env["LOCK_FILE"], "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = harness.run()

    assert result.returncode == 0
    assert "another sync holds" in result.out
    assert not harness.keys_file.exists()
    assert not (Path(harness.env["METRICS_DIR"]) / "engineer_keys_sync.prom").exists(), "the holder reports, not this run"
    assert harness.calls("aws") == []


def test_unchanged_content_still_repairs_the_file_mode(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    harness.keys_file.chmod(0o664)

    result = harness.run()

    assert result.returncode == 0
    assert "file=unchanged" in result.out
    assert stat.S_IMODE(harness.keys_file.stat().st_mode) == 0o644


def test_slug_with_trailing_newline_is_rejected(harness: Harness, tmp_path: Path) -> None:
    _two_engineers(harness, tmp_path / "gen")
    harness.set_roster(["alice\n", "bob"])

    result = harness.run()

    assert result.returncode == 1
    assert "not a valid schema 1 roster" in result.out
    assert harness.calls("curl") == []


def test_directory_and_file_modes_match_the_package_layout(harness: Harness, tmp_path: Path) -> None:
    """sshd_config.d ships as 0700 and a previous version of this script widened
    it. The run must put it back, keep the keys directory traversable for the
    login user, and give the drop-in the 0600 the packaged drop-ins have."""
    _two_engineers(harness, tmp_path / "gen")
    dropin_dir = harness.dropin.parent
    dropin_dir.chmod(0o755)

    assert harness.run().returncode == 0

    assert stat.S_IMODE(dropin_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(harness.dropin.stat().st_mode) == 0o600
    assert stat.S_IMODE(harness.keys_file.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(harness.keys_file.stat().st_mode) == 0o644


def test_prune_refuses_to_remove_a_key_the_roster_does_not_serve(harness: Harness, tmp_path: Path) -> None:
    """The prune only ever removes keys the roster file now serves. A stranger
    key in the legacy file blocks it, is named by fingerprint, and the roster
    file is live regardless. Removing the line by hand unblocks the prune."""
    _two_engineers(harness, tmp_path / "gen")
    stranger = _keygen(tmp_path / "gen" / "stranger") + " carol@laptop"
    with harness.legacy.open("a") as legacy:
        legacy.write(f"{stranger}\n")
    before = harness.legacy.read_bytes()

    result = harness.run()

    assert result.returncode == 1
    assert "prune blocked" in result.out and "SHA256:" in result.out and "carol@laptop" in result.out
    assert harness.legacy.read_bytes() == before, "nothing was removed"
    assert not harness.marker.exists()
    assert harness.keys_file.exists() and harness.live_marker.exists(), "the roster is live even though the prune is blocked"
    assert _metrics(harness)["engineer_keys_sync_legacy_pruned"] == "0"

    harness.legacy.write_text("".join(f"{line}\n" for line in before.decode().splitlines() if "carol@laptop" not in line))
    result = harness.run()

    assert result.returncode == 0, result.out
    assert "prune=done" in result.out
    assert "prune removes SHA256:" in result.out
    assert harness.marker.exists()


def test_kept_dropin_gets_its_mode_repaired_without_a_reload(harness: Harness, tmp_path: Path) -> None:
    """A drop-in an older run installed as 0644 converges to 0600 on the next
    run with identical content, and sshd is not reloaded for it."""
    _two_engineers(harness, tmp_path / "gen")
    assert harness.run().returncode == 0
    harness.dropin.chmod(0o644)
    reloads_before = len(harness.calls("systemctl reload"))

    result = harness.run()

    assert result.returncode == 0, result.out
    assert "sshd=kept" in result.out
    assert stat.S_IMODE(harness.dropin.stat().st_mode) == 0o600
    assert len(harness.calls("systemctl reload")) == reloads_before
