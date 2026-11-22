// ==UserScript==
// @name         ZJU Calendar 本机课表采集
// @namespace    https://github.com/Ayanami-WU/zju-ical-py_Beta
// @version      1.0.0
// @license      LGPL-2.1
// @description  在本科教务页面采集课表；不读取 Cookie、密码，不自动上传
// @match        https://zdbk.zju.edu.cn/jwglxt/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==
(() => {
  "use strict";
  if (location.hostname !== "zdbk.zju.edu.cn" || document.getElementById("zjuical-capture-panel")) return;
  /*__CAPTURE_CORE__*/
  const configurations = /*__CAPTURE_CONFIGS__*/;
  const importUrl = /*__CAPTURE_IMPORT_URL__*/;
  const panel = document.createElement("details");
  panel.id = "zjuical-capture-panel";
  panel.style.cssText = "position:fixed;bottom:12px;right:12px;z-index:2147483646;max-height:85vh;overflow:auto;width:min(92vw,360px);padding:12px;background:white;color:#172033;border:2px solid #1565c0;border-radius:12px;box-shadow:0 4px 24px #0003;font:16px/1.5 system-ui;box-sizing:border-box";
  const summary = document.createElement("summary"); summary.textContent = "ZJU Calendar 本机采集"; panel.append(summary);
  const note = document.createElement("p"); note.textContent = "在此读取课程与考试，不读取 Cookie 或密码，也不会自动上传。更新订阅时需要重新采集。"; panel.append(note);
  const configSelect = document.createElement("select"); configSelect.style.cssText = "width:100%;font:inherit;margin:6px 0";
  configurations.forEach(item => { const option = document.createElement("option"); option.value = item.file; option.textContent = item.label; option.selected = item.file === "config.json"; configSelect.append(option); });
  panel.append(configSelect);
  const scope = document.createElement("select"); scope.style.cssText = "width:100%;font:inherit;margin:6px 0";
  [["all", "课程与考试"], ["courses", "仅课程"]].forEach(([value, label]) => { const option = document.createElement("option"); option.value = value; option.textContent = label; scope.append(option); }); panel.append(scope);
  const collect = document.createElement("button"); collect.textContent = "采集当前登录账号的课表"; collect.type = "button"; collect.style.cssText = "font:inherit;padding:8px"; panel.append(collect);
  const status = document.createElement("p"); status.setAttribute("role", "status"); panel.append(status);
  const output = document.createElement("textarea"); output.readOnly = true; output.hidden = true; output.style.cssText = "box-sizing:border-box;width:100%;height:100px;font:14px monospace"; panel.append(output);
  const copy = document.createElement("button"); copy.textContent = "复制课表数据"; copy.type = "button"; copy.hidden = true; copy.style.cssText = "font:inherit;padding:8px;margin:8px 0"; panel.append(copy);
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(output.value); status.textContent = "已复制。回到订阅网页粘贴，并由你确认上传。"; }
    catch { output.focus(); output.select(); status.textContent = "请在已选中的文本上长按并复制，再回到订阅网页。"; }
  };
  const back = document.createElement("a"); back.href = importUrl; back.textContent = "返回订阅网页"; back.target = "_blank"; back.rel = "noopener noreferrer"; back.style.cssText = "display:block;padding:8px 0"; panel.append(back);
  const clear = document.createElement("button"); clear.textContent = "清空本页采集数据"; clear.type = "button"; panel.append(clear);
  clear.onclick = () => { output.value = ""; output.hidden = true; copy.hidden = true; status.textContent = "已清空本页数据；剪贴板请自行清理。"; };
  document.body.append(panel);

  async function query(path, values, referrer) {
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(path, {method: "POST", credentials: "same-origin", redirect: "error", cache: "no-store", signal: controller.signal,
        referrer: new URL(referrer, location.origin).href,
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}, body: new URLSearchParams(values)});
      if (!response.ok) throw new Error("学校未接受当前登录，请重新打开官方课表页登录。");
      let data = await response.json(); if (typeof data === "string") data = JSON.parse(data); return data;
    } finally { clearTimeout(timer); }
  }
  collect.onclick = async () => {
    collect.disabled = true; output.value = ""; output.hidden = true; copy.hidden = true;
    const selected = configurations.find(item => item.file === configSelect.value); const exportType = scope.value;
    try {
      const courses = []; let username = "";
      for (const term of selected.terms) {
        status.textContent = "正在本机采集 " + term.year + " " + term.term;
        const data = await query("/jwglxt/kbcx/xskbcx_cxXsKb.html", {xnm: term.year, xqm: term.query}, "/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151&layout=default");
        if (!Array.isArray(data.kbList) || typeof data.xh !== "string" || !/^[0-9]{5,20}$/.test(data.xh)) throw new Error("未获得完整课表或学号，请先在官方页面确认已登录。");
        if (username && username !== data.xh) throw new Error("采集期间账号发生变化，请重新采集。");
        username = data.xh;
        courses.push({year: term.year, term: term.term, rows: data.kbList.map(row => pickCaptureRecord(row, COURSE_FIELDS))});
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      let exams = null;
      if (exportType === "all") {
        status.textContent = "正在本机采集考试";
        const data = await query("/jwglxt/xskscx/kscx_cxXsgrksIndex.html?doType=query&gnmkdm=N509070&su=" + encodeURIComponent(username),
          {_search: "false", nd: String(Date.now()), "queryModel.showCount": "5000", "queryModel.currentPage": "1", "queryModel.sortName": "xkkh", "queryModel.sortOrder": "asc", time: "0"},
          "/jwglxt/xskscx/kscx_cxXsgrksIndex.html?gnmkdm=N509070&layout=default");
        if (!Array.isArray(data.items) || Number(data.totalResult || 0) > data.items.length) throw new Error("考试结果不完整；请检查官方考试页，或明确选择仅课程后重新采集。");
        exams = data.items.map(row => pickCaptureRecord(row, EXAM_FIELDS));
      }
      const text = JSON.stringify({format: "zjuical-local-capture-v1", username, config: selected.file, export: exportType, courses, exams});
      parseLocalCapture(text);
      output.value = text; output.hidden = false; copy.hidden = false;
      status.textContent = "已在本机采集学号尾号 " + username.slice(-4) + " 的 " + courses.reduce((sum, term) => sum + term.rows.length, 0) + " 条课程记录。尚未上传，请复制后回到订阅页确认。";
    } catch { status.textContent = "采集未完成，未生成部分数据。请确认已在官方课表页登录，或选择仅课程后重试；不会自动重试登录。"; }
    finally { collect.disabled = false; }
  };
})();
