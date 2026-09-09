#!/usr/bin/env python3
"""Isolated command boundary for the real fresh-boot smoke script."""
import json
import os
from pathlib import Path
import sys


command = Path(sys.argv[0]).name
args = sys.argv[1:]
log = Path(os.environ["SMOKE_FIXTURE_LOG"])
calls = json.loads(log.read_text())
calls.append([command, *args])
log.write_text(json.dumps(calls))

if command == "uname" and args == ["-m"]:
    print(os.environ.get("FIXTURE_NATIVE_ARCH", "x86_64"))
elif command == "java":
    if "-jar" in args:
        sys.exit(int(os.environ.get("FIXTURE_REMOTING_EXIT", "0")))
    print("    java.specification.version = " + os.environ.get("FIXTURE_JAVA", "21"),
          file=sys.stderr)
elif command == "sudo" and args[:1] in (["systemctl"], ["docker"]):
    # The old smoke uses sudo docker, which masks worker-user access failures.
    pass
elif command == "docker" and args[:2] == ["run", "--rm"]:
    sys.exit(int(os.environ.get("FIXTURE_DOCKER_EXIT", "0")))
elif command == "curl":
    Path(args[args.index("--output") + 1]).write_text("fixture remoting jar")
elif command in ("git", "aws", "file", "tar", "unzip", "7za"):
    raise AssertionError(f"Only command presence is expected: {command} {args}")
else:
    raise AssertionError(f"Unexpected command: {command} {args}")
