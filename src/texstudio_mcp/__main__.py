"""Entry point: `python -m texstudio_mcp`."""

from __future__ import annotations

from texstudio_mcp.server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
