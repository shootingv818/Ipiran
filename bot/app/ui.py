"""Message formatting and keyboards for Telegram.

Everything is laid out inside a monospace block with dashed dividers so it
reads like a tidy little dashboard in the chat. Numbers are shown in Persian
digits; bytes and durations are humanised.
"""
from __future__ import annotations

from typing import Any

RULE = "-" * 30
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(value: Any) -> str:
    return str(value).translate(FA_DIGITS)


def human_bytes(n: int) -> str:
    if not n:
        return fa(0) + " B"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    s = f"{v:.1f}" if (v < 10 and i) else f"{round(v)}"
    return fa(s) + " " + units[i]


def _seen(ts: int | None) -> str:
    if not ts:
        return "بی‌استفاده"
    import time
    d = int(time.time()) - ts
    if d < 90:
        return "همین الان"
    if d < 3600:
        return f"{fa(d // 60)} دقیقه پیش"
    if d < 86400:
        return f"{fa(d // 3600)} ساعت پیش"
    return f"{fa(d // 86400)} روز پیش"


# --------------------------------------------------------------------------
# screens  (each returns (text, keyboard))
# --------------------------------------------------------------------------

def home(d: dict[str, Any]) -> tuple[str, list]:
    lines = [f"🌐 {d['title']} — پنل آیپی ایران", RULE]
    if not d["configured"]:
        lines += ["⚠️ هنوز کامل تنظیم نشده.",
                  "دکمه‌ی «راه‌اندازی» را بزن.", RULE]
    healthy = sum(1 for s in d["servers"] if s.get("health", {}).get("ok"))
    total = len(d["servers"])
    lines += [
        "📊 وضعیت",
        f"   کاربران: {fa(len(d['users']))}  ·  آنلاین: {fa(d['online_total'])}",
        f"   مصرف امروز: {human_bytes(d['today_bytes'])}",
        f"   مصرف این ماه: {human_bytes(d['month_bytes'])}",
        f"   سرور سالم: {fa(healthy)} از {fa(total)}",
        RULE,
    ]
    if d["servers"]:
        lines.append("🇮🇷 سرورهای ایران")
        for s in d["servers"]:
            lines.append("   " + server_line(s))
        lines.append(RULE)
    lines.append("انتخاب کن:")
    text = _mono("\n".join(lines))

    kb = [
        [btn("👤 کاربران", "users:0"), btn("➕ کاربر جدید", "user_new")],
        [btn("🇮🇷 سرورها", "servers"), btn("➕ افزودن سرور", "server_add")],
        [btn("🔄 بروزرسانی", "home"), btn("⚙️ تنظیمات", "settings")],
    ]
    return text, kb


def server_line(s: dict[str, Any]) -> str:
    h = s.get("health", {})
    if not s["enabled"]:
        dot, extra = "⚪", "غیرفعال"
    elif h.get("ok"):
        dot = "🟢"
        extra = f"{fa(h.get('latency_ms'))}ms" if h.get("latency_ms") is not None else "سالم"
        if h.get("selected"):
            extra += " ◄ فعال"
    elif h.get("reachable"):
        dot, extra = "🟡", (h.get("detail") or "خارج از مسیر")
    else:
        dot, extra = "🔴", "مسدود/قطع"
    return f"{dot} {s['label']}  {extra}"


def users_list(d: dict[str, Any], page: int = 0, per: int = 6) -> tuple[str, list]:
    users = d["users"]
    pages = max(1, (len(users) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = users[page * per:(page + 1) * per]

    lines = [f"👤 کاربران  ({fa(len(users))})", RULE]
    if not chunk:
        lines.append("هنوز کاربری ساخته نشده.")
    kb: list = []
    for u in chunk:
        lines.append(_user_summary(u))
        lines.append(RULE)
        kb.append([btn(f"{_status_emoji(u)} {u['label']}", f"user:{u['id']}")])

    nav = []
    if page > 0:
        nav.append(btn("◄ قبلی", f"users:{page-1}"))
    if page < pages - 1:
        nav.append(btn("بعدی ►", f"users:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([btn("➕ کاربر جدید", "user_new"), btn("🏠 خانه", "home")])
    return _mono("\n".join(lines)), kb


def _status_emoji(u: dict[str, Any]) -> str:
    if not u["enabled"]:
        return "⛔"
    return "🟢" if u["online"] else "⚪"


def _user_summary(u: dict[str, Any]) -> str:
    used = human_bytes(u["used_bytes"])
    quota = human_bytes(u["quota_bytes"]) if u["quota_bytes"] else "∞"
    state = ("آنلاین" if u["online"] else _seen(u["last_seen"])) if u["enabled"] \
        else {"quota": "حجم تمام", "expired": "منقضی"}.get(u["disabled_reason"], "غیرفعال")
    exp = f"{fa(u['days_left'])} روز" if u["days_left"] is not None else "بی‌انقضا"
    return (f"{_status_emoji(u)} {u['label']}\n"
            f"   {used} / {quota}  ·  {exp}  ·  {state}")


def user_detail(u: dict[str, Any]) -> tuple[str, list]:
    used = human_bytes(u["used_bytes"])
    quota = human_bytes(u["quota_bytes"]) if u["quota_bytes"] else "نامحدود"
    lines = [
        f"👤 {u['label']}", RULE,
        f"شناسه: {u['email']}",
        f"مصرف: {used} از {quota}",
        f"انقضا: {fa(u['days_left']) + ' روز' if u['days_left'] is not None else 'ندارد'}",
        f"وضعیت: {'فعال' if u['enabled'] else 'غیرفعال'}"
        + (f" ({u['disabled_reason']})" if not u['enabled'] and u['disabled_reason'] else ""),
        f"دستگاه آنلاین: {fa(u['online'])}",
        RULE,
    ]
    kb = [
        [btn("📎 لینک و QR", f"link:{u['id']}")],
        [btn("⛔ توقف" if u["enabled"] else "✅ فعال", f"toggle:{u['id']}"),
         btn("➕۳۰ روز", f"extend:{u['id']}")],
        [btn("♻️ صفر کردن حجم", f"resetu:{u['id']}"),
         btn("🔑 کلید جدید", f"rotate:{u['id']}")],
        [btn("🗑 حذف", f"del:{u['id']}")],
        [btn("◄ کاربران", "users:0"), btn("🏠 خانه", "home")],
    ]
    return _mono("\n".join(lines)), kb


def servers_list(d: dict[str, Any]) -> tuple[str, list]:
    lines = [f"🇮🇷 سرورهای ایران  ({fa(len(d['servers']))})", RULE]
    if not d["servers"]:
        lines.append("هنوز سروری اضافه نشده.")
        lines.append("لینک vless سرور ایران را بفرست.")
    kb: list = []
    for s in d["servers"]:
        lines.append(server_line(s))
        lines.append(f"   {s['address']}:{fa(s['port'])}")
        lines.append(RULE)
        kb.append([btn(f"{s['label']}", f"server:{s['tag']}")])
    lines.append(f"روش انتخاب: {_strategy_fa(d['strategy'])}")
    kb.append([btn("➕ افزودن سرور", "server_add"), btn("🔀 روش انتخاب", "strategy")])
    kb.append([btn("🏠 خانه", "home")])
    return _mono("\n".join(lines)), kb


def server_detail(s: dict[str, Any]) -> tuple[str, list]:
    h = s.get("health", {})
    lines = [
        f"🇮🇷 {s['label']}  ({s['tag']})", RULE,
        f"آدرس: {s['address']}:{fa(s['port'])}",
        f"SNI: {s['sni']}",
        f"وضعیت: {server_line(s).split('  ', 1)[-1] if '  ' in server_line(s) else '—'}",
        f"تأخیر: {fa(h.get('latency_ms')) + 'ms' if h.get('latency_ms') is not None else '—'}",
        RULE,
    ]
    kb = [
        [btn("⛔ غیرفعال" if s["enabled"] else "✅ فعال", f"srv_toggle:{s['tag']}")],
        [btn("🗑 حذف سرور", f"srv_del:{s['tag']}")],
        [btn("◄ سرورها", "servers"), btn("🏠 خانه", "home")],
    ]
    return _mono("\n".join(lines)), kb


def settings_screen(d: dict[str, Any]) -> tuple[str, list]:
    lines = [
        "⚙️ تنظیمات", RULE,
        f"آدرس سرور خارجی: {d['public_host'] or '— تنظیم نشده'}",
        f"دامنه‌ی استتار (SNI): {d['sni']}",
        f"روش انتخاب سرور: {_strategy_fa(d['strategy'])}",
        RULE,
        "نکته: اگر همه‌ی سرورهای ایران قطع شوند،",
        "اتصال بسته می‌شود و از آی‌پی خارجی بیرون نمی‌رود.",
    ]
    kb = [
        [btn("🔀 روش انتخاب سرور", "strategy")],
        [btn("🔑 ساخت کلید جدید REALITY", "newkeys")],
        [btn("🏠 خانه", "home")],
    ]
    return _mono("\n".join(lines)), kb


def strategy_screen(current: str) -> tuple[str, list]:
    lines = ["🔀 روش انتخاب سرور", RULE,
             "• کم‌ترین تأخیر: همیشه سریع‌ترین سالم",
             "• تصادفی: پخش بار بین سرورها",
             "• چرخشی: نوبتی بین همه",
             RULE, f"فعلی: {_strategy_fa(current)}"]
    kb = [
        [btn(("● " if current == "leastPing" else "") + "کم‌ترین تأخیر", "setstrat:leastPing")],
        [btn(("● " if current == "random" else "") + "تصادفی", "setstrat:random")],
        [btn(("● " if current == "roundRobin" else "") + "چرخشی", "setstrat:roundRobin")],
        [btn("◄ تنظیمات", "settings")],
    ]
    return _mono("\n".join(lines)), kb


def link_message(label: str, link: str) -> str:
    # The link sits in its own fenced block so a tap copies the whole URI.
    # The label is sanitised because it appears outside any code fence.
    return (f"📎 کانفیگ «{_safe(label)}»\n"
            f"{RULE}\n"
            "این را در v2rayNG یا Hiddify وارد کن (روی کادر زیر بزن تا کپی شود):\n"
            f"```\n{link}\n```\n"
            "برای اینکه فقط برنامه‌های ایرانی از تونل بروند، در برنامه حالت "
            "per-app را روشن کن.")


def _safe(text: str) -> str:
    """Drop the few characters that would break legacy-Markdown parsing when
    shown outside a code fence."""
    for ch in "`*_[]":
        text = text.replace(ch, "")
    return text


def _strategy_fa(s: str) -> str:
    return {"leastPing": "کم‌ترین تأخیر", "random": "تصادفی",
            "roundRobin": "چرخشی"}.get(s, s)


def _mono(text: str) -> str:
    return f"```\n{text}\n```"


def btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}
