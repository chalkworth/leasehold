"""`demo`: a throwaway machine with a scripted crowd on it (`leasehold.demo`), shown in the
terminal (`top`) or the browser (`--ui`)."""

from __future__ import annotations

import signal
import sys

from leasehold.client import Client


def register(sub) -> None:
    s = sub.add_parser("demo", help="watch a made-up busy machine; touches nothing real")
    s.add_argument("--ui", action="store_true", help="show it in the browser instead of top")
    s.add_argument("--port", type=int, default=0, help="with --ui: the port (default: any)")
    s.add_argument("--no-open", action="store_true", help="with --ui: open no browser")
    s.add_argument("--seed", type=int, default=0, help="the script's seed (default 0)")
    s.set_defaults(run=_demo)


def _demo(args, ctx) -> int:
    from leasehold.demo import Demo

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)  # a kill cleans up as Ctrl-C does
    demo = Demo(seed=args.seed)
    demo.start()
    try:
        sys.stderr.write(
            f"leasehold demo: a made-up machine on {demo.socket}\n"
            f"  in another terminal: leasehold --socket {demo.socket} top\n"
        )
        if args.ui:
            return _ui(demo, args)
        from leasehold import top

        return top.run(Client(demo.socket))
    except KeyboardInterrupt:
        return 0
    finally:
        demo.stop()


def _ui(demo, args) -> int:
    from leasehold.webui import UiServer, open_browser

    try:
        server = UiServer(lambda: Client(demo.socket), port=args.port)
    except OSError as exc:
        sys.stderr.write(f"leasehold: cannot listen on 127.0.0.1:{args.port}: {exc}\n")
        return 1
    print(f"leasehold demo: {server.url}  (Ctrl-C stops it)")
    sys.stdout.flush()
    if not args.no_open:
        open_browser(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
