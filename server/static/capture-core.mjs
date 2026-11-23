// Shared by the website and the self-contained userscript. No cookies or network access.
export const COURSE_FIELDS = ["xkkh", "xqj", "dsz", "xxq", "kcb", "djj", "skcd"];
export const EXAM_FIELDS = ["xkkh", "kcmc", "xf", "kssj", "jsmc", "zwxh", "qzkssj", "qzjsmc", "qzzwxh"];
export function pickCaptureRecord(row, fields) {
  const result = {};
  if (!row || typeof row !== "object") throw new Error("学校返回的记录无效。");
  for (const field of fields) {
    if (row[field] !== undefined && row[field] !== null) {
      if (!["string", "number"].includes(typeof row[field])) throw new Error("学校记录格式已变化。");
      result[field] = String(row[field]).replace(/[\x00-\x1f]/g, " ");
    }
  }
  return result;
}
function checkRows(rows, fields, maximum) {
  if (!Array.isArray(rows) || rows.length > maximum) throw new Error("采集记录数量异常，请重新采集。");
  for (const row of rows) {
    if (!row || typeof row !== "object" || Array.isArray(row) ||
        Object.keys(row).some(key => !fields.includes(key)) ||
        Object.values(row).some(value => typeof value !== "string" || value.length > 4096 || /[\x00-\x1f]/.test(value))) {
      throw new Error("采集内容包含无关字段或无效文本，请使用采集脚本重新生成。");
    }
  }
}
export function parseLocalCapture(text) {
  if (typeof text !== "string" || text.length > 524288) throw new Error("采集内容过大。");
  let data;
  try { data = JSON.parse(text); } catch { throw new Error("请粘贴采集脚本生成的课表 JSON。"); }
  const keys = ["format", "username", "config", "export", "courses", "exams"];
  if (!data || typeof data !== "object" || Array.isArray(data) ||
      Object.keys(data).length !== keys.length || Object.keys(data).some(key => !keys.includes(key)) ||
      data.format !== "zjuical-local-capture-v1" || typeof data.username !== "string" ||
      !/^[0-9]{5,20}$/.test(data.username) || typeof data.config !== "string" ||
      !/^config[\w.-]*\.json$/.test(data.config) || !["all", "courses"].includes(data.export) ||
      !Array.isArray(data.courses) || !data.courses.length || data.courses.length > 8) {
    throw new Error("不是有效的本机课表快照，请重新采集。");
  }
  const seen = new Set();
  for (const term of data.courses) {
    if (!term || Object.keys(term).sort().join(",") !== "rows,term,year" ||
        typeof term.year !== "string" || !/^\d{4}-\d{4}$/.test(term.year) ||
        !["秋", "冬", "春", "夏", "短", "暑"].includes(term.term)) throw new Error("采集学期无效。");
    const key = term.year + ":" + term.term;
    if (seen.has(key)) throw new Error("采集学期重复。");
    seen.add(key);
    checkRows(term.rows, COURSE_FIELDS, 200);
    for (const row of term.rows) {
      if (Object.keys(row).length !== COURSE_FIELDS.length ||
          !row.xkkh || row.xkkh.length > 120 || !row.kcb.includes("<br>") ||
          !/^[1-7]$/.test(row.xqj) || !/^\d{1,2}$/.test(row.djj) || !/^\d{1,2}$/.test(row.skcd) ||
          Number(row.djj) < 1 || Number(row.skcd) < 1 || Number(row.djj) + Number(row.skcd) > 15) {
        throw new Error("课程字段不完整，请重新采集。");
      }
    }
  }
  if (data.export === "courses") {
    if (data.exams !== null) throw new Error("仅课程快照包含意外考试数据。");
  } else {
    checkRows(data.exams, EXAM_FIELDS, 500);
  }
  return data;
}
