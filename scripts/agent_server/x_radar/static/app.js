"use strict";

const $ = (selector) => document.querySelector(selector);
const pages = {
  dashboard: ["داشبورد", "حساب‌های تحت رصد، امتیازها و پیش‌نویس جواب"],
  personal: ["حساب من", "فالوورها، فالووینگ‌ها و افراد SMART حساب خودت"],
  followers: ["فالوورها", "دریافت فهرست‌ها و فیلتر براساس امتیاز و نسبت فالو"],
  compare: ["فالو‌بک و اشتراک", "فهرست پیشنهادی آنفالو و حساب‌های مشترک"],
  history: ["تاریخچه", "رویدادهای ثبت‌شده، مقایسهٔ فهرست‌ها و خروجی CSV"],
  alerts: ["هشدارها", "اعلان‌ها و شرایط ارسال آن‌ها به تلگرام"],
  settings: ["تنظیمات", "اتصال سرویس‌ها، زمان بررسی و پشتیبان‌گیری"],
};
const eventLabels = {follow: "فالو", unfollow: "آنفالو", reply: "ریپلای"};
const directions = {followers: "فالوورها", following: "فالووینگ‌ها", nonfollowers: "فالو‌بک نداده‌ها", smart: "افراد SMART"};
const services = {sorsa: "سورسا", twitter: "توییتر‌اسکور", openrouter: "مدل OpenRouter", telegram: "توکن بات تلگرام"};
const number = new Intl.NumberFormat("fa-IR", {maximumFractionDigits: 2});
const time = new Intl.DateTimeFormat("fa-IR", {dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Tehran"});
let state, selectedProfile = "", group = "", alertPage = 1, unreadOnly = false, audio;

const h = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
));
const n = (value) => typeof value === "number" && Number.isFinite(value) ? number.format(value) : "—";
const when = (value) => value && !Number.isNaN(Date.parse(value)) ? time.format(new Date(value)) : "—";
const bdi = (value) => `<bdi dir="ltr">${h(value)}</bdi>`;
const empty = (text) => `<p class="empty">${h(text)}</p>`;
const name = (value) => value ? bdi("@" + value) : "—";
const checked = (value) => value ? " checked" : "";
const selected = (a, b) => a === b ? " selected" : "";
const action = (label, key, attrs = {}, css = "") => `<button type="button" class="${css}" data-action="${key}" ${Object.entries(attrs).map(([k,v]) => `data-${k}="${h(v)}"`).join(" ")}>${h(label)}</button>`;
const panel = (title, inner, note = "") => `<section class="panel"><h2>${title}</h2>${note ? `<p class="muted">${note}</p>` : ""}${inner}</section>`;
const table = (headers, records) => records.length ? `<div class="table"><table><thead><tr>${headers.map((s) => `<th scope="col">${s}</th>`).join("")}</tr></thead><tbody>${records.map((cells) => `<tr>${cells.map((s) => `<td>${s}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : empty("هنوز موردی برای نمایش نیست.");
const options = (entries, value) => Object.entries(entries).map(([key, label]) => `<option value="${h(key)}"${selected(key,value)}>${h(label)}</option>`).join("");
const checkbox = (key, label, value = false) => `<label class="check"><input type="checkbox" name="${key}"${checked(value)}> ${label}</label>`;
const input = (key, label, value = "", attrs = "") => `<label>${label}<input name="${key}" value="${h(value)}" ${attrs}></label>`;
const safeUrl = (value) => {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
};
const link = (text, url, cls = "") => safeUrl(url) ? `<a class="${cls}" href="${h(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${h(text)}</a>` : "";
const handleInput = (key, label, val = "", required = false) => input(key, label, val, `dir="ltr" placeholder="@username" maxlength="120" ${required ? "required" : ""}`);
const scanButton = (label, mode, subject, restart = false) => action(label, "scan", {mode, subject, restart: restart ? "yes" : ""});

function notice(message, bad = false) {
  const node = $("#notice");
  node.hidden = !message;
  node.classList.toggle("error", bad);
  node.textContent = message;
}

async function api(path, body) {
  const config = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-Radar-Request": "1"}, body: JSON.stringify(body),
  };
  const response = await fetch("./api/" + path, config);
  const data = await response.json();
  if (response.ok) return data;
  if (response.status === 401 && path !== "login") showLogin();
  const detail = typeof data.detail === "string" ? data.detail : "ورودی یا پاسخ سرور معتبر نیست.";
  throw new Error(detail);
}

function showLogin() {
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-form input").focus();
}

async function loadState() {
  state = await api("state");
  $("#unread").textContent = state.unread ? n(state.unread) : "";
  $("#login").hidden = true;
  $("#shell").hidden = false;
}

async function work(button, task) {
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    await task();
  } catch (error) {
    if ($("#login").hidden) notice(error.message, true);
    else $("#login-error").textContent = error.message;
  } finally {
    if (button) button.disabled = false;
  }
}

function draftBlock(item) {
  if (!item) return "";
  return `<div class="draft"><p>پیش‌نویس پاسخ برای ${name(item.post.author)}</p>
    <blockquote dir="ltr">${h(item.text)}</blockquote>
    <div class="actions">${action("کپی پاسخ", "copy", {text: item.text})}
    ${link("باز کردن پاسخ در X", item.intent, "button primary")}
    ${link("پست اصلی", item.post.url, "button")}
    ${action("پاسخ دیگری بساز", "draft", {url: item.post.url, regenerate: "yes"})}</div></div>`;
}

function profileBlock(item) {
  const profile = item?.profile || {};
  if (!item) return empty("یک حساب را برای دیدن جزئیات انتخاب کن.");
  const top = item.top || [];
  const recent = item.activity || [];
  return `<div class="row"><div><h3>${h(profile.name || item.handle)} ${name(item.handle)}</h3>
    <p class="muted">آخرین بررسی ${when(profile.checked_at)}</p></div>
    ${scanButton("دریافت جزئیات", "profile", item.handle)}</div>
    ${profile.bio ? `<p>${h(profile.bio)}</p>` : ""}
    <div class="table"><table><tbody>
    <tr><th>فالوور</th><td>${n(profile.followers)}</td><th>فالووینگ</th><td>${n(profile.following)}</td></tr>
    <tr><th>نسبت فالو ٪</th><td>${n(profile.ff)}</td><th>فالوور سرمایه‌گذار</th><td>${n(item.vc)}</td></tr>
    <tr><th>امتیاز سورسا</th><td>${n(profile.sorsa)}</td><th>توییتر‌اسکور</th><td>${n(profile.twitter)}</td></tr>
    </tbody></table></div>
    <p class="muted">تغییر امتیاز سورسا در ۷ و ۳۰ روز ${bdi(item.score_change?.week ?? "—")} و ${bdi(item.score_change?.month ?? "—")}</p>
    ${item.score_history?.length ? '<canvas class="small-chart" id="score-history" role="img" aria-label="تاریخچهٔ امتیاز سورسا"></canvas>' : ""}
    <hr class="separator"><h3>فالوورهای برتر سورسا</h3>
    ${table(["حساب", "امتیاز سورسا", "فالوور"], top.map((p) => [link("@" + p.handle, "https://x.com/" + p.handle), n(p.sorsa), n(p.followers)]))}
    <hr class="separator"><h3>فعالیت اخیر</h3>
    ${recent.length ? `<ul class="list">${recent.slice(0, 6).map((p) => `<li><p>${h(p.text.slice(0,220))}</p>${link("دیدن پست", p.url)} · ${when(p.date)}</li>`).join("")}</ul>` : empty("با دریافت جزئیات، فعالیت اخیر اینجا می‌آید.")}`;
}

function dashboard() {
  const all = state.dashboard;
  const visible = all.filter((item) => !group || item.group === group);
  const profile = all.find((item) => item.handle === selectedProfile) || visible[0];
  const groups = {"": "همهٔ فهرست‌ها"};
  for (const entry of all) if (entry.group) groups[entry.group] = entry.group;
  const rows = visible.map((item) => [
    name(item.handle), h(item.group || "—"), n(item.profile.sorsa), n(item.profile.twitter), n(item.profile.ff),
    `<div class="actions">${action("جزئیات", "profile", {subject: item.handle})}${action("ویرایش", "edit-watch", {subject: item.handle})}${action("حذف", "remove", {kind: "watch", id: item.handle}, "danger")}</div>`,
  ]);
  const watchForm = `<form class="form" id="watch-form">
    ${handleInput("username", "نام کاربری", "", true)}${input("group", "نام فهرست", "", 'placeholder="سونیوم فرندز" maxlength="60"')}
    ${checkbox("replies","رصد ریپلای‌ها",true)}${checkbox("followers","رصد فالوورها")}${checkbox("following","رصد فالووینگ‌ها")}
    <button class="primary" type="submit">ذخیرهٔ حساب</button></form>`;
  return `<div class="metrics">
    <div class="panel metric"><span class="muted">حساب‌های تحت رصد</span><strong>${n(all.length)}</strong></div>
    <div class="panel metric"><span class="muted">درخواست سورسا امروز</span><strong>${n(state.used_today)} <small class="muted">از ${n(state.options.daily_limit)}</small></strong></div>
    <div class="panel metric"><span class="muted">آخرین بررسی</span><p id="last-scan">${when(state.last_scan?.at)}</p><p class="muted">${state.options.scheduled ? "بررسی دوره‌ای روشن است" : "بررسی دستی"}</p></div>
  </div><div class="stack">
    ${panel("حساب‌های تحت رصد", `<details><summary>افزودن یا ویرایش حساب</summary>${watchForm}</details>
    <label class="result">فهرست رصد<select id="watch-group">${options(groups, group)}</select></label>
    ${table(["حساب", "فهرست", "سورسا", "توییتر‌اسکور", "نسبت فالو ٪", "مدیریت"], rows)}`)}
    <div class="grid">
      <div class="stack">
        ${panel("امتیاز سورسا و نسبت فالو", '<canvas id="scatter" role="img" aria-label="مقایسهٔ امتیاز سورسا و نسبت فالو حساب‌ها"></canvas><p id="chart-note" class="muted"></p>', "نسبت فالو از تقسیم فالووینگ بر فالوور و ضرب در ۱۰۰ به‌دست می‌آید.")}
        ${panel("پیش‌نویس ریپلای", `<form class="form" id="draft-form">${input("url", "لینک پست یا ریپلای", "", 'dir="ltr" required maxlength="300" placeholder="https://x.com/…"')}<button class="primary" type="submit">ساخت پاسخ</button></form><div id="draft-result"></div><details class="result"><summary>پیش‌نویس‌های اخیر</summary>${state.drafts.length ? state.drafts.slice(0, 8).map(draftBlock).join("") : empty("پیش‌نویسی ثبت نشده.")}</details>`)}
      </div>
      ${panel("پروفایل و فالوورهای برتر", profileBlock(profile))}
    </div>
  </div>`;
}

function memberForm(kind = "followers", account = state.options.owner, locked = false) {
  const checks = ["ff", "sorsa", "twitter"].flatMap((field) => {
    const label = {ff:"نسبت فالو", sorsa:"امتیاز سورسا", twitter:"توییتر‌اسکور"}[field];
    return ["min","max"].map((side) => input(field+"_"+side, (side === "min" ? "حداقل " : "حداکثر ") + label, "", 'type="number" min="0" step="0.01" dir="ltr"'));
  }).join("");
  return `<form id="members-form" class="form">
    ${handleInput("account", "نام کاربری حساب", account, true)}
    ${locked ? `<input type="hidden" name="kind" value="${kind}">` : `<label>نوع فهرست<select name="kind">${options(directions, kind)}</select></label>`}
    ${input("q", "جست‌وجوی نام یا حساب", "", 'maxlength="120"')}
    ${checkbox("smart", "فقط حساب‌های SMART")}
    <details class="wide"><summary>فیلتر امتیاز و نسبت</summary><div class="form">${checks}</div></details>
    <div class="actions wide"><button class="primary" type="submit">نمایش و فیلتر</button>
    ${action("دریافت صفحه‌ها", "capture")}${action("شروع برداشت از ابتدا", "capture", {restart: "yes"})}</div>
    </form><div class="result" id="members-result"></div>`;
}

function personal() {
  const owner = state.options.owner;
  if (!owner) return panel("حساب شخصی", `<p>نام کاربری‌ات را در <a href="#settings">تنظیمات</a> ثبت کن تا شبکه و کامنت‌های حساب خودت را ببینی.</p>`);
  return `<div class="stack">${panel("شبکهٔ " + name(owner), memberForm("followers", owner))}
  ${panel("کامنت‌های حساب من", `<p class="muted">آخرین ریپلای‌های عمومی خطاب به حساب تو در تاریخچه ذخیره می‌شوند.</p><div class="actions result">${scanButton("بررسی ریپلای‌های دریافتی", "incoming", owner)}<a class="button" href="#history">باز کردن تاریخچه</a></div>`)}</div>`;
}

function comparePage() {
  return `<div class="stack">
    ${panel("پیشنهاد آنفالو", memberForm("nonfollowers", state.options.owner, true), "پیشنهادها با فهرست کامل فالوورها و فالووینگ‌ها ساخته می‌شوند. حساب‌های دارای امتیاز پایین‌تر اول می‌آیند.")}
    ${panel("اشتراک دو حساب", `<form class="form" id="compare-form">
      ${handleInput("left","حساب اول",state.options.owner,true)}${handleInput("right","حساب دوم","",true)}
      <label>نوع اشتراک<select name="kind">${options({followers:"فالوورهای مشترک",following:"فالووینگ‌های مشترک"},"followers")}</select></label>
      <div class="actions wide"><button type="submit" class="primary">مقایسه</button>
      ${action("برداشت حساب اول","compare-capture",{side:"left"})}${action("برداشت حساب دوم","compare-capture",{side:"right"})}</div></form><div id="compare-result" class="result"></div>`)}
  </div>`;
}

function historyPage() {
  return `<div class="stack">
    ${panel("رویدادهای رصد", `<form id="history-form" class="form">
      <label>رویداد<select name="type">${options({"":"همه",...eventLabels},"")}</select></label>
      ${handleInput("account","حساب تحت رصد")}${handleInput("actor","حساب اقدام‌کننده")}
      ${input("q","جست‌وجوی متن")}${input("start","از تاریخ میلادی","","type=date")}${input("end","تا تاریخ میلادی","","type=date")}
      <div class="actions wide"><button class="primary" type="submit">اعمال فیلتر</button>${action("دانلود CSV","csv")}</div>
      </form><div id="history-result" class="result"></div>`, "زمان فالو و آنفالو، زمان مشاهدهٔ تغییر بین دو برداشت است.")}
    ${panel("مقایسهٔ دستی فهرست‌ها", `<form id="snapshot-form" class="form">
      ${input("name","نام فهرست","","required maxlength=80")}
      <label class="wide">نام‌های کاربری<textarea name="handles" dir="ltr" placeholder="@account1&#10;@account2"></textarea></label>
      <button class="primary" type="submit">ذخیره و مقایسه</button></form><div id="snapshot-result" class="result"></div>`,
    "بار اول فهرست پایه ذخیره می‌شود. بار بعد همان نام را وارد کن تا اضافه‌شده‌ها و حذف‌شده‌ها را ببینی.")}
  </div>`;
}

function alertsPage() {
  const channelOptions = {"":"فقط داخل پنل"};
  for (const item of state.channels) channelOptions[item.id] = item.name;
  const rules = state.rules.map((rule) => [
    h(rule.name), h(eventLabels[rule.type]), name(rule.account), name(rule.actor), h(rule.keyword || "—"), h(channelOptions[rule.channel] || "—"),
    rule.enabled ? "روشن" : "خاموش",
    `<div class="actions">${action("ویرایش","edit-rule",{id:rule.id})}${action("حذف","remove",{kind:"rule",id:rule.id},"danger")}</div>`,
  ]);
  return `<div class="stack">
    ${panel("صندوق هشدارها", `<div class="actions result">${action(unreadOnly ? "نمایش همه" : "فقط خوانده‌نشده‌ها","toggle-unread")}${action("علامت‌گذاری همه به‌عنوان خوانده‌شده","mark-read")}</div><div id="alert-result" class="result"></div>`)}
    ${panel("قوانین هشدار", `<form id="rule-form" class="form"><input type="hidden" name="id">
      ${input("name","نام قانون","","required maxlength=70")}
      <label>رویداد<select name="type">${options(eventLabels,"reply")}</select></label>
      ${handleInput("account","برای حساب")}${handleInput("actor","فقط از این حساب")}
      ${input("keyword","عبارت داخل ریپلای","","maxlength=120")}
      <label>مقصد<select name="channel">${options(channelOptions,"")}</select></label>
      ${checkbox("draft","پیش‌نویس همراه اعلان تلگرام")}${checkbox("enabled","فعال",true)}
      <button class="primary" type="submit">ذخیرهٔ قانون</button></form>
      ${table(["نام", "نوع", "برای حساب", "از حساب", "عبارت", "مقصد", "وضعیت", "مدیریت"],rules)}`)}
  </div>`;
}

function settingsPage() {
  const o = state.options;
  return `<div class="stack">
    <div class="actions">${action("باز کردن همهٔ تنظیمات","open-settings")}</div>
    <details class="panel" open><summary>حساب و زمان بررسی</summary>
    <form id="options-form" class="form">${handleInput("owner","حساب شخصی",o.owner)}
      ${input("public_url","آدرس HTTPS سرویس",o.public_url,'dir="ltr" type="url" maxlength="500"')}
      ${input("model","مدل OpenRouter",o.model,'dir="ltr" required maxlength="150"')}
      ${input("interval","فاصلهٔ بررسی به دقیقه",o.interval,'dir="ltr" type="number" min="3" max="1440" required')}
      ${input("scan_limit","حد درخواست سورسا در هر بررسی",o.scan_limit,'dir="ltr" type="number" min="1" max="100" required')}
      ${input("daily_limit","حد درخواست سورسا در روز",o.daily_limit,'dir="ltr" type="number" min="1" max="50000" required')}
      ${checkbox("scheduled","بررسی دوره‌ای",o.scheduled)}${checkbox("telegram_enabled","ارسال اعلان به مقصدهای تلگرام",o.telegram_enabled)}
      ${checkbox("browser_sound","صدای هشدار داخل پنل",o.browser_sound)}${action("آزمایش صدا","test-sound")}
      <button class="primary" type="submit">ذخیرهٔ تنظیمات</button></form></details>
    <details class="panel"><summary>کلیدهای سرویس</summary>
      <p class="muted">وضعیت ثبت کلیدها و سهمیهٔ سورسا را اینجا ببین.</p>
      ${Object.entries(services).map(([service,label])=>`<form class="form secret-form" data-service="${service}">
      ${input("key",h(label) + (state.keys[service] ? " · ثبت شده" : " · ثبت نشده"),"",'dir="ltr" type="password" maxlength="500" autocomplete="off"')}
      <div class="actions"><button type="submit">ذخیرهٔ کلید</button>${action("پاک کردن کلید ثبت‌شده","clear-key",{service})}</div></form>`).join("")}
      <div class="actions result">${scanButton("دیدن سهمیهٔ سورسا","usage","")}</div>
    </details>
    <details class="panel"><summary>مقصدهای تلگرام و صدای هشدار</summary>
      <form class="form" id="channel-form"><input type="hidden" name="id">
      ${input("name","نام مقصد","","required maxlength=70")}
      ${input("chat","شناسهٔ عددی چت یا نام کانال","","dir=ltr required maxlength=32")}
      ${checkbox("silent","اعلان بی‌صدا")}
      <button class="primary" type="submit">ذخیرهٔ مقصد</button></form>
      ${table(["مقصد","شناسه","اعلان","مدیریت"],state.channels.map(c=>[
        h(c.name),bdi(c.chat),c.silent?"بی‌صدا":"عادی",
        `<div class="actions">${action("ویرایش","edit-channel",{id:c.id})}${action("حذف","remove",{kind:"channel",id:c.id},"danger")}</div>`,
      ]))}
    </details>
    <details class="panel"><summary>پست‌های هدف برای رصد کامنت</summary>
      <form class="form" id="target-form">${input("url","لینک پست","","dir=ltr required maxlength=300")}
      <button class="primary" type="submit">افزودن پست</button></form>
      ${table(["پست","مدیریت"],state.targets.map(p=>[link("دیدن پست",p.url),`<div class="actions">${scanButton("خواندن ریپلای‌ها","comments",p.id)}${action("حذف","remove",{kind:"target",id:p.id},"danger")}</div>`]))}
    </details>
    <details class="panel"><summary>پشتیبان ابری و بازیابی</summary>
      <p class="muted">دادهٔ همین پنل از دستگاه‌های مختلف قابل دسترسی است. نسخهٔ پشتیبان با عبارت عبور رمزگذاری می‌شود.</p>
      <form class="form" id="backup-form">
      ${input("passphrase","عبارت عبور بکاپ","","type=password minlength=12 maxlength=250 required autocomplete=new-password")}
      <button type="submit">دانلود بکاپ رمزگذاری‌شده</button></form>
      <form class="form" id="restore-form">
      ${input("file","فایل بکاپ","","type=file accept=application/json,.json required")}
      ${input("passphrase","عبارت عبور فایل","","type=password minlength=12 maxlength=250 required")}
      <button class="danger" type="submit">بازیابی بکاپ</button></form>
    </details>
  </div>`;
}

function getTab() {
  const key = location.hash.slice(1);
  return Object.hasOwn(pages,key) ? key : "dashboard";
}

function chart(canvas, points, history = false) {
  if (!canvas) return;
  const width = Math.max(canvas.clientWidth, 220);
  const height = canvas.clientHeight;
  const scale = window.devicePixelRatio || 1;
  canvas.width = width * scale;
  canvas.height = height * scale;
  const ctx = canvas.getContext("2d");
  ctx.scale(scale,scale);
  ctx.font = "11px Tahoma, sans-serif";
  if (!points.length) {
    ctx.textAlign = "center";
    ctx.fillStyle = "#526b73";
    ctx.fillText("امتیاز حساب‌ها پس از بررسی نمایش داده می‌شود", width/2, height/2);
    return;
  }
  const bounds = {left: 40, right: width-16, top: 12, bottom: height-28};
  const maxX = Math.max(history ? 1 : 100, ...points.map(p=>p.x));
  const maxY = Math.max(history ? 10 : 100, ...points.map(p=>p.y));
  ctx.strokeStyle = "#d7e5e1";
  ctx.fillStyle = "#51636d";
  for (let i = 0; i <= 4; i++) {
    const y = bounds.bottom - (bounds.bottom-bounds.top)*i/4;
    ctx.beginPath();
    ctx.moveTo(bounds.left,y);
    ctx.lineTo(bounds.right,y);
    ctx.stroke();
    ctx.fillText(n(maxY*i/4),4,y+4);
  }
  if (!history) ctx.fillText("نسبت فالو ٪",bounds.left,bounds.bottom+20);
  const position = (p) => [bounds.left + (bounds.right-bounds.left)*p.x/maxX, bounds.bottom - (bounds.bottom-bounds.top)*p.y/maxY];
  if (history) {
    ctx.strokeStyle = "#006c65";
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p,i) => i ? ctx.lineTo(...position(p)) : ctx.moveTo(...position(p)));
    ctx.stroke();
  }
  ctx.fillStyle = "#006c65";
  for (const p of points) {
    const [x,y] = position(p);
    ctx.beginPath();
    ctx.arc(x,y,history ? 3 : 5,0,Math.PI*2);
    ctx.fill();
  }
}

function renderCharts() {
  const visible = state.dashboard.filter(item=>!group || item.group===group);
  const profiles = visible.filter(item=>Number.isFinite(item.profile.sorsa) && Number.isFinite(item.profile.ff));
  chart($("#scatter"), profiles.map(item=>({x:item.profile.ff,y:item.profile.sorsa})));
  if ($("#chart-note")) $("#chart-note").textContent = profiles.length ? n(profiles.length) + " حساب دارای هر دو مقدار" : "";
  const history = (state.dashboard.find(i=>i.handle===selectedProfile) || visible[0])?.score_history || [];
  chart($("#score-history"),history.map((item,i)=>({x:i,y:item.score})),true);
}

async function render() {
  const tab = getTab();
  $("#title").textContent = pages[tab][0];
  $("#subheading").textContent = pages[tab][1];
  document.querySelectorAll("nav a").forEach(a=>{
    if (a.hash === "#" + tab) a.setAttribute("aria-current","page");
    else a.removeAttribute("aria-current");
  });
  const renderers = {
    dashboard, personal, followers:()=>panel("فهرست فالوورها و فالووینگ‌ها",memberForm()),
    compare:comparePage, history:historyPage, alerts:alertsPage, settings:settingsPage,
  };
  $("#screen").innerHTML = renderers[tab]();
  if (tab === "dashboard") renderCharts();
  if (tab === "history") {
    await showHistory();
    const target = $("#snapshot-result");
    const saved = await api("snapshots");
    if (target?.isConnected && !target.hasChildNodes() && saved.length) {
      target.innerHTML = saved.slice(0,8).map(snapshotBlock).join("");
    }
  }
  if (tab === "alerts") await showAlerts();
}

function formValues(form) {
  const data = Object.fromEntries(new FormData(form));
  form.querySelectorAll("input[type=checkbox]").forEach(field=>data[field.name]=field.checked);
  form.querySelectorAll("input[type=number]").forEach(field=>data[field.name]=field.value==="" ? "" : Number(field.value));
  return data;
}

function query(data) {
  return new URLSearchParams(Object.entries(data).filter(([,v])=>v!=="" && v!==undefined)).toString();
}

function pageButtons(key, page, next) {
  return `<div class="actions result">${page>1?action("صفحهٔ قبل",key,{page:page-1}):""}${next?action("صفحهٔ بعد",key,{page:page+1}):""}</div>`;
}

async function showMembers(page=1) {
  const form=$("#members-form");
  const data=formValues(form);
  if (!data.account) throw new Error("نام کاربری را وارد کن.");
  const result=await api("members?" + query({...data,page}));
  const target=$("#members-result");
  if (!target) return;
  const note = result.complete ? "فهرست ثبت‌شده" : "برداشت ناقص است. برای تکمیل دوباره «دریافت صفحه‌ها» را بزن.";
  const records=result.rows.map(p=>[
    link("@" + p.handle,"https://x.com/" + p.handle),h(p.name),n(p.followers),n(p.following),
    n(p.ff),n(p.sorsa),n(p.twitter),p.smart?"بله":"—",
  ]);
  target.innerHTML=`<p class="muted">${h(note)} · ${n(result.total)} نتیجه ${result.at?" · "+when(result.at):""}
    ${result.pending!==null?" · برداشت جدید: "+n(result.pending)+" حساب":""}</p>
    ${table(["حساب","نام","فالوور","فالووینگ","نسبت ٪","سورسا","توییتر‌اسکور","SMART"],records)}
    ${result.rows.length ? action("دریافت امتیاز حساب‌های این صفحه", "enrich", {users:result.rows.slice(0,50).map(p=>p.handle).join(",")}) : ""}
    ${pageButtons("members-page",page,page*100<result.total)}`;
}

async function showHistory(page=1) {
  const data=formValues($("#history-form"));
  const result=await api("history?" + query({...data,page}));
  if (!$("#history-result")) return;
  const records=result.rows.map(e=>[
    h(eventLabels[e.type]),name(e.account),name(e.actor),when(e.at),
    h(e.direction ? directions[e.direction] : e.text.slice(0,240)),
    `<div class="actions">${link("منبع",e.url,"button")}${e.type==="reply"?action("پیش‌نویس","draft",{url:e.url}):""}</div>`,
  ]);
  $("#history-result").innerHTML=table(["رویداد","حساب","طرف مقابل","زمان","جزئیات","اقدام"],records)+pageButtons("history-page",page,result.more);
}

function snapshotBlock(item) {
  return `<div class="alert result"><strong>${h(item.name)}</strong> · ${when(item.at)}
  <p>تعداد ${n(item.handles.length)} ${item.before?"· مشترک "+n(item.shared):"· فهرست پایه"}</p>
  ${item.before?`<p>اضافه‌شده ${n(item.added.length)} · ${item.added.slice(0,100).map(name).join(" ، ") || "—"}</p>
  <p>حذف‌شده ${n(item.removed.length)} · ${item.removed.slice(0,100).map(name).join(" ، ") || "—"}</p>`:""}
  <div class="actions result">${action("دانلود مقایسهٔ کامل","snapshot-download",{name:item.name})}</div></div>`;
}

async function showAlerts() {
  const data=await api("alerts?" + query({page:alertPage,unread:unreadOnly}));
  if (!$("#alert-result")) return;
  const statuses={pending:"در صف",sending:"در حال ارسال",sent:"ارسال شد",error:"خطای ارسال",paused:"متوقف",canceled:"لغو شده",unknown:"وضعیت ارسال نامشخص"};
  $("#alert-result").innerHTML=data.rows.length?`<div class="cards">${data.rows.map(item=>{
    const e=item.event;
    return `<article class="alert ${item.read?"":"unread"}"><div class="row"><h3>${h(item.rule)} · ${h(eventLabels[e.type])}</h3><span class="muted">${when(e.at)}</span></div>
    <p>${name(e.actor)} برای ${name(e.account)} ${item.delivery?"· "+h(statuses[item.delivery] || item.delivery):""}</p>
    <p>${h(e.text.slice(0,500))}</p>${item.draft_error?`<p class="error">${h(item.draft_error)}</p>`:""}
    ${item.delivery_error?`<p class="error">${h(item.delivery_error)}</p>`:""}
    <div class="actions result">${link("باز کردن منبع",e.url,"button")}
    ${item.read?"":action("خواندم","mark-read",{id:item.id})}
    ${e.type==="reply" && !item.draft?action("ساخت پاسخ","draft",{url:e.url}):""}</div>${draftBlock(item.draft)}</article>`;
  }).join("")}</div>`+pageButtons("alerts-page",alertPage,alertPage*50<data.total):empty("هشداری برای نمایش نیست.");
}

function scanMessage(result) {
  const parts=[`بررسی با ${n(result.requests)} درخواست سورسا انجام شد.`];
  for (const item of result.results) {
    if (item.mode==="usage") {
      parts.push(`سهمیهٔ باقی‌مانده ${n(item.remaining_requests)} از ${n(item.total_requests)}`);
    } else if (item.complete===false) {
      parts.push(`برداشت ${item.subject} ادامه دارد. برای صفحه‌های بعد دوباره بررسی کن.`);
    }
  }
  return parts.concat(result.errors || []).join("\n");
}

async function refresh() {
  await loadState();
  await render();
}

async function showCompare(page=1) {
  const data=formValues($("#compare-form"));
  const result=await api("compare?"+query({...data,page}));
  if (!$("#compare-result")) return;
  $("#compare-result").innerHTML=`<p>${n(result.total)} حساب مشترک از ${n(result.left)} و ${n(result.right)} حساب</p>`
    + table(["نام کاربری","نام"],result.rows.map(p=>[name(p.handle),h(p.name)]))
    + pageButtons("compare-page",page,page*100<result.total);
}

function allowSound() {
  if (!window.AudioContext) return Promise.resolve();
  audio ??= new AudioContext();
  return audio.resume();
}

function chime() {
  if (audio?.state !== "running") return;
  const tone=audio.createOscillator();
  const gain=audio.createGain();
  const start=audio.currentTime;
  tone.frequency.value=660;
  gain.gain.setValueAtTime(.08,start);
  gain.gain.exponentialRampToValueAtTime(.001,start+.25);
  tone.connect(gain).connect(audio.destination);
  tone.start(start);
  tone.stop(start+.26);
}

function fillForm(form, data) {
  form.closest("details")?.setAttribute("open","");
  for (const [key,value] of Object.entries(data)) {
    const field=form.elements.namedItem(key);
    if (!field) continue;
    if (field.type==="checkbox") field.checked=value;
    else field.value=value ?? "";
  }
  form.querySelector("input:not([type=hidden])")?.focus();
  form.scrollIntoView({block:"nearest"});
}

async function runScan(mode, subject, restart=false) {
  notice("در حال دریافت داده از منبع…");
  const result=await api("scan",{mode,subject,restart});
  await loadState();
  if (getTab()==="dashboard") await render();
  if ($("#members-form") && $("#members-form").elements.account.value) await showMembers();
  notice(scanMessage(result), Boolean(result.errors?.length));
}

function download(filename, data) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(data)],{type:"application/json"}));
  const a=document.createElement("a");
  a.href=url;
  a.download=filename;
  a.click();
  setTimeout(()=>URL.revokeObjectURL(url),2000);
}

document.addEventListener("submit", (event)=>{
  event.preventDefault();
  const form=event.target;
  const data=formValues(form);
  work(event.submitter,async()=>{
    switch(form.id) {
      case "login-form":
        await api("login",data);
        form.reset();
        $("#login-error").textContent="";
        await refresh();
        return;
      case "watch-form": await api("watch",data); await refresh(); break;
      case "members-form": await showMembers(); return;
      case "compare-form": await showCompare(); return;
      case "history-form": await showHistory(); return;
      case "snapshot-form": {
        const result=await api("snapshot",data);
        $("#snapshot-result").innerHTML=snapshotBlock(result);
        return;
      }
      case "draft-form": {
        const result=await api("draft",data);
        $("#draft-result").innerHTML=draftBlock(result);
        return;
      }
      case "options-form":
        if (data.browser_sound) allowSound().catch(()=>{});
        await api("options",data);
        await refresh();
        break;
      case "channel-form": await api("channel",data); await refresh(); break;
      case "rule-form": await api("rule",data); await refresh(); break;
      case "target-form": await api("target",data); await refresh(); break;
      case "backup-form":
        download("agant-x-radar-backup.json",await api("backup",{passphrase:data.passphrase}));
        form.reset();
        notice("نسخهٔ رمزگذاری‌شده آماده شد.");
        return;
      case "restore-form": {
        if (!data.file?.size || data.file.size>32*1024**2) throw new Error("فایل پشتیبان معتبر انتخاب کن.");
        const envelope=JSON.parse(await data.file.text());
        if (!confirm("داده‌های داخل این بکاپ جایگزین داده‌های فعلی شود؟")) return;
        await api("restore",{passphrase:data.passphrase,envelope});
        await refresh();
        notice("بکاپ بازیابی شد. برای ادامهٔ رصد و اعلان، آن‌ها را در تنظیمات فعال کن.");
        return;
      }
      default:
        if (!form.classList.contains("secret-form")) return;
        if (!data.key) throw new Error("کلید سرویس را وارد کن.");
        await api("secret",{service:form.dataset.service,key:data.key.trim()});
        form.reset();
        await loadState();
        form.querySelector("label").firstChild.textContent=services[form.dataset.service]+" · ثبت شده";
    }
    notice("ذخیره شد.");
  });
});

document.addEventListener("click", (event)=>{
  const button=event.target.closest("[data-action]");
  if (!button) return;
  const d=button.dataset;
  work(button,async()=>{
    switch(d.action) {
      case "refresh": await refresh(); notice("پنل به‌روز شد."); break;
      case "logout": await api("logout",{}); showLogin(); break;
      case "scan": await runScan(d.mode,d.subject || "",d.restart==="yes"); break;
      case "profile": selectedProfile=d.subject; await render(); break;
      case "edit-watch": {
        const item=state.watch.find(i=>i.handle===d.subject);
        fillForm($("#watch-form"),{...item,username:item.handle});
        break;
      }
      case "edit-rule": fillForm($("#rule-form"),state.rules.find(i=>i.id===d.id)); break;
      case "edit-channel": fillForm($("#channel-form"),state.channels.find(i=>i.id===d.id)); break;
      case "remove":
        if (confirm("این مورد حذف شود؟")) {
          await api("remove",{kind:d.kind,id:d.id});
          await refresh();
          notice("حذف شد.");
        }
        break;
      case "draft": {
        const draft=await api("draft",{url:d.url,regenerate:d.regenerate==="yes"});
        let target=$("#draft-result");
        if (!target) {
          target=document.createElement("div");
          button.closest(".actions").after(target);
        }
        target.innerHTML=draftBlock(draft);
        target.scrollIntoView({block:"nearest"});
        break;
      }
      case "copy": await navigator.clipboard.writeText(d.text); notice("متن پاسخ کپی شد."); break;
      case "capture": {
        const data=formValues($("#members-form"));
        if (data.kind==="nonfollowers") {
          notice("در حال بررسی فالوورها و فالووینگ‌ها…");
          const followers=await api("scan",{mode:"followers",subject:data.account,restart:d.restart==="yes"});
          const following=await api("scan",{mode:"following",subject:data.account,restart:d.restart==="yes"});
          await loadState();
          await showMembers();
          notice(scanMessage(followers)+"\n"+scanMessage(following));
        } else await runScan(data.kind,data.account,d.restart==="yes");
        break;
      }
      case "members-page": await showMembers(Number(d.page)); break;
      case "compare-capture": {
        const data=formValues($("#compare-form"));
        if (!data[d.side]) throw new Error("نام کاربری حساب را وارد کن.");
        await runScan(data.kind,data[d.side]);
        break;
      }
      case "compare-page": await showCompare(Number(d.page)); break;
      case "snapshot-download": download("agant-snapshot.json",await api("snapshot?"+query({name:d.name}))); break;
      case "test-sound": await allowSound(); chime(); break;
      case "enrich": {
        const result=await api("enrich",{handles:d.users.split(",")});
        await showMembers();
        notice(`امتیازها به‌روز شدند. درخواست سورسا: ${n(result.requests)}`+(result.errors.length?"\n"+result.errors.join("\n"):""),!!result.errors.length);
        break;
      }
      case "history-page": await showHistory(Number(d.page)); break;
      case "csv": {
        const data=formValues($("#history-form"));
        const a=document.createElement("a");
        a.href="./api/history?"+query({...data,export:true});
        a.download="agant-x-history.csv";
        a.click();
        break;
      }
      case "toggle-unread": unreadOnly=!unreadOnly; alertPage=1; await render(); break;
      case "alerts-page": alertPage=Number(d.page); await showAlerts(); break;
      case "mark-read": await api("alerts/read",{id:d.id || ""}); await loadState(); await showAlerts(); break;
      case "open-settings": document.querySelectorAll("#screen details").forEach(detail=>detail.open=true); break;
      case "clear-key":
        if (confirm("کلید ذخیره‌شده در پنل پاک شود؟")) {
          await api("secret",{service:d.service,key:""});
          await refresh();
          notice("کلید ذخیره‌شده پاک شد.");
        }
        break;
    }
  });
});
document.addEventListener("change",(event)=>{
  if (event.target.id==="watch-group") {
    group=event.target.value;
    selectedProfile="";
    work(null,render);
  }
});
window.addEventListener("hashchange",()=>{if(state && $("#login").hidden){notice("");work(null,render);}});
window.addEventListener("resize",()=>{if(state && getTab()==="dashboard")renderCharts();});
document.addEventListener("pointerdown",()=>{if(state?.options.browser_sound && !audio)allowSound().catch(()=>{});});
setInterval(async()=>{
  if (!state || !$("#login").hidden) return;
  try {
    const data=await api("pulse");
    const changed=data.unread!==state.unread;
    if (data.unread>state.unread && state.options.browser_sound) chime();
    state.unread=data.unread;
    $("#unread").textContent=data.unread?n(data.unread):"";
    if ($("#last-scan")) $("#last-scan").textContent=when(data.last_scan?.at);
    if (changed && getTab()==="alerts") await showAlerts();
  } catch {}
}, 15000);
refresh().catch((error)=>{showLogin(); if(error.message!=="وارد پنل شو.")$("#login-error").textContent=error.message;});
