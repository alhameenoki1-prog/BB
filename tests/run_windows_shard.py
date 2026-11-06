"""Run the complete Windows pytest collection in bounded, isolated shards.

Windows pytest runs were interrupted after about five minutes even while
passing tests. Fresh interpreters and smaller batches make a slow test or a
runner interruption diagnosable without silently dropping any modules.
This file is not itself a pytest test.
"""

import sys
import threading
import traceback
from pathlib import Path

import pytest


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"1", "2", "3"}:
        raise SystemExit("usage: python tests/run_windows_shard.py {1|2|3}")

    root = Path(__file__).parent
    files = sorted(root.rglob("test_*.py"))
    groups = ([], [], [])
    for path in files:
        name = path.name
        if name < "test_email_finder.py":
            groups[0].append(str(path))
        elif name < "test_mail_runs.py":
            groups[1].append(str(path))
        else:
            groups[2].append(str(path))

    assert len(files) == sum(map(len, groups)), "some test modules were omitted"
    shard = int(sys.argv[1]) - 1
    selected = groups[shard]
    assert selected, "empty pytest shard"

    print(
        f"Windows shard {shard + 1}/3: {len(selected)} of {len(files)} test modules, "
        f"from {Path(selected[0]).name} to {Path(selected[-1]).name}",
        flush=True,
    )
    # A Windows test can stall inside a TestClient request without producing a
    # pytest failure. Emit thread stacks before the runner interrupts it;
    # keep the test running so a real failure is never turned into a pass.
    # Avoid faulthandler's platform-dependent timer in the Windows runner.
    # A daemon observer prints stacks but never interrupts pytest or alters its result.
    finished = threading.Event()

    def report_stacks() -> None:
        while not finished.wait(20):
            print("Windows pytest diagnostic: thread stacks after 20s", file=sys.stderr, flush=True)
            for thread_id, frame in sys._current_frames().items():
                print(f"Thread {thread_id}:", file=sys.stderr)
                traceback.print_stack(frame, file=sys.stderr)
            sys.stderr.flush()

    observer = threading.Thread(target=report_stacks, name="pytest-stack-observer", daemon=True)
    observer.start()
    try:
        return pytest.main(["-vv", "--full-trace", "--tb=long", *selected])
    finally:
        finished.set()
        observer.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
