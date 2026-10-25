"""
Purpose: Own backend-specific CLI commands outside the SDK launch command.
Input: An argument parser or parsed command arguments.
Output: Local service and journal import commands.
"""
from argparse import Namespace
from typing import Any


def add_commands(commands: Any) -> None:
    """
    Purpose: Register the local service and import commands.
    Input: The CLI subcommand parser.
    Output: Parsers with documented defaults and explicit data directories.
    """
    serve = commands.add_parser("serve", help="Serve a local dashboard and SQLite backend")
    serve.add_argument("directory", help="Directory for the database and artifacts")
    serve.add_argument("--host", default="127.0.0.1", help="Loopback bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="Local HTTP port (default: 8765; 0 chooses a free port)")
    serve.add_argument("--import-journals", action="append", default=[], metavar="PATH", help="Import journals without a change to source records")
    importer = commands.add_parser("import", help="Import journals into a local data directory")
    importer.add_argument("directory", help="Local backend data directory")
    importer.add_argument("journals", nargs="+", help="Journal directories or their parent directories")


def dispatch(args: Namespace) -> bool:
    if args.command == "serve":
        from .local.server import serve
        serve(args.directory, host=args.host, port=args.port, import_paths=args.import_journals)
        return True
    if args.command == "import":
        from .local.server import import_journals, running_server
        with running_server(args.directory, write_connection=False) as server:
            import_journals(server, args.journals)
        return True
    return False
