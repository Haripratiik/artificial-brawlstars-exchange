"""Supervisor for the collector, so a laptop is a viable host.

The collector is already crash-safe -- its state is a SQLite file plus
append-only shards, so stopping it loses nothing and restarting resumes from
exactly where the frontier left off. What it is not is *self-starting*. Close a
laptop lid and the process suspends; hibernate or reboot and it dies. Neither
costs data, but both cost collection hours, and someone has to notice.

This wrapper removes the noticing. It restarts the collector whenever it exits,
backing off if it is failing repeatedly rather than spinning, and stopping
outright on the two failures a restart cannot fix: a missing key and a rejected
one.

    python tools/run_collector.py -- --proxy --data-dir data/raw

Pair it with a startup entry and the collector simply runs whenever the machine
is awake:

    Windows   Task Scheduler -> Create Task -> Trigger "At log on"
              Action: python.exe, arguments: tools/run_collector.py -- --proxy
    macOS     a launchd plist with RunAtLoad
    Linux     the systemd unit in docs/collector.md, which already does this

Coverage will still have holes while the machine sleeps, and those holes are
not missing-at-random -- they correlate with your timezone, and therefore with
which regions were playing. That is a real limitation of laptop collection, and
it is an argument for an always-on host eventually. It is not an argument for
delaying the start.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone

# Exit codes the collector uses for problems a restart cannot fix.
FATAL_EXIT_CODES = {
    2,  # BRAWL_API_KEY not set
    3,  # key rejected: wrong key, or allow-listing the wrong IP
}

MIN_BACKOFF = 10.0
MAX_BACKOFF = 300.0
# A run lasting at least this long counts as healthy, so a process that dies
# after hours of good work does not inherit the backoff of an earlier crash.
HEALTHY_RUNTIME = 120.0


def main(argv: list[str]) -> int:
    passthrough = argv[1:]
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    command = [sys.executable, "-m", "collectors.brawl_api", *passthrough]
    backoff = MIN_BACKOFF
    restarts = 0

    _log(f"supervising: {' '.join(command)}")

    while True:
        started = time.monotonic()
        try:
            completed = subprocess.run(command, check=False)
        except KeyboardInterrupt:
            _log("interrupted; collector state is durable, rerun to resume")
            return 0

        runtime = time.monotonic() - started
        code = completed.returncode

        if code == 0:
            _log("collector exited cleanly; not restarting")
            return 0

        if code in FATAL_EXIT_CODES:
            _log(
                f"collector exited {code} -- a configuration problem a restart "
                "cannot fix. Read the message above and rerun once corrected."
            )
            return code

        restarts += 1
        if runtime >= HEALTHY_RUNTIME:
            # It ran fine for a while, so this looks like a suspend or a network
            # drop rather than a persistent fault. Reset the penalty.
            backoff = MIN_BACKOFF

        _log(
            f"collector exited {code} after {runtime:.0f}s "
            f"(restart #{restarts}); retrying in {backoff:.0f}s"
        )
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            _log("interrupted while waiting to restart")
            return 0
        backoff = min(backoff * 2.0, MAX_BACKOFF)


def _log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} SUPERVISOR  {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
