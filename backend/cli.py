"""
Purpose: Own backend-specific CLI commands outside the SDK launch command.
Input: An argument parser or parsed command arguments.
Output: Local and bucket dashboard commands and journal import commands.
Note: A gs://, s3:// or file:// argument selects the bucket backend.
"""
from argparse import Namespace
from typing import Any


def add_commands(commands: Any) -> None:
    """
    Purpose: Register the local service and import commands.
    Input: The CLI subcommand parser.
    Output: Parsers with documented defaults and explicit data directories.
    """
    serve = commands.add_parser("serve", help="Serve a local dashboard over SQLite or a bucket")
    serve.add_argument("directory", help="Data directory, or a gs:// or s3:// bucket URI")
    serve.add_argument("--host", default="127.0.0.1", help="Loopback bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="Local HTTP port (default: 8765; 0 chooses a free port)")
    serve.add_argument("--import-journals", action="append", default=[], metavar="PATH", help="Import journals without a change to source records")
    _add_bucket_options(serve)
    importer = commands.add_parser("import", help="Import journals into a local data directory or bucket")
    importer.add_argument("directory", help="Local backend data directory, or a gs:// or s3:// bucket URI")
    importer.add_argument("journals", nargs="+", help="Journal directories or their parent directories")
    _add_bucket_options(importer)


def dispatch(args: Namespace) -> bool:
    from .bucket.config import is_bucket_uri
    is_bucket = args.command in {"serve", "import"} and is_bucket_uri(args.directory)
    if args.command == "serve" and is_bucket:
        from .bucket.server import serve_bucket
        serve_bucket(args.directory, host=args.host, port=args.port, cache_dir=args.cache_dir,
                     refresh_s=args.refresh_s, import_paths=args.import_journals)
        return True
    if args.command == "serve":
        from .local.server import serve
        serve(args.directory, host=args.host, port=args.port, import_paths=args.import_journals)
        return True
    if args.command == "import" and is_bucket:
        from .bucket.server import running_bucket_server
        from .local.server import import_journals
        with running_bucket_server(args.directory, cache_dir=args.cache_dir,
                                   refresh_s=args.refresh_s) as server:
            import_journals(server, args.journals)
        return True
    if args.command == "import":
        from .local.server import import_journals, running_server
        with running_server(args.directory, write_connection=False) as server:
            import_journals(server, args.journals)
        return True
    return False


def _add_bucket_options(parser: Any) -> None:
    parser.add_argument("--cache-dir", default=None, help="Bucket mirror cache directory (default: ~/.cache/backprop/bucket/<uri hash>)")
    parser.add_argument("--refresh-s", type=float, default=15.0, help="Seconds between bucket refreshes (default: 15)")
