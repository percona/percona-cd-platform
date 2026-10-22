"""Run the actual smoke script with isolated command and image-file boundaries.

No AWS, Docker service, JVM or Jenkins connection is exercised by these tests.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SmokeContract(unittest.TestCase):
    def run_smoke(self, **overrides):
        with tempfile.TemporaryDirectory(prefix="agent-smoke-test-") as temporary:
            work = Path(temporary)
            commands = work / "bin"
            commands.mkdir()
            for name in ("uname", "java", "sudo", "docker", "curl", "git", "aws",
                         "file", "tar", "unzip", "7za"):
                shutil.copy2(Path(__file__).with_name("smoke-command.py"), commands / name)
                (commands / name).chmod(0o755)
            log = work / "calls.json"
            log.write_text("[]")
            # Image-file availability is a separate boundary. The real bake
            # must install these files and the fresh-boot smoke checks them.
            shell_environment = work / "bash-env"
            shell_environment.write_text(
                'test() { case "${2:-}" in /licenses/*) return 0 ;; '
                '*) builtin test "$@" ;; esac; }\n'
            )
            environment = dict(os.environ,
                               PATH=str(commands) + os.pathsep + os.environ["PATH"],
                               BASH_ENV=str(shell_environment),
                               SMOKE_FIXTURE_LOG=str(log),
                               SMOKE_ARCH="x86_64",
                               SMOKE_JENKINS_URL="https://jenkins.invalid")
            environment.update(overrides)
            result = subprocess.run(["bash", str(ROOT / "smoke/verify.sh")],
                                    env=environment, text=True, capture_output=True,
                                    timeout=10, check=False)
            return result, json.loads(log.read_text())

    def test_supported_runtime_passes(self):
        result, calls = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(call[0] == "java" and "-jar" in call for call in calls))
        self.assertTrue(any(call[:3] == ["docker", "run", "--rm"] for call in calls))
        self.assertFalse(any(call[:2] == ["sudo", "docker"] for call in calls))

    def test_java_17_is_rejected(self):
        result, _ = self.run_smoke(FIXTURE_JAVA="17")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Java 21", result.stderr)

    def test_wrong_native_architecture_is_rejected(self):
        result, _ = self.run_smoke(FIXTURE_NATIVE_ARCH="aarch64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("native architecture", result.stderr)

    def test_unsupported_architecture_is_rejected(self):
        result, _ = self.run_smoke(SMOKE_ARCH="unsupported")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported", result.stderr)

    def test_worker_docker_permission_failure_is_not_hidden_by_sudo(self):
        result, _ = self.run_smoke(FIXTURE_DOCKER_EXIT="29")
        self.assertEqual(result.returncode, 29, result.stderr)

    def test_remoting_classloader_failure_is_not_ignored(self):
        result, _ = self.run_smoke(FIXTURE_REMOTING_EXIT="61")
        self.assertEqual(result.returncode, 61, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
