"use strict";

// The whole panel lives under /<secret>/. The page was served from there,
// so API calls are relative to the directory of the current path.
const BASE = location.pathname.replace(/\/$/, "");

const $ = (id) => document.getElementById(id);
const banner = $("banner");

/* ----------------------------------------------------------- utilities */

function fa(n) {
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

function rate(bps) {
  if (!bps || bps < 64) return fa(0);
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let i = 0;
  let v = bps;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return fa(v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)) + " " + units[i];
}

function mmss(total) {
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${fa(m)}:${fa(String(s).padStart(2, "0"))}`;
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

/* ------------------------------------------------------- dashboard state */

function render(state) {
  $("title").textContent = state.title;
  document.title = state.title;
  $("server-dot").className = "dot " + (state.configured ? "ok" : "bad");

  $("s-online").textContent = fa(state.online_total);
  $("s-speed").replaceChildren(
    el("span", { class: "rate-down", text: "↓ " + rate(state.down_bps) }),
    el("span", { class: "rate-up", text: "↑ " + rate(state.up_bps) }),
  );
  $("s-today").textContent = bytes(state.today_bytes);
  $("s-month").textContent = bytes(state.month_bytes);

  renderChart(state.usage);
  renderClients(state.clients);
  renderMonitorControls(state.monitor);

  $("sec-note").textContent =
    `لینک پنل تنها کلید ورود است. اگر فکر می‌کنی جایی لو رفته، عوضش کن؛ لینک قبلی `
    + `بلافاصله از کار می‌افتد. آدرس فعلی روی ${location.protocol === "https:"
      ? "HTTPS است و در شبکه دیده نمی‌شود." : "HTTP است، پس در شبکه‌ی اپراتور قابل دیدن است."}`;

  if (!state.configured) {
    say("پنل کامل تنظیم نشده: کلید REALITY یا آدرس سرور خالی است.");
  }
}

function renderChart(usage) {
  const host = $("chart");
  host.replaceChildren();
  const peak = Math.max(1, ...usage.map((d) => d.bytes));
  for (const d of usage) {
    const h = Math.max(3, (d.bytes / peak) * 100);
    const label = d.day.slice(8);
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
    const level = c.percent == null ? "" : c.percent >= 100 ? "over" : c.percent >= 80 ? "hot" : "";
    const moving = c.down_bps + c.up_bps > 0;

    host.append(el("div", { class: "client" }, [
      el("div", { class: "client-top" }, [
        el("div", { class: "client-name" }, el("span", { text: c.label })),
        statusBadge(c),
      ]),
      c.quota_bytes
        ? el("div", { class: `meter ${level}` },
            el("i", { style: `width:${Math.min(100, c.percent || 0)}%` }))
        : null,
      el("div", { class: "client-meta" }, [
        el("span", { text: used }),
        moving
          ? el("span", { class: "client-rate",
              text: `↓ ${rate(c.down_bps)}  ↑ ${rate(c.up_bps)}` })
          : el("span", { text: c.expires_at ? `${fa(c.days_left)} روز مانده` : "بدون انقضا" }),
      ]),
      el("div", { class: "client-actions" }, [
        el("button", { class: "mini primary", text: "QR / لینک", onclick: () => openShare(c) }),
        el("button", {
          class: "mini ghost",
          text: c.enabled ? "توقف" : "فعال",
          onclick: () => patch(c.id, { enabled: !c.enabled }, c.enabled ? "متوقف شد" : "فعال شد"),
        }),
        el("button", { class: "mini ghost", text: "+۳۰ روز",
          onclick: () => patch(c.id, { add_days: 30 }, "تمدید شد") }),
        el("button", { class: "mini ghost", text: "صفر حجم",
          onclick: () => patch(c.id, { reset_usage: true }, "صفر شد") }),
        el("button", { class: "mini danger", text: "حذف",
          onclick: () => { if (confirm(`«${c.label}» حذف شود؟`)) remove(c.id); } }),
      ]),
    ]));
  }
}

/* ------------------------------------------------------------ path view */

function renderPath(data) {
  const v = $("verdict");
  if (data.inside_tunnel) {
    v.className = "verdict inside";
    v.replaceChildren(
      el("div", { html:
        "<b>✅ همین الان از داخل تونل وصلی.</b> آی‌پی‌ای که سرور از تو می‌بیند "
        + `<code>${data.your_ip}</code> است، یعنی خود سرور، نه آی‌پی اینترنت تو. `
        + `سایت‌ها هم <code>${data.exit_ip}</code> را می‌بینند.` }),
    );
  } else {
    v.className = "verdict outside";
    v.replaceChildren(
      el("div", { html:
        "<b>⚠️ این صفحه را بیرون از تونل باز کرده‌ای.</b> آی‌پی واقعی تو "
        + `<code>${data.your_ip}</code> است. اگر فیلترشکن را روشن کنی و همین صفحه `
        + "را دوباره باز کنی، اینجا باید آی‌پی سرور را نشان بدهد." }),
    );
  }

  const list = $("path");
  list.replaceChildren();
  for (const s of data.steps) {
    list.append(el("li", { class: s.ok ? "" : "off" }, [
      el("div", { class: "p-title", text: s.title }),
      el("div", { class: "p-detail", text: s.detail }),
      el("div", { class: "p-note", text: s.note }),
    ]));
  }
}

async function loadPath() {
  try { renderPath(await api("/api/path")); }
  catch (e) { say(e.message); }
}

/* --------------------------------------------------------- live monitor */

let monitorTimer = null;

function renderMonitorControls(m) {
  const active = m && m.active;
  $("mon-start").classList.toggle("hidden", !!active);
  $("mon-stop").classList.toggle("hidden", !active);
  $("mon-count").classList.toggle("hidden", !active);
  if (active) $("mon-count").textContent = "باز تا " + mmss(m.seconds_left);
}

function renderMonitorFeed(feed) {
  renderMonitorControls(feed);
  const sum = $("mon-summary");
  const table = $("mon-table");

  if (!feed.active) {
    sum.classList.add("hidden");
    table.replaceChildren();
    if (feed.error) say(feed.error);
    return;
  }

  const s = feed.summary || {};
  sum.classList.remove("hidden");
  sum.replaceChildren(
    el("span", { html: `اتصال: <b>${fa(s.total || 0)}</b>` }),
    el("span", { html: `نام شناسایی‌شده: <b>${fa(s.sniffed || 0)}</b>` }),
    el("span", { html: `دامنه‌ی یکتا: <b>${fa(s.unique_hosts || 0)}</b>` }),
    el("span", { html: `آی‌پی خام: <b>${fa(s.raw_ip || 0)}</b>` }),
  );

  table.replaceChildren();
  if (!(feed.entries || []).length) {
    table.append(el("p", { class: "hint",
      text: "هنوز چیزی رد نشده. یک برنامه را روی گوشی باز کن تا اینجا ظاهر شود." }));
    return;
  }
  for (const e of feed.entries) {
    const dest = el("span", { class: "m-host" }, [
      el("span", { text: e.host + (e.port ? ":" + e.port : "") }),
      // The pairing is the proof: the client handed over an address, and the
      // server recovered the name from the traffic itself.
      e.raw ? el("small", { class: "m-raw", text: "درخواست شد: " + e.raw }) : null,
    ]);
    table.append(el("div", { class: "mon-row" }, [
      el("span", { class: "m-time", text: e.time }),
      dest,
      el("span", {
        class: "tag " + (e.sniffed ? "yes" : "no"),
        text: e.sniffed ? "اسنیف شد" : "آی‌پی خام",
        title: e.sniffed
          ? "نام دامنه روی سرور از دل ترافیک خوانده شد، پس دستگاهت لازم نبود "
            + "از اپراتور آدرس بپرسد."
          : "برای این اتصال نامی در ترافیک نبود که خوانده شود.",
      }),
    ]));
  }
}

async function pollMonitor() {
  try {
    const feed = await api("/api/monitor");
    renderMonitorFeed(feed);
    if (feed.active) {
      startMonitorLoop();          // window may have been opened elsewhere
    } else if (monitorTimer) {
      clearInterval(monitorTimer);
      monitorTimer = null;
    }
  } catch (e) { /* transient, the next tick retries */ }
}

function startMonitorLoop() {
  if (monitorTimer) return;
  monitorTimer = setInterval(pollMonitor, 2000);
  pollMonitor();
}

$("mon-start").addEventListener("click", () => {
  if (!confirm("مونیتور ۱۰ دقیقه‌ای روشن شود؟ Xray ری‌استارت می‌شود و اتصال‌ها یک لحظه قطع می‌شوند.")) return;
  guard(async () => {
    await api("/api/monitor/start", { method: "POST", body: { minutes: 10 } });
    startMonitorLoop();
  }, "مونیتور روشن شد؛ ۱۰ دقیقه بعد خودش خاموش می‌شود.");
});

$("mon-stop").addEventListener("click", () => {
  guard(async () => {
    await api("/api/monitor/stop", { method: "POST" });
    await pollMonitor();
  }, "مونیتور خاموش شد و لاگ پاک شد.");
});

/* ------------------------------------------------------------- actions */

async function reload() {
  try { render(await api("/api/state")); }
  catch (e) { say(e.message); }
}

async function guard(fn, ok) {
  try { await fn(); await reload(); if (ok) say(ok, true); }
  catch (e) { say(e.message); }
}

const patch = (id, body, ok) =>
  guard(() => api(`/api/clients/${id}`, { method: "POST", body }), ok);
const remove = (id) =>
  guard(() => api(`/api/clients/${id}/delete`, { method: "POST" }), "حذف شد");

/* ------------------------------------------------------------- share */

const dlgShare = $("dlg-share");

function renderQR(container, text) {
  for (let type = 4; type <= 20; type += 1) {
    try {
      const qr = qrcode(type, "M");
      qr.addData(text);
      qr.make();
      container.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
      return;
    } catch (e) { /* too small, try the next type */ }
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

$("btn-path").addEventListener("click", loadPath);

$("btn-rotate").addEventListener("click", async () => {
  if (!confirm("لینک مخفی پنل عوض شود؟ آدرس فعلی از کار می‌افتد و به آدرس جدید منتقل می‌شوی.")) return;
  try {
    const r = await api("/api/secret/rotate", { method: "POST" });
    location.href = `/${r.secret}/`;
  } catch (e) { say(e.message); }
});

/* -------------------------------------------------------------- boot */

reload();
loadPath();
pollMonitor();
setInterval(reload, 8000);
setInterval(loadPath, 60000);
