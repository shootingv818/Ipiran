"""ipiranctl — command-line administration, used by the install script and
for headless management.

    ipiranctl init-keys
    ipiranctl set public_host 1.2.3.4
    ipiranctl set reality_sni www.varzesh3.com
    ipiranctl render
    ipiranctl show
"""
from __future__ import annotations

import argparse
import json
import sys

from . import db, service, xray


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_init_keys(args):
    conn = db.init()
    if db.get_setting(conn, "reality_private_key") and not args.force:
        _die("keys already exist; pass --force to replace "
             "(every existing client link stops working)")
    priv, pub = xray.generate_keypair()
    db.set_setting(conn, "reality_private_key", priv)
    db.set_setting(conn, "reality_public_key", pub)
    db.set_setting(conn, "reality_short_id", xray.new_short_id())
    print("public key:", pub)
    print("short id:  ", db.get_setting(conn, "reality_short_id"))


def cmd_set(args):
    allowed = set(db.DEFAULT_SETTINGS) - {"reality_private_key", "reality_public_key"}
    if args.key not in allowed:
        _die(f"unknown or protected key. settable: {', '.join(sorted(allowed))}")
    conn = db.init()
    db.set_setting(conn, args.key, args.value)
    print(f"{args.key} = {args.value}")


def cmd_show(args):
    conn = db.init()
    s = db.all_settings(conn)
    if s.get("reality_private_key"):
        s["reality_private_key"] = "<hidden>"
    print(json.dumps(s, indent=2, ensure_ascii=False))


def cmd_render(args):
    conn = db.init()
    if args.stdout:
        print(json.dumps(xray.build_config(conn), indent=2, ensure_ascii=False))
    else:
        print("wrote", xray.write_config(conn))


def cmd_poll(args):
    conn = db.init()
    print(json.dumps(service.poll(conn), ensure_ascii=False))


def cmd_url(args):
    """Print the real panel URL, read from the database rather than guessed,
    so it stays right after the secret path is rotated."""
    from . import config
    conn = db.init()
    secret = db.get_setting(conn, "secret_path")
    host = db.get_setting(conn, "public_host") or "<server-ip>"
    scheme = "https" if config.TLS_CERT and config.TLS_KEY else "http"
    port = "" if config.PORT in (80, 443) else f":{config.PORT}"
    print(f"{scheme}://{host}{port}/{secret}/")


def cmd_rotate(args):
    import secrets as _secrets
    conn = db.init()
    new = _secrets.token_urlsafe(16)
    db.set_setting(conn, "secret_path", new)
    print("new secret path:", new)
    print("restart the panel for it to take effect: systemctl restart ipiran-panel")


def cmd_add(args):
    conn = db.init()
    c = service.create_client(conn, args.label, args.quota_gb, args.expire_days)
    print("created:", c["email"])
    print(service.client_share_link(conn, c["id"]))


def build_parser():
    p = argparse.ArgumentParser(prog="ipiranctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    ik = sub.add_parser("init-keys", help="generate REALITY key pair")
    ik.add_argument("--force", action="store_true")
    ik.set_defaults(func=cmd_init_keys)

    st = sub.add_parser("set", help="change a setting")
    st.add_argument("key")
    st.add_argument("value")
    st.set_defaults(func=cmd_set)

    sub.add_parser("show", help="print settings").set_defaults(func=cmd_show)

    rn = sub.add_parser("render", help="regenerate + validate xray config")
    rn.add_argument("--stdout", action="store_true")
    rn.set_defaults(func=cmd_render)

    sub.add_parser("poll", help="drain counters once").set_defaults(func=cmd_poll)
    sub.add_parser("url", help="print the panel URL").set_defaults(func=cmd_url)
    sub.add_parser("rotate-secret", help="change the panel's secret path") \
       .set_defaults(func=cmd_rotate)

    ad = sub.add_parser("add-client", help="create a client")
    ad.add_argument("label")
    ad.add_argument("--quota-gb", type=float, default=None)
    ad.add_argument("--expire-days", type=int, default=None)
    ad.set_defaults(func=cmd_add)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (service.ServiceError, xray.XrayError) as exc:
        _die(str(exc))


if __name__ == "__main__":
    main()
