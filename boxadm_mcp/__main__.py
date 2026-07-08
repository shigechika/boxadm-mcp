"""Entry point: `boxadm-mcp` (stdio server) / `boxadm-mcp auth` / `boxadm-mcp --version`."""

import asyncio
import os
import sys

from boxadm_mcp import __version__


def _auth() -> None:
    """One-time OAuth login: writes the token cache for BoxOAuthClient."""
    from boxadm_mcp.oauth import DEFAULT_REDIRECT_URI, login

    cache = login(
        os.environ["BOX_CLIENT_ID"],
        os.environ["BOX_CLIENT_SECRET"],
        redirect_uri=os.environ.get("BOX_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        token_cache=os.environ.get("BOX_TOKEN_CACHE") or None,
    )
    print(f"token cache written: {cache}")


def main() -> None:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"boxadm-mcp {__version__}")
        return
    if argv and argv[0] == "auth":
        _auth()
        return
    try:
        # Import lazily so `--version` / `auth` work without the MCP runtime.
        # The import sits inside the try so a ^C during the (slow) import chain
        # also exits cleanly, not just one delivered while the server runs.
        from boxadm_mcp.server import mcp

        mcp.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # anyio's teardown on SIGINT dumps a 20-80 line traceback. What it
        # raises out of mcp.run() is Python-version-dependent: a bare
        # KeyboardInterrupt on 3.12/3.13, but asyncio.CancelledError on 3.10
        # (asyncio.Runner.run() re-raises CancelledError instead of letting
        # KeyboardInterrupt propagate). Catch both and exit clean, same
        # convention as the sibling fleet MCP servers.
        os._exit(0)


if __name__ == "__main__":
    main()
