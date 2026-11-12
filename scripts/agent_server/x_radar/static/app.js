"use strict";

const $ = (selector) => document.querySelector(selector);
const labels = {comment: "کامنت روی پست تو", smart: "پست جدید SMART"};
const number = new Intl.NumberFormat("fa-IR");
const time = new Intl.DateTimeFormat("fa-IR", {
  dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Tehran",
});
let state, filter = "", page = 1, dashboardRequest = 0, inboxVersion = "";
const working = new WeakSet();

const h = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => (
  {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]
));
const n = (value) => number.format(value);
const when = (value) => value && Number.isFinite(Date.parse(value)) ? time.format(new Date(value)) : "—";
const settingsPage = () => location.hash === "#settings";

function link(text, value, cls = "button") {
  try {
    const url = new URL(value);
    if (url.origin === "https://x.com" && !url.username && !url.password) {
      return `<a class="${cls}" href="${h(url.href)}" target="_blank" rel="noopener noreferrer">${h(text)}</a>`;
    }
  } catch { /* A malformed link is not rendered. */ }
  return "";
}

function notice(message, error = false) {
  const node = $("#notice");
  node.hidden = !message;
  node.classList.toggle("error", error);
  node.textContent = message;
}

async function api(path, body) {
  const config = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Radar-Request": "1"},
    body: JSON.stringify(body),
  };
  const response = await fetch("./api/" + path, config);
  if (response.status === 401 && path !== "login") showLogin();
  const data = await response.json();
  if (response.ok) return data;
  throw new Error(typeof data.detail === "string" ? data.detail : "پاسخ پنل قابل خواندن نیست.");
}

function showLogin() {
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-form input").focus();
}

async function work(button, task) {
  if (button?.disabled || working.has(button)) return;
  if (button) {
    working.add(button);
    button.setAttribute("aria-busy", "true");
  }
  try {
    await task();
  } catch (error) {
    if ($("#login").hidden) notice(error.message, true);
    else $("#login-error").textContent = error.message;
  } finally {
    if (button) {
      working.delete(button);
      button.removeAttribute("aria-busy");
    }
  }
}

function draftBlock(draft) {
  return `<div class="draft">
    <p class="muted">پیش‌نویس پاسخ</p>
    <blockquote dir="auto">${h(draft.text)}</blockquote>
    <div class="actions">
      <button type="button" data-action="copy" data-text="${h(draft.text)}">کپی پاسخ</button>
      ${link("باز کردن فرم پاسخ در X", draft.intent, "button primary")}
    </div>
  </div>`;
}

function card(item) {
  const post = item.post;
  let reply = `<p class="muted result">در صف ساخت پیش‌نویس</p>`;
  if (item.draft) {
    reply = draftBlock(item.draft);
  } else if (item.draft_error) {
    reply = `<p class="muted result">${h(item.draft_error)}</p>
      <button type="button" data-action="retry" data-url="${h(post.url)}">تلاش دوباره برای پیش‌نویس</button>`;
  }
  const delivery = item.telegram_error || (item.telegram === "unknown" ? "وضعیت پیام تلگرام مشخص نیست." : "");
  return `<article class="card">
    <div class="row">
      <div class="actions"><span class="tag">${labels[item.kind]}</span><strong><bdi>@${h(post.author)}</bdi></strong></div>
      <time class="muted" datetime="${h(post.date)}">${when(post.date)}</time>
    </div>
    <p class="source" dir="auto">${h(post.text)}</p>
    ${link("دیدن پست در X", post.url)}
    ${reply}
    ${delivery ? `<p class="muted result">${h(delivery)}</p>` : ""}
  </article>`;
}

async function refreshDashboard() {
  const request = ++dashboardRequest;
  const [snapshot, inbox] = await Promise.all([
    api("state"), api("inbox?" + new URLSearchParams({kind: filter, page: String(page)})),
  ]);
  if (request !== dashboardRequest || settingsPage() || $("#shell").hidden) return;
  state = snapshot;
  const last = state.last_scan;
  $("#scan-status").innerHTML = `
    <div class="panel metric"><span class="muted">رصد دوره‌ای</span>
      <strong>${state.options.scheduled ? "هر " + n(state.options.interval) + " دقیقه" : "خاموش"}</strong>
      <p class="muted">${state.keys.sorsa ? "کلید سورسا ثبت شده" : "کلید سورسا را در تنظیمات وارد کن"}</p></div>
    <div class="panel metric"><span class="muted">درخواست‌های امروز سورسا</span>
      <strong>${n(state.used_today)} از ${n(state.options.daily_limit)}</strong>
      ${state.used_today >= state.options.daily_limit ? '<p class="muted">سقف امروز پر شده است.</p>' : ""}</div>
    <div class="panel metric"><span class="muted">آخرین بررسی</span>
      <strong>${when(last?.at)}</strong>
      ${last?.pending ? `<p class="muted">${n(last.pending)} بخش برای ادامه</p>` : ""}
      ${last?.skipped ? `<p class="muted">${n(last.skipped)} مورد با دادهٔ ناقص</p>` : ""}
      ${last?.errors?.length ? `<p class="error">${h(last.errors.join(" · "))}</p>` : ""}</div>`;
  const version = JSON.stringify([filter, page, inbox]);
  if (version !== inboxVersion) {
    inboxVersion = version;
    $("#inbox").innerHTML = inbox.rows.map(card).join("") ||
      `<p class="empty">${state.feeds ? "هنوز مورد تازه‌ای ثبت نشده است." : "برای شروع، حساب خودت یا لیست SMART را در تنظیمات ثبت کن."}</p>`;
  }
  $("#pager").hidden = page === 1 && !inbox.more;
  $('[data-step="-1"]').disabled = page === 1;
  $('[data-step="1"]').disabled = !inbox.more;
}

function fillSettings() {
  const form = $("#monitor-form");
  const options = state.options;
  for (const name of ["owner", "public_url", "interval", "daily_limit"]) form.elements[name].value = options[name];
  form.elements.smart_list.value = options.smart_list ? "https://x.com/i/lists/" + options.smart_list : "";
  form.elements.smart_handles.value = options.smart_handles.map((name) => "@" + name).join("\n");
  form.elements.scheduled.checked = options.scheduled;
  form.elements.telegram_enabled.checked = options.telegram_enabled;
  $("#telegram-status").textContent = state.telegram_ready
    ? "شناسهٔ مالک و توکن بات تلگرام ثبت شده‌اند."
    : "برای پیام خصوصی، شناسهٔ عددی مالک و توکن بات را در هاستلس ثبت کن.";
  $("#usage-estimate").textContent = state.feeds
    ? `هر دور کامل حداقل ${n(state.feeds)} درخواست سورسا، با صفحه‌های اضافه بیشتر` : "";
  for (const service of ["sorsa", "openrouter"]) {
    $("#" + service + "-status").textContent = state.keys[service] ? "ثبت شده" : "ثبت نشده";
  }
}

async function showPage() {
  dashboardRequest++;
  const settings = settingsPage();
  $("#dashboard").hidden = settings;
  $("#settings").hidden = !settings;
  $("#scan").hidden = settings;
  $("#title").textContent = settings ? "تنظیمات" : "داشبورد";
  for (const tab of document.querySelectorAll("nav a")) {
    if (tab.hash === (settings ? "#settings" : "#dashboard")) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  if (settings) {
    state = await api("state");
    if (settingsPage()) fillSettings();
  } else {
    await refreshDashboard();
  }
}

$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  work(form.querySelector("button"), async () => {
    $("#login-error").textContent = "";
    await api("login", {password: form.elements.password.value});
    form.reset();
    $("#login").hidden = true;
    $("#shell").hidden = false;
    await showPage();
  });
});

$("#monitor-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  work(form.querySelector("button"), async () => {
    const options = Object.fromEntries(new FormData(form));
    options.interval = Number(options.interval);
    options.daily_limit = Number(options.daily_limit);
    options.scheduled = form.elements.scheduled.checked;
    options.telegram_enabled = form.elements.telegram_enabled.checked;
    await api("options", options);
    await showPage();
    notice("تنظیمات ذخیره شد.");
  });
});

for (const form of document.querySelectorAll("form[data-key]")) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    work(form.querySelector("button"), async () => {
      await api("secret", {service: form.dataset.key, key: form.elements.key.value.trim()});
      form.reset();
      state = await api("state");
      $("#" + form.dataset.key + "-status").textContent = state.keys[form.dataset.key] ? "ثبت شده" : "ثبت نشده";
      notice("کلید سرویس ذخیره شد.");
    });
  });
}

$("#draft-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  work(form.querySelector("button"), async () => {
    const draft = await api("draft", {url: form.elements.url.value});
    $("#draft-result").innerHTML = draftBlock(draft) + link("دیدن پست در X", draft.post.url);
  });
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  work(button, async () => {
    switch (button.dataset.action) {
      case "logout":
        await api("logout", {});
        $("#inbox").replaceChildren();
        $("#draft-result").replaceChildren();
        $("#monitor-form").reset();
        inboxVersion = "";
        showLogin();
        break;
      case "scan": {
        notice("در حال خواندن پست‌های جدید…");
        const result = await api("scan", {});
        await refreshDashboard();
        notice(`${n(result.new)} مورد تازه ثبت شد · ${n(result.requests)} درخواست سورسا`, result.errors.length > 0);
        break;
      }
      case "retry":
        await api("draft", {url: button.dataset.url});
        await refreshDashboard();
        break;
      case "copy":
        await navigator.clipboard.writeText(button.dataset.text);
        notice("متن پاسخ کپی شد.");
        break;
      case "filter":
        filter = button.dataset.kind;
        page = 1;
        for (const option of $("#filters").querySelectorAll("button")) {
          option.setAttribute("aria-pressed", String(option === button));
        }
        await refreshDashboard();
        break;
      case "page":
        page += Number(button.dataset.step);
        await refreshDashboard();
        break;
    }
  });
});

window.addEventListener("hashchange", () => {
  if (!$("#shell").hidden) work(null, showPage);
});
setInterval(() => {
  if (!$("#shell").hidden && !settingsPage() && !document.hidden) refreshDashboard().catch(() => {});
}, 15000);

api("state").then((snapshot) => {
  state = snapshot;
  $("#shell").hidden = false;
  showPage().catch((error) => notice(error.message, true));
}).catch(showLogin);
