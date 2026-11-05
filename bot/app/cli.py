"""ipsazctl — headless setup for the relay bot, used by the installer.

    ipsazctl init-keys
    ipsazctl set public_host 203.0.113.9
    ipsazctl set reality_sni www.cloudflare.com
    ipsazctl add-server 'vless://...'      # paste an Iran exit link
    ipsazctl render
    ipsazctl show
"""
from __future__ import annotations

import argparse
import json
import sys

from . import db, links, service, xray


def _die(msg: str):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_init_keys(args):
    conn = db.init()
    if db.get_setting(conn, "reality_private_key") and not args.force:
        _die("keys already exist; pass --force to replace (breaks existing links)")
    priv, pub = xray.generate_keypair()
    db.set_setting(conn, "reality_private_key", priv)
    db.set_setting(conn, "reality_public_key", pub)
    db.set_setting(conn, "reality_short_id", xray.new_short_id())
    print("public key:", pub)
    print("short id:  ", db.get_setting(conn, "reality_short_id"))


def cmd_set(args):
    allowed = set(db.DEFAULT_SETTINGS) - {"reality_private_key", "reality_public_key"}
    if args.key not in allowed:
        _die(f"settable: {', '.join(sorted(allowed))}")
    conn = db.init()
    db.set_setting(conn, args.key, args.value)
    print(f"{args.key} = {args.value}")


def cmd_show(args):
    conn = db.init()
    s = db.all_settings(conn)
    if s.get("reality_private_key"):
        s["reality_private_key"] = "<hidden>"
    print(json.dumps(s, indent=2, ensure_ascii=False))


def cmd_add_server(args):
    conn = db.init()
    srv = service.add_iran_server(conn, args.link, label=args.label)
    print(f"added {srv['tag']}: {srv['address']}:{srv['port']}")


def cmd_list_servers(args):
    conn = db.init()
    rows = conn.execute("SELECT * FROM iran_servers ORDER BY sort_order, id").fetchall()
    if not rows:
        print("no servers")
        return
    for r in rows:
        print(f"{r['tag']:<6} {r['address']}:{r['port']:<6} sni={r['sni']} "
              f"{'on' if r['enabled'] else 'off'}")


def cmd_render(args):
    conn = db.init()
    if args.stdout:
        print(json.dumps(xray.build_config(conn), indent=2, ensure_ascii=False))
    else:
        print("wrote", xray.write_config(conn))


def build_parser():
    p = argparse.ArgumentParser(prog="ipsazctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ik = sub.add_parser("init-keys"); ik.add_argument("--force", action="store_true")
    ik.set_defaults(func=cmd_init_keys)
    st = sub.add_parser("set"); st.add_argument("key"); st.add_argument("value")
    st.set_defaults(func=cmd_set)
    sub.add_parser("show").set_defaults(func=cmd_show)
    ad = sub.add_parser("add-server"); ad.add_argument("link"); ad.add_argument("--label", default=None)
    ad.set_defaults(func=cmd_add_server)
    sub.add_parser("list-servers").set_defaults(func=cmd_list_servers)
    rn = sub.add_parser("render"); rn.add_argument("--stdout", action="store_true")
    rn.set_defaults(func=cmd_render)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (service.ServiceError, links.LinkError, xray.XrayError) as exc:
        _die(str(exc))


if __name__ == "__main__":
    main()
