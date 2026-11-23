import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {parseLocalCapture, pickCaptureRecord, COURSE_FIELDS} from "../server/static/capture-core.mjs";

const course = {xkkh: "(2026-2027-1)-000000001-1", xqj: "1", dsz: "2", xxq: "秋冬", kcb: "测试课程<br>1-8周<br>教师<br>教室", djj: "1", skcd: "2"};
const snapshot = () => ({format: "zjuical-local-capture-v1", username: "3000000000", config: "config.json", export: "courses", courses: [{year: "2026-2027", term: "秋", rows: [course]}], exams: null});
test("local captures contain only course fields and reject cookies or control characters", () => {
  assert.deepEqual(pickCaptureRecord({...course, JSESSIONID: "sensitive", password: "sensitive"}, COURSE_FIELDS), course);
  assert.equal(parseLocalCapture(JSON.stringify(snapshot())).username, "3000000000");
  for (const change of [data => data.password = "sensitive", data => data.courses[0].rows[0] = {...course, Cookie: "sensitive"}, data => data.courses[0].rows[0] = {...course, kcb: "bad\nATTENDEE:injected"}]) {
    const data = snapshot(); change(data);
    assert.throws(() => parseLocalCapture(JSON.stringify(data)));
  }
});

async function runCapture({failExam = false, switchAccount = false} = {}) {
  const elements = [], requests = [], clipboard = [];
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.value = ""; elements.push(this); }
    append(child) {
      this.children.push(child);
      if (this.tag === "select" && (child.selected || this.children.length === 1)) this.value = child.value;
    }
    setAttribute() {}
  }
  const body = new Element("body");
  const document = {body, createElement: tag => new Element(tag), getElementById: id => elements.find(element => element.id === id)};
  Object.defineProperty(document, "cookie", {get() { throw new Error("Must not read cookies"); }});
  const configuration = [{file: "config.json", label: "当前秋冬", terms: [{year: "2026-2027", term: "秋", query: "1|秋"}, {year: "2026-2027", term: "冬", query: "1|冬"}]}];
  const script = fs.readFileSync(new URL("../server/capture.user.js", import.meta.url), "utf8")
    .replace("/*__CAPTURE_CORE__*/", fs.readFileSync(new URL("../server/static/capture-core.mjs", import.meta.url), "utf8").replace(/^export (const|function) /gm, "$1 "))
    .replace("/*__CAPTURE_CONFIGS__*/", JSON.stringify(configuration))
    .replace("/*__CAPTURE_IMPORT_URL__*/", JSON.stringify("https://calendar.example.test"));
  const context = {document, location: {hostname: "zdbk.zju.edu.cn", origin: "https://zdbk.zju.edu.cn"}, URL, URLSearchParams, AbortController,
    navigator: {clipboard: {writeText: text => { clipboard.push(text); return Promise.resolve(); }}},
    setTimeout: (fn, ms) => { if (ms < 20000) queueMicrotask(fn); return {}; }, clearTimeout() {},
    fetch: async (url, options) => {
      requests.push({url, options});
      assert.equal(new URL(url).origin, "https://zdbk.zju.edu.cn");
      assert(new URL(url).pathname.startsWith("/jwglxt/"));
      assert.equal(options.credentials, "same-origin");
      assert.equal(options.redirect, "error");
      assert(options.referrer.startsWith("https://zdbk.zju.edu.cn/jwglxt/"));
      if (url.includes("xskscx")) {
        if (failExam) throw new Error("School unavailable");
        return {ok: true, json: async () => ({items: [], totalResult: 0})};
      }
      return {ok: true, json: async () => ({xh: switchAccount && requests.length > 1 ? "3000000001" : "3000000000", JWTUser: "discard", kbList: [{...course, password: "discard"}]})};
    }};
  Object.defineProperty(context, "localStorage", {get() { throw new Error("Must not read browser storage"); }});
  vm.runInNewContext(script, context, {timeout: 1000});
  await elements.find(element => element.textContent === "采集当前登录账号的课表").onclick();
  return {elements, requests, clipboard};
}

test("userscript uses same-origin authenticated requests, completes all queries and waits for explicit copy", async () => {
  const {elements, requests, clipboard} = await runCapture();
  assert.equal(requests.length, 3);
  const text = elements.find(element => element.tag === "textarea").value;
  const data = parseLocalCapture(text);
  assert.equal(data.courses.length, 2);
  assert(!text.includes("discard"));
  assert.equal(clipboard.length, 0);
  await elements.find(element => element.textContent === "复制课表数据").onclick();
  assert.deepEqual(clipboard, [text]);
});

test("failed exams or an account switch never expose a partial successful snapshot", async () => {
  for (const options of [{failExam: true}, {switchAccount: true}]) {
    const {elements, clipboard} = await runCapture(options);
    assert.equal(elements.find(element => element.tag === "textarea").value, "");
    assert.equal(elements.find(element => element.textContent === "复制课表数据").hidden, true);
    assert.equal(clipboard.length, 0);
  }
});
