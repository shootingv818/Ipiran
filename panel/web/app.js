"use strict";

// The whole panel lives under /<secret>/. The page was served from there,
// so API calls are relative to the directory of the current path.
const BASE = location.pathname.replace(/\/$/, "");

const $ = (id) => document.getElementById(id);
const banner = $("banner");

let clients = [];

/* ----------------------------------------------------------- utilities */

function fa(n) {
  // Western digits -> Persian digits for display.
  return String(n).replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[+d]);
}

function bytes(n) {
  if (!n) return fa(0) + " B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  const s = v < 10 && i > 0 ? v.toFixed(1) : Math.round(v).toString();
  return fa(s) + " " + units[i];
}

function say(msg, good = false) {
  banner.textContent = msg;
  banner.classList.toggle("good", good);
  banner.classList.remove("hidden");
  if (good) setTimeout(() => banner.classList.add("hidden"), 3500);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(BASE + path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) throw new Error((data && data.detail) || `خطا ${res.status}`);
  return data;
}

function el(tag, props = {}, kids = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v != null) n.setAttribute(k, v);
  }
  for (const kid of [].concat(kids)) if (kid) n.append(kid);
  return n;
}

/* ------------------------------------------------------------- render */

function render(state) {
  $("title").textContent = state.title;
  document.title = state.title;
  $("server-dot").className = "dot " + (state.configured ? "ok" : "bad");

  $("s-online").textContent = fa(state.online_total);
  $("s-today").textContent = bytes(state.today_bytes);
  $("s-month").textContent = bytes(state.month_bytes);
  $("s-count").textContent = fa(state.clients.length);

  renderChart(state.usage);
  renderClients(state.clients);

  if (!state.configured) {
    say("پنل کامل تنظیم نشده: کلید REALITY یا آدرس سرور خالی است.");
  } else {
    banner.classList.add("hidden");
  }
}

function renderChart(usage) {
  const host = $("chart");
  host.replaceChildren();
  const peak = Math.max(1, ...usage.map((d) => d.bytes));
  for (const d of usage) {
    const h = Math.max(3, (d.bytes / peak) * 100);
    const label = d.day.slice(8); // day of month
    host.append(el("div", {
      class: "bar",
      style: `height:${h}%`,
      "data-tip": `${label}: ${bytes(d.bytes)}`,
    }, el("span", { text: fa(label) })));
  }
}

function statusBadge(c) {
  if (!c.enabled) {
    const reason = { quota: "حجم تمام", expired: "منقضی", manual: "غیرفعال" };
    return el("span", { class: "badge off", text: reason[c.disabled_reason] || "غیرفعال" });
  }
  if (c.online > 0) {
    return el("span", { class: "badge online", text: `${fa(c.online)} آنلاین` });
  }
  return el("span", { class: "badge idle", text: lastSeen(c.last_seen) });
}

function lastSeen(ts) {
  if (!ts) return "بی‌استفاده";
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 90) return "همین الان";
  if (diff < 3600) return `${fa(Math.floor(diff / 60))} دقیقه پیش`;
  if (diff < 86400) return `${fa(Math.floor(diff / 3600))} ساعت پیش`;
  return `${fa(Math.floor(diff / 86400))} روز پیش`;
}

function renderClients(list) {
  clients = list;
  const host = $("clients");
  host.replaceChildren();
  if (!list.length) {
    host.append(el("p", { class: "hint", text: "هنوز کاربری ساخته نشده." }));
    return;
  }
  for (const c of list) {
    const used = c.quota_bytes
      ? `${bytes(c.used_bytes)} از ${bytes(c.quota_bytes)}`
      : bytes(c.used_bytes);
    const meterLevel = c.percent == null ? "" : c.percent >= 100 ? "over" : c.percent >= 80 ? "hot" : "";

    const card = el("div", { class: "client" }, [
      el("div", { class: "client-top" }, [
        el("div", { class: "client-name" }, [
          el("span", { text: c.label }),
        ]),
        statusBadge(c),
      ]),
      c.quota_bytes
        ? el("div", { class: `meter ${meterLevel}` },
            el("i", { style: `width:${Math.min(100, c.percent || 0)}%` }))
        : null,
      el("div", { class: "client-meta" }, [
        el("span", { text: used }),
        el("span", { text: c.expires_at ? `${fa(c.days_left)} روز مانده` : "بدون انقضا" }),
      ]),
      el("div", { class: "client-actions" }, [
        el("button", { class: "mini primary", text: "QR / لینک", onclick: () => openShare(c) }),
        el("button", {
          class: "mini ghost",
          text: c.enabled ? "توقف" : "فعال",
          onclick: () => patch(c.id, { enabled: !c.enabled }, c.enabled ? "متوقف شد" : "فعال شد"),
        }),
        el("button", { class: "mini ghost", text: "+۳۰ روز", onclick: () => patch(c.id, { add_days: 30 }, "تمدید شد") }),
        el("button", { class: "mini ghost", text: "صفر حجم", onclick: () => patch(c.id, { reset_usage: true }, "صفر شد") }),
        el("button", {
          class: "mini danger", text: "حذف",
          onclick: () => { if (confirm(`«${c.label}» حذف شود؟`)) remove(c.id); },
        }),
      ]),
    ]);
    host.append(card);
  }
}

/* ------------------------------------------------------------ actions */

async function reload() {
  try { render(await api("/api/state")); }
  catch (e) { say(e.message); }
}

async function guard(fn, ok) {
  try { await fn(); await reload(); if (ok) say(ok, true); }
  catch (e) { say(e.message); }
}

const patch = (id, body, ok) => guard(() => api(`/api/clients/${id}`, { method: "POST", body }), ok);
const remove = (id) => guard(() => api(`/api/clients/${id}/delete`, { method: "POST" }), "حذف شد");

/* ------------------------------------------------------------- share */

const dlgShare = $("dlg-share");

function renderQR(container, text) {
  // Auto-pick the smallest type number that fits, error-correction level M.
  for (let type = 4; type <= 20; type += 1) {
    try {
      const qr = qrcode(type, "M");
      qr.addData(text);
      qr.make();
      container.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
      return;
    } catch (e) {
      // too small for this type; try the next
    }
  }
  container.textContent = "لینک برای QR بیش از حد بلند است.";
}

async function openShare(c) {
  $("share-name").textContent = c.label;
  $("share-qr").replaceChildren();
  $("share-link").value = "در حال ساخت…";
  dlgShare.showModal();
  try {
    const res = await fetch(`${BASE}/api/clients/${c.id}/link`);
    const link = await res.text();
    if (!res.ok) throw new Error(link || `خطا ${res.status}`);
    $("share-link").value = link;
    renderQR($("share-qr"), link);
  } catch (e) {
    $("share-qr").replaceChildren();
    $("share-link").value = "خطا در ساخت لینک: " + e.message;
  }
}

$("share-close").addEventListener("click", () => dlgShare.close());
$("share-copy").addEventListener("click", async () => {
  const box = $("share-link");
  try { await navigator.clipboard.writeText(box.value); say("کپی شد", true); }
  catch { box.select(); say("دستی کپی کن"); }
});

/* --------------------------------------------------------- new client */

const dlgNew = $("dlg-new");
$("btn-new").addEventListener("click", () => { $("form-new").reset(); dlgNew.showModal(); });
$("form-new").addEventListener("submit", (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  guard(async () => {
    await api("/api/clients", { method: "POST", body: data });
    dlgNew.close();
  }, "کاربر ساخته شد");
});

/* ----------------------------------------------------------- top bar */

$("btn-refresh").addEventListener("click", () =>
  guard(() => api("/api/poll", { method: "POST" }), "به‌روز شد"));

$("btn-apply").addEventListener("click", () => {
  if (!confirm("کانفیگ بازنویسی و Xray ری‌استارت شود؟ اتصال‌های فعلی لحظه‌ای قطع می‌شوند.")) return;
  guard(() => api("/api/apply", { method: "POST" }), "اعمال شد");
});

/* -------------------------------------------------------------- boot */

reload();
setInterval(reload, 10000);
