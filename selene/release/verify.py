"""Release manifest identity check."""

from __future__ import annotations

import argparse
import json
import os
import sys

from selene.evidence.verify import ArtifactError, verify_release
from shared.runtime.release_identity import resolve_git_sha, resolve_image_digest


def verify_release_identity(path: str, environ: dict[str, str] | None = None) -> dict:
    env = environ or dict(os.environ)
    data = verify_release(path)
    git_sha = resolve_git_sha(env)
    image = resolve_image_digest(env)
    if git_sha not in {"unknown", ""} and git_sha != str(data.get("git_sha") or ""):
        raise ArtifactError("running git SHA does not match release.git_sha")
    digests = data.get("image_digests") or {}
    if image not in {"unknown", ""} and image not in {str(v) for v in digests.values()}:
        raise ArtifactError("running image digest not in release.image_digests")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args(argv)
    try:
        verify_release_identity(args.path)
    except (ArtifactError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"status": "fail", "error": str(exc)}) + "\n")
        return 2
    sys.stdout.write(json.dumps({"status": "ok", "path": args.path}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
