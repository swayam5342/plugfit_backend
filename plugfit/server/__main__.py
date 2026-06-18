import json
import sys
from pathlib import Path

from .proxy import MockProxy
from .server import MCPServer


def parse_args(argv: list[str]) -> dict:
    args = {
        "manifest_path": None,
        "base_url": "",
        "headers": {},
        "mock": False,
    }

    it = iter(argv[1:])
    for token in it:
        if token in ("--help", "-h"):
            print(__doc__)
            sys.exit(0)
        elif token == "--base-url":
            args["base_url"] = next(it, "")
        elif token == "--header":
            raw = next(it, "")
            if "=" in raw:
                k, v = raw.split("=", 1)
                args["headers"][k.strip()] = v.strip()
        elif token == "--mock":
            args["mock"] = True
        elif not token.startswith("--"):
            args["manifest_path"] = token

    return args


def main():
    args = parse_args(sys.argv)

    if not args["manifest_path"]:
        print("Error: manifest path required\n", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    path = Path(args["manifest_path"])
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in manifest: {e}", file=sys.stderr)
        sys.exit(1)

    mock = MockProxy() if args["mock"] else None

    server = MCPServer(
        manifest=manifest,
        base_url=args["base_url"],
        extra_headers=args["headers"],
        mock_proxy=mock,
    )

    server.run()


if __name__ == "__main__":
    main()
