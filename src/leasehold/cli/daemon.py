"""`serve`, `init`, `doctor` and `observe`: running a daemon, configuring one, and checking the
setup."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from leasehold import discovery, paths
from leasehold.facts import probe


def register(sub) -> None:
    s = sub.add_parser(
        "serve",
        help="run a daemon: your own, or --system for the machine's",
        description="Run a daemon. It serves on --socket, else $LEASEHOLD_SOCKET, else the "
        "config's `socket`, else its mode's socket.",
    )
    _mode_options(s)
    s.add_argument("--verbose", action="store_true", help="log every request")
    s.set_defaults(run=_serve)

    s = sub.add_parser("init", help="write a commented config sized for this machine")
    _mode_options(s)
    s.add_argument("--print", action="store_true", help="print it instead of writing it")
    s.add_argument("--force", action="store_true", help="replace a config that exists")
    s.set_defaults(run=_init)

    s = sub.add_parser("doctor", help="check this machine and this user's setup")
    s.add_argument("--json", action="store_true")
    s.set_defaults(run=_doctor)

    s = sub.add_parser(
        "observe",
        help="what the config's [[observe]] rules match right now",
        description="What the config's [[observe]] rules match right now. "
        "Experimental: may change before 1.0.",
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(run=_observe)


def _mode_options(s) -> None:
    group = s.add_mutually_exclusive_group()
    group.add_argument(
        "--system",
        dest="mode",
        action="store_const",
        const="system",
        help="the machine's one daemon, run by systemd as the leasehold user",
    )
    group.add_argument(
        "--user",
        dest="mode",
        action="store_const",
        const="user",
        help="a daemon of your own, no root needed (the default)",
    )


def _serve(args, ctx) -> int:
    from leasehold import machine
    from leasehold.server import serve

    try:
        # Only the mode's own config: a user daemon never picks up the machine's.
        path = paths.find_config(ctx.config, ctx.env, args.mode or "user")
        if path is None:
            slots = os.cpu_count() or 1
            config = machine.MachineConfig.defaults(slots=slots)
            sys.stderr.write(
                f"leasehold: no config, so {slots} slots (one per CPU) and the default pools; "
                "`leasehold init` writes one to edit\n"
            )
        else:
            config = machine.load(path)
        named = ctx.socket or ctx.env.get(discovery.ENV_SOCKET)
        if named:  # --socket, then $LEASEHOLD_SOCKET, over the config's
            config = config.model_copy(update={"socket": Path(named)})
        config = machine.resolve(config, args.mode, ctx.env)
        if config.mode == "user":
            # A user's own state directory; the system one is systemd's StateDirectory.
            Path(config.ledger_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        return serve(config, verbose=args.verbose)
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"leasehold: cannot serve: {exc}\n")
        return 1


def _init(args, ctx) -> int:
    mode = args.mode or "user"
    from leasehold.template import sized

    text = sized(mode, probe())
    if args.print:
        sys.stdout.write(text)
        return 0
    target = Path(ctx.config) if ctx.config else paths.config_candidates(None, ctx.env, mode)[0]
    if target.is_symlink():
        sys.stderr.write(f"leasehold: {target} is a symlink; write the file it points to\n")
        return 1
    if os.path.lexists(target) and not args.force:
        sys.stderr.write(f"leasehold: {target} exists; --force replaces it, --print shows it\n")
        return 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Never through a symlink, and never over a file made since the check above unless
        # --force: O_EXCL without it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if args.force else os.O_EXCL)
        with os.fdopen(os.open(target, flags, 0o644), "w") as handle:
            handle.write(text)
    except OSError as exc:
        hint = " (the system config needs root: try sudo)" if mode == "system" else ""
        sys.stderr.write(f"leasehold: cannot write {target}: {exc}{hint}\n")
        return 1
    print(
        f"wrote {target}; start the daemon with `leasehold serve{' --system' * (mode == 'system')}`"
    )
    return 0


def _observe(args, ctx) -> int:
    from leasehold import machine, observe, proctable

    try:
        path = paths.find_config(ctx.config, ctx.env)
        config = machine.load(path) if path else None
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"leasehold: {exc}\n")
        return 1
    rules = config.observe if config else []
    procs = proctable.scan()
    uids = observe.uid_table(rules)
    leased = observe.under_leasehold(procs)
    found = {
        rule.name: [
            {"pid": p.pid, "uid": p.uid, "command": p.cmdline or p.comm}
            for p in observe.roots(rule, procs, uids, leased)
        ]
        for rule in rules
    }
    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    if not rules:
        print(f"no [[observe]] rules in {path or 'any config'}")
    for rule in rules:
        matches = found[rule.name]
        print(f"{rule.name}: weight {rule.weight} each, {len(matches)} running now")
        for m in matches:
            print(f"  pid {m['pid']:<8} uid {m['uid']:<6} {m['command'][:100]}")
    return 0


_MARK = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}


def _doctor(args, ctx) -> int:
    from leasehold import doctor

    checks = doctor.run(socket=ctx.socket, config=ctx.config, env=ctx.env)
    if args.json:
        print(json.dumps([c.to_dict() for c in checks], indent=2))
    else:
        for c in checks:
            print(f"{_MARK[c.level]}  {c.name:<9} {c.detail}")
            if c.fix and c.level != "ok":
                print(f"{'':15} fix: {c.fix}")
    return 1 if any(c.level == "fail" for c in checks) else 0
