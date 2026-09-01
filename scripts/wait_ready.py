#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5000/livez")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    deadline = time.time() + args.timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(args.url, timeout=3) as resp:
                if 200 <= resp.status < 300:
                    sys.stdout.write("ready\n")
                    return 0
                last = str(resp.status)
        except Exception as exc:
            last = str(exc)
        time.sleep(2)
    sys.stderr.write(f"timeout waiting for {args.url}: {last}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
