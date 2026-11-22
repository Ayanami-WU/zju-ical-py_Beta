"use strict";
import {parseTeachingSession} from "./session-import.mjs";
import {parseLocalCapture} from "./capture-core.mjs";
const $ = (id) => document.getElementById(id);
let csrf = "", current = null, editing = false, pendingKey = false;
function show(id, visible) { $(id).hidden = !visible; }
function notify(message) { $("notice").textContent = message; show("notice", Boolean(message)); }
async function api(path, data) {
  const response = await fetch(`/api/${path}`, {
    method: data === undefined ? "GET" : "POST",
    credentials: "same-origin", cache: "no-store",
    headers: data === undefined ? {} : { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败，请稍后再试");
  if (result.csrf) csrf = result.csrf;
  return result;
}
function sourceHelp() {
  const snapshot = $("source").value === "snapshot";
  show("snapshot-fields", snapshot); show("token-fields", !snapshot);
  $("secret").required = !snapshot; $("username").required = !snapshot; $("capture-json").required = snapshot;
  $("kind").disabled = snapshot; if (snapshot) $("kind").value = "ugrs";
  $("consent-text").textContent = snapshot
    ? "同意上传并加密保存课表快照，用于替换本订阅的上次课表。本站不保存本机登录会话；以后更新需再次在手机采集并上传。"
    : "同意服务加密保存教务系统会话，用于定时更新。会话仍具有该系统的账户权限，过期后需重新提供；可随时删除。";
  $("form-help").textContent = snapshot ? "上传本机采集的课表，更新时需重新采集上传。" : "连接教务系统，使用专用会话后台更新。";
  $("submit").textContent = editing ? (snapshot ? "上传并替换快照" : "保存并重新同步") : "创建订阅";
  if (!$("capture-json").value) $("capture-preview").textContent = "请粘贴完整快照并核对学号、学期和记录数量。手机实机兼容性仍需验证。";
  $("consent").checked = false; modeHelp();
}
function readCapture() {
  const capture = parseLocalCapture($("capture-json").value);
  if (![...$("config").options].some(option => option.value === capture.config)) throw new Error("该快照学期不在当前服务配置中，请更新采集脚本。");
  $("config").value = capture.config; $("export").value = capture.export;
  $("capture-preview").textContent = `学号 ${capture.username} · ${$("config").selectedOptions[0].textContent} · ${capture.courses.reduce((sum, term) => sum + term.rows.length, 0)} 条课程记录 · ${capture.exams === null ? "未采集考试" : capture.exams.length + " 条考试记录"}。尚未上传。`;
  return capture;
}
$("source").onchange = sourceHelp;
$("read-capture").onclick = () => action(async () => { readCapture(); $("consent").checked = false; notify("请核对采集内容，并确认替换本订阅的课表。"); });
function modeHelp() {
  const graduate = $("kind").value === "grs";
  $("secret-label").textContent = graduate ? "② 研究生系统 X-Access-Token" : "② 本科教务 JSESSIONID（路径 /jwglxt）";
  $("secret").value = "";
  $("route").value = "";
  $("root-session").value = "";
  $("teaching-token").value = "";
  show("route-field", !graduate);
  show("root-session-field", !graduate);
  show("teaching-token-field", !graduate);
  show("graduate-note", graduate);
  $("official-login").href = graduate ? "https://zjuam.zju.edu.cn/cas/login?service=https%3A%2F%2Fyjsy.zju.edu.cn%2F" : "https://zjuam.zju.edu.cn/cas/login?service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html";
  $("token-help").textContent = graduate
    ? "登录 yjsy.zju.edu.cn，在浏览器开发者工具的网络请求中获取 X-Access-Token，仅粘贴值。过期后请在此更新。"
    : "在开发者工具 → 存储 / 应用 → Cookie → zdbk.zju.edu.cn，按路径分别填写两条 JSESSIONID，并提供同域的 JSESSIONTOKEN；不存在的可选项留空。不要提供全局 JWTUser、JSESSIONPREJSDM 或 zjuam 的 iPlanetDirectoryPro。";
  $("export").querySelector('[value="exams"]').disabled = graduate;
  if (graduate && $("export").value === "exams") $("export").value = "courses";
}
function render(result) {
  if (result.csrf) csrf = result.csrf;
  current = result.subscription;
  show("loading", false); show("login", false);
  show("setup", !current || editing); show("dashboard", Boolean(current) && !editing);
  if (!current) return;
  $("account").textContent = `${current.kind === "ugrs" ? "本科生" : "研究生"} · ${current.username} · ${current.options.config === "config.json" ? "当前学期" : current.options.config.replace("config.", "").replace(".json", "")}`;
  $("feed-url").value = current.url;
  $("apple-link").href = current.url.replace(/^https?:/, "webcal:");
  $("apple-link").setAttribute("aria-disabled", String(!current.ready));
  $("refresh").textContent = current.mode === "snapshot" ? "上传新快照" : "立即刷新";
  $("edit").textContent = current.mode === "snapshot" ? "更新快照 / 设置" : "更新凭据 / 设置";
  $("status-badge").textContent = current.mode === "snapshot" ? "本机采集" : current.paused ? "需要处理" : current.syncing ? "同步中" : current.error ? "更新失败" : current.ready ? "已连接" : "等待首次同步";
  const messages = { authentication_failed: "教务系统未接受当前会话，请检查同一浏览器的两条 JSESSIONID 及 JSESSIONTOKEN；会话也可能已经过期。", upstream_unavailable: "暂时无法连接学校系统，将稍后重试。", refresh_failed: "课表更新未成功，请检查凭据和学校系统状态。", timeout: "学校系统响应超时，将稍后重试。" };
  $("sync-status").textContent = current.error
    ? `${messages[current.error] || "更新失败。"}${current.ready ? " 仍可订阅上次成功的课表。" : ""}${current.paused ? " 连续失败后已暂停自动重试。" : ""}`
    : current.updated ? `上次成功更新：${new Date(current.updated * 1000).toLocaleString("zh-CN")}。课表将定时同步。` : "正在排队获取课表。首次成功后，即可在日历 App 中订阅。";
  if (current.mode === "snapshot") $("sync-status").textContent = `上次上传：${new Date(current.updated * 1000).toLocaleString("zh-CN")}。更新时请在学校页面重新采集，再向原订阅上传；云端不会自动访问学校。`;
  if (result.management_key) {
    $("management-key").value = result.management_key;
    $("saved-key").checked = false;
    pendingKey = true; show("key-box", true);
  }
}
async function action(work) {
  notify("");
  const buttons = [...document.querySelectorAll("button")];
  buttons.forEach((button) => { button.disabled = true; });
  try { await work(); } catch (error) { notify(error.message); }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}
$("kind").addEventListener("change", modeHelp);
function importSession(text) {
  const data = parseTeachingSession(text);
  $("kind").value = data.kind; modeHelp();
  if (data.username) $("username").value = data.username;
  $("secret").value = data.secret;
  $("root-session").value = data.root_session || "";
  $("teaching-token").value = data.teaching_token || "";
  $("route").value = data.route || "";
  $("consent").checked = false;
  notify("已在本机筛选并填写教务会话，尚未上传。请填写或检查学号和订阅设置，再同意保存并提交。");
}
$("session-file").addEventListener("change", () => action(async () => {
  const file = $("session-file").files[0];
  if (!file) return;
  try {
    if (file.size > 131072) throw new Error("会话文件过大，请只导出当前教务网站的 JSON。");
    importSession(await file.text());
  } finally { $("session-file").value = ""; }
}));
$("import-cookies").onclick = () => action(async () => {
  try { importSession($("cookies-json").value); }
  finally { $("cookies-json").value = ""; }
});
$("setup-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    if ($("source").value === "snapshot") {
      const capture = parseLocalCapture($("capture-json").value);
      if ($("config").value !== capture.config) throw new Error("采集学期与表单不一致，请先点击核对采集内容。");
      if (capture.export === "courses" && $("export").value !== "courses") throw new Error("本次没有采集考试，请选择仅课程或重新采集。");
      if (!capture.courses.some(term => term.rows.length) && !(capture.exams?.length) && !confirm("本次快照没有课程或考试，将清空本订阅的现有事件。确认上传？")) return;
      const result = await api("local-capture", {capture, export: $("export").value, consent: $("consent").checked});
      $("capture-json").value = ""; editing = false; render(result); return;
    }
    const data = { username: $("username").value.trim(), kind: $("kind").value, mode: "token", secret: $("secret").value, route: $("kind").value === "ugrs" ? $("route").value : "", root_session: $("kind").value === "ugrs" ? $("root-session").value : "", teaching_token: $("kind").value === "ugrs" ? $("teaching-token").value : "", config: $("config").value, export: $("export").value, consent: $("consent").checked };
    const result = await api(editing ? "settings" : "subscriptions", data);
    $("secret").value = ""; $("route").value = ""; $("root-session").value = ""; $("teaching-token").value = ""; editing = false; render(result);
  });
});
$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => { const result = await api("login", { key: $("management-input").value.trim() }); $("management-input").value = ""; render(result); });
});
$("show-login").onclick = () => { show("setup", false); show("login", true); };
$("back-setup").onclick = () => { show("login", false); show("setup", true); };
function startEdit() {
  editing = true; $("source").value = current.mode === "snapshot" ? "snapshot" : "token"; sourceHelp(); $("username").value = current.username; $("kind").value = current.kind;
  modeHelp(); $("export").value = current.options.export; $("config").value = current.options.config; $("consent").checked = false;
  $("form-title").textContent = current.mode === "snapshot" ? "更新课表快照" : "更新凭据与订阅设置"; $("submit").textContent = $("source").value === "snapshot" ? "上传并替换快照" : "保存并重新同步";
  show("recovery-entry", false); show("cancel-edit", true); render({ subscription: current });
}
$("edit").onclick = startEdit;
$("cancel-edit").onclick = () => { editing = false; $("secret").value = ""; render({ subscription: current }); };
$("refresh").onclick = () => action(async () => { if (current.mode === "snapshot") { startEdit(); notify("请粘贴重新采集的课表快照，原订阅链接会保留。"); return; } await api("refresh", {}); notify("已加入刷新队列，请稍候。后台会保留上次成功的课表。"); });
$("rotate").onclick = () => {
  if (confirm("重置后旧订阅链接立即失效，你需要在日历 App 中重新订阅。继续吗？")) action(async () => { render(await api("rotate", {})); notify("旧链接已撤销，请复制新的订阅链接。"); });
};
$("rotate-management").onclick = () => {
  if (confirm("重置管理密钥将使旧密钥与其他设备的管理会话失效。继续吗？")) action(async () => { render(await api("management-key", {})); });
};
$("saved-key").onchange = () => { pendingKey = !$("saved-key").checked; };
$("logout").onclick = () => {
  if (pendingKey) { notify("请先保存管理密钥，并勾选确认，以便之后找回订阅。"); return; }
  action(async () => { await api("logout", {}); location.reload(); });
};
$("delete").onclick = () => {
  if (confirm("确认删除订阅、凭据及缓存？订阅链接与管理密钥会立即失效。")) action(async () => { await api("delete", {}); pendingKey = false; location.reload(); });
};
$("apple-link").onclick = (event) => { if (!current?.ready) { event.preventDefault(); notify("首次同步成功后即可订阅，请稍候。"); } };
async function copy(id) {
  try { await navigator.clipboard.writeText($(id).value); notify("已复制。"); }
  catch { $(id).focus(); $(id).select(); notify("请复制已选中的内容。"); }
}
$("copy-url").onclick = () => copy("feed-url");
$("copy-key").onclick = () => copy("management-key");
window.addEventListener("beforeunload", (event) => { if (pendingKey) { event.preventDefault(); event.returnValue = ""; } });
async function bootstrap() {
  try {
    const configs = await api("configs");
    $("config").replaceChildren(...configs.configs.map((item) => {
      const option = document.createElement("option"); option.value = item.file; option.textContent = `${item.default ? "当前 · " : ""}${item.label}`; option.selected = item.default; return option;
    }));
    render(await api("session")); sourceHelp();
  } catch (error) { show("loading", false); notify(error.message); }
}
setInterval(async () => {
  if (current && !editing && document.visibilityState === "visible") {
    try { render(await api("session")); } catch (error) { notify(error.message); }
  }
}, 10000);
bootstrap();
