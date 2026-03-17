#!/usr/bin/env python3

import os
import sys
import time


def main() -> None:
    unit_name = os.environ.get("TEST_UNIT_NAME", "diff-editor-terminal-failure-test.service")
    delay_seconds = float(os.environ.get("FAIL_DELAY_SECONDS", "0.15"))

    sys.stderr.write(f"{unit_name}: starting intentional failure payload\n")
    sys.stderr.flush()
    time.sleep(delay_seconds)

    raise RuntimeError(
        "Intentional failure for diff-editor-terminal task manager testing"
    )


if __name__ == "__main__":
    main()
