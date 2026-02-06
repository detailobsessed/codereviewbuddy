"""Main entry point for codereviewbuddy."""

from codereviewbuddy.server import check_prerequisites, mcp


def main() -> None:
    """Run the codereviewbuddy MCP server."""
    check_prerequisites()
    mcp.run()


if __name__ == "__main__":
    main()
