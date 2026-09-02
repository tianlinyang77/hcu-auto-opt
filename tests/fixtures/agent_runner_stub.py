from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _write_usage(total_tokens: int) -> None:
    path = Path(os.environ["HCUOPT_USAGE_PATH"])
    path.write_text(
        json.dumps(
            {
                "schema_version": "hcuopt-agent-usage-v1",
                "total_tokens": total_tokens,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    parser.add_argument("values", nargs="*")
    parser.add_argument("--tokens", type=int, default=7)
    parser.add_argument("--bytes", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--marker")
    parser.add_argument("--ready")
    parser.add_argument("--environment-name")
    parser.add_argument("--input-path")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.mode == "missing-usage":
        sys.stdout.buffer.write(b'{"proposal":"missing usage"}')
        return 0
    if args.mode == "malformed-usage":
        Path(os.environ["HCUOPT_USAGE_PATH"]).write_text("not-json", encoding="utf-8")
        sys.stdout.buffer.write(b'{"proposal":"malformed usage"}')
        return 0

    _write_usage(args.tokens)
    if args.mode == "success":
        sys.stdout.buffer.write(b'{"proposal":"safe"}')
        return 0
    if args.mode == "argv":
        sys.stdout.write(json.dumps(args.values))
        return 0
    if args.mode == "environment":
        sys.stdout.write(os.environ.get(args.environment_name or "", "<missing>"))
        return 0
    if args.mode == "readonly-input":
        path = Path(os.environ["HCUOPT_INPUT_ROOT"]) / str(args.input_path)
        original = path.read_bytes()
        writable = True
        try:
            path.write_bytes(b"changed")
        except OSError:
            writable = False
        sys.stdout.write(json.dumps({"content": original.decode("utf-8"), "writable": writable}))
        return 0
    if args.mode == "nonzero":
        sys.stderr.write("generator failed")
        return 17
    if args.mode == "overflow-stdout":
        sys.stdout.buffer.write(b"x" * args.bytes)
        sys.stdout.buffer.flush()
        time.sleep(args.seconds)
        return 0
    if args.mode == "overflow-stderr":
        sys.stderr.buffer.write(b"y" * args.bytes)
        sys.stderr.buffer.flush()
        time.sleep(args.seconds)
        return 0
    if args.mode == "secret-output":
        sys.stderr.write("token=super-secret-value")
        sys.stdout.buffer.write(b'{"proposal":"safe"}')
        return 0
    if args.mode in {"spawn-child", "spawn-background"}:
        child = subprocess.Popen(
            (sys.executable, "-c", f"import time; time.sleep({args.seconds})"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
            shell=False,
        )
        Path(str(args.marker)).write_text(str(child.pid), encoding="ascii")
        Path(str(args.ready)).write_text("ready", encoding="ascii")
        if args.mode == "spawn-background":
            sys.stdout.buffer.write(b'{"proposal":"background child cleaned"}')
            return 0
        time.sleep(args.seconds)
        return 0
    raise ValueError(f"unsupported mode: {args.mode}")


if __name__ == "__main__":
    raise SystemExit(main())
