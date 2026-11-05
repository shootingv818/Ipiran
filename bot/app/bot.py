"""Main bot loop: long-poll Telegram, enforce owner-only access, route button
presses and the few text prompts, and run the background poller."""
from __future__ import annotations

import threading
import time
import traceback

from . import config, db, health, links, service, ui
from .telegram import Telegram, TelegramError

# One owner, one process: a tiny pending-input state is enough.
_pending: dict[str, object] = {}


def run() -> None:
    if not config.BOT_TOKEN or not config.OWNER_ID:
        raise SystemExit("set IPSAZ_BOT_TOKEN and IPSAZ_OWNER_ID first")
    db.init()
    tg = Telegram(config.BOT_TOKEN)
    me = tg.get_me()
    print(f"bot @{me.get('username')} up; owner={config.OWNER_ID}", flush=True)

    threading.Thread(target=_poller, daemon=True).start()
    threading.Thread(target=_health_loop, daemon=True).start()

    offset = 0
    while True:
        try:
            updates = tg.get_updates(offset, timeout=50)
        except TelegramError as exc:
            print("getUpdates:", exc, flush=True)
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                _dispatch(tg, upd)
            except Exception:  # noqa: BLE001 - never let one update kill the loop
                traceback.print_exc()


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def _owner(obj: dict) -> bool:
    frm = obj.get("from") or {}
    return frm.get("id") == config.OWNER_ID


def _dispatch(tg: Telegram, upd: dict) -> None:
    if "callback_query" in upd:
        cq = upd["callback_query"]
        if not _owner(cq):
            tg.answer_callback(cq["id"], "اجازه نداری.")
            return
        _on_callback(tg, cq)
        return

    msg = upd.get("message")
    if not msg or not _owner(msg):
        return
    text = (msg.get("text") or "").strip()
    chat = msg["chat"]["id"]

    action = _pending.pop("action", None)
    if action == "new_user":
        _do_new_user(tg, chat, text)
        return
    if action == "add_server":
        _do_add_server(tg, chat, text)
        return

    if text in ("/start", "/menu", "/home", "start"):
        _send_home(tg, chat)
    else:
        _send_home(tg, chat)


def _on_callback(tg: Telegram, cq: dict) -> None:
    data = cq.get("data", "")
    chat = cq["message"]["chat"]["id"]
    mid = cq["message"]["message_id"]
    tg.answer_callback(cq["id"])
    # Pressing any button cancels a half-started text prompt, so the next
    # thing the owner types is never mistaken for a server link or a name.
    _pending.pop("action", None)
    conn = db.connect()
    try:
        _route(tg, conn, chat, mid, data)
    except (service.ServiceError, links.LinkError) as exc:
        tg.send_message(chat, f"⚠️ {exc}", parse_mode=None)
    except Exception as exc:  # noqa: BLE001
        tg.send_message(chat, f"خطا: {exc}", parse_mode=None)
        traceback.print_exc()
    finally:
        conn.close()


def _route(tg, conn, chat, mid, data: str) -> None:
    def show(screen):
        text, kb = screen
        tg.edit_message(chat, mid, text, kb)

    if data == "home":
        return show(ui.home(service.dashboard(conn)))
    if data == "settings":
        return show(ui.settings_screen(service.dashboard(conn)))
    if data == "servers":
        return show(ui.servers_list(service.dashboard(conn)))
    if data == "strategy":
        return show(ui.strategy_screen(db.get_setting(conn, "strategy", "leastPing")))

    if data.startswith("users:"):
        page = int(data.split(":")[1])
        return show(ui.users_list(service.dashboard(conn), page))

    if data.startswith("setstrat:"):
        db.set_setting(conn, "strategy", data.split(":")[1])
        service.rebuild(conn)
        return show(ui.strategy_screen(db.get_setting(conn, "strategy")))

    if data == "user_new":
        _pending["action"] = "new_user"
        return tg.send_message(chat, "نام کاربر جدید را بفرست (مثلاً: گوشی علی):")
    if data == "server_add":
        _pending["action"] = "add_server"
        return tg.send_message(
            chat, "لینک vless سرور ایران را بفرست.\n"
                  "(از پنل سرور ایران، دکمه‌ی «کپی لینک» همان کاربر relay)")

    if data == "newkeys":
        from . import xray
        priv, pub = xray.generate_keypair()
        db.set_setting(conn, "reality_private_key", priv)
        db.set_setting(conn, "reality_public_key", pub)
        db.set_setting(conn, "reality_short_id", xray.new_short_id())
        try:
            service.rebuild(conn)
        except Exception:  # noqa: BLE001
            pass
        tg.send_message(chat, "🔑 کلید جدید ساخته شد. لینک کاربرهای قبلی باید دوباره گرفته شود.")
        return show(ui.settings_screen(service.dashboard(conn)))

    # ---- user actions ----
    if data.startswith("user:"):
        return show(_user_screen(conn, int(data.split(":")[1])))
    if data.startswith("link:"):
        uid = int(data.split(":")[1])
        link = service.user_link(conn, uid)
        row = conn.execute("SELECT label FROM users WHERE id=?", (uid,)).fetchone()
        return tg.send_message(chat, ui.link_message(row["label"], link))
    if data.startswith("toggle:"):
        uid = int(data.split(":")[1])
        cur = conn.execute("SELECT enabled FROM users WHERE id=?", (uid,)).fetchone()
        service.update_user(conn, uid, enabled=not cur["enabled"])
        return show(_user_screen(conn, uid))
    if data.startswith("extend:"):
        uid = int(data.split(":")[1])
        service.update_user(conn, uid, add_days=30)
        return show(_user_screen(conn, uid))
    if data.startswith("resetu:"):
        uid = int(data.split(":")[1])
        service.update_user(conn, uid, reset_usage=True)
        return show(_user_screen(conn, uid))
    if data.startswith("rotate:"):
        uid = int(data.split(":")[1])
        service.update_user(conn, uid, rotate_uuid=True)
        tg.send_message(chat, "🔑 شناسه عوض شد. لینک جدید را بگیر و بده.")
        return show(_user_screen(conn, uid))
    if data.startswith("del:"):
        uid = int(data.split(":")[1])
        row = conn.execute("SELECT label FROM users WHERE id=?", (uid,)).fetchone()
        kb = [[ui.btn("✅ بله، حذف", f"delok:{uid}"), ui.btn("لغو", f"user:{uid}")]]
        return tg.edit_message(chat, mid,
                               ui._mono(f"حذف «{row['label']}»؟\nاین کار برگشت ندارد."), kb)
    if data.startswith("delok:"):
        uid = int(data.split(":")[1])
        service.delete_user(conn, uid)
        return show(ui.users_list(service.dashboard(conn), 0))

    # ---- server actions ----
    if data.startswith("server:"):
        return show(_server_screen(conn, data.split(":", 1)[1]))
    if data.startswith("srv_toggle:"):
        tag = data.split(":", 1)[1]
        cur = conn.execute("SELECT enabled FROM iran_servers WHERE tag=?", (tag,)).fetchone()
        service.set_server_enabled(conn, tag, not cur["enabled"])
        return show(_server_screen(conn, tag))
    if data.startswith("srv_del:"):
        tag = data.split(":", 1)[1]
        kb = [[ui.btn("✅ بله، حذف", f"srv_delok:{tag}"), ui.btn("لغو", f"server:{tag}")]]
        return tg.edit_message(chat, mid, ui._mono(f"حذف سرور {tag}؟"), kb)
    if data.startswith("srv_delok:"):
        tag = data.split(":", 1)[1]
        service.remove_iran_server(conn, tag)
        return show(ui.servers_list(service.dashboard(conn)))


# --------------------------------------------------------------------------
# text-prompt handlers
# --------------------------------------------------------------------------

def _do_new_user(tg: Telegram, chat: int, text: str) -> None:
    conn = db.connect()
    try:
        user = service.create_user(conn, label=text)
        link = service.user_link(conn, user["id"])
        tg.send_message(chat, ui.link_message(user["label"], link))
        t, kb = _user_screen(conn, user["id"])
        tg.send_message(chat, t, kb)
    except (service.ServiceError, links.LinkError) as exc:
        tg.send_message(chat, f"⚠️ {exc}", parse_mode=None)
    finally:
        conn.close()


def _do_add_server(tg: Telegram, chat: int, text: str) -> None:
    conn = db.connect()
    try:
        srv = service.add_iran_server(conn, text)
        tg.send_message(chat, f"✅ سرور «{srv['label']}» ({srv['tag']}) اضافه و فعال شد.",
                        parse_mode=None)
        t, kb = ui.servers_list(service.dashboard(conn))
        tg.send_message(chat, t, kb)
    except (service.ServiceError, links.LinkError) as exc:
        tg.send_message(chat, f"⚠️ {exc}", parse_mode=None)
    except Exception as exc:  # noqa: BLE001
        tg.send_message(chat, f"خطا در افزودن سرور: {exc}", parse_mode=None)
    finally:
        conn.close()


def _user_screen(conn, uid: int):
    for u in service.dashboard(conn)["users"]:
        if u["id"] == uid:
            return ui.user_detail(u)
    return ui.users_list(service.dashboard(conn), 0)


def _server_screen(conn, tag: str):
    for s in service.dashboard(conn)["servers"]:
        if s["tag"] == tag:
            return ui.server_detail(s)
    return ui.servers_list(service.dashboard(conn))


def _send_home(tg: Telegram, chat: int) -> None:
    conn = db.connect()
    try:
        text, kb = ui.home(service.dashboard(conn))
        tg.send_message(chat, text, kb)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# background loops
# --------------------------------------------------------------------------

def _poller() -> None:
    while True:
        time.sleep(config.POLL_SECONDS)
        conn = db.connect()
        try:
            service.poll(conn)
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.close()


def _health_loop() -> None:
    while True:
        time.sleep(config.HEALTH_SECONDS)
        conn = db.connect()
        try:
            health.refresh(conn)
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.close()


if __name__ == "__main__":
    run()
