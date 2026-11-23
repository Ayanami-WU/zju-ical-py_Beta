// Parse locally. Never return the original export, global SSO cookies or unknown fields.
const allowed = ["username", "kind", "mode", "secret", "root_session", "teaching_token", "route"];
function validValue(value, maximum = 4096) {
  return typeof value === "string" && value.length <= maximum && /^[\x21-\x7e]*$/.test(value) && !/[;,"\\]/.test(value);
}
function validate(data) {
  if (!data || typeof data !== "object" || Array.isArray(data) ||
      Object.keys(data).some((key) => !allowed.includes(key)) || data.mode !== "token" ||
      !["ugrs", "grs"].includes(data.kind) ||
      (data.username !== undefined && (typeof data.username !== "string" || !/^[0-9]{5,20}$/.test(data.username))) ||
      !validValue(data.secret) || !data.secret.length ||
      ["root_session", "teaching_token", "route"].some((key) => data[key] !== undefined &&
        (!validValue(data[key], key === "route" ? 512 : 4096) || (data.kind === "grs" && data[key])))) {
    throw new Error("不是有效的教务专用会话，请重新导出 JSON。");
  }
  return Object.fromEntries(Object.entries(data).filter(([key]) => allowed.includes(key)));
}
export function parseTeachingSession(text) {
  if (typeof text !== "string" || text.length > 131072) throw new Error("导入内容过大，请只导出当前教务网站的 JSON。");
  let bundle;
  try { bundle = JSON.parse(text); }
  catch { throw new Error("请粘贴 Cookie-Editor 导出的 JSON，或导入本机助手的会话文件。"); }
  if (!Array.isArray(bundle)) {
    if (bundle?.format !== "zjuical-teaching-session-v1") throw new Error("无法识别的会话格式，请选择 JSON 导出。");
    return validate(bundle.credentials);
  }
  if (bundle.length > 500) throw new Error("Cookie 数量过多，请只导出当前教务网站。");
  const data = {kind: "ugrs", mode: "token"};
  for (const cookie of bundle) {
    if (!cookie || typeof cookie !== "object" || typeof cookie.domain !== "string" ||
        cookie.domain.replace(/^\./, "").toLowerCase() !== "zdbk.zju.edu.cn" ||
        cookie.partitionKey || cookie.firstPartyDomain ||
        (typeof cookie.expirationDate === "number" && cookie.expirationDate > 0 && cookie.expirationDate < Date.now() / 1000)) continue;
    const path = typeof cookie.path === "string" ? cookie.path.replace(/\/$/, "") : null;
    let key;
    if (cookie.name === "JSESSIONID" && path === "/jwglxt") key = "secret";
    else if (cookie.name === "JSESSIONID" && path === "") key = "root_session";
    else if (cookie.name === "JSESSIONTOKEN" && path === "") key = "teaching_token";
    else if (cookie.name === "route" && ["", "/jwglxt"].includes(path)) key = "route";
    if (!key) continue;
    if (data[key] !== undefined && data[key] !== cookie.value) throw new Error("存在不同分区或节点的重复会话，请只导出当前登录窗口的教务 Cookie。");
    data[key] = cookie.value;
  }
  if (!data.secret) throw new Error("没有找到 zdbk.zju.edu.cn 路径 /jwglxt 的 JSESSIONID，请先登录本科教务页面再导出。");
  return validate(data);
}
