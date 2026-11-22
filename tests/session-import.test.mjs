import test from "node:test";
import assert from "node:assert/strict";
import {parseTeachingSession} from "../server/static/session-import.mjs";

const cookie = (name, value, path = "/", domain = "zdbk.zju.edu.cn") => ({name, value, path, domain});
test("mobile export keeps both paths and discards global identity and SSO", () => {
  const result = parseTeachingSession(JSON.stringify([
    cookie("JSESSIONID", "teaching", "/jwglxt"), cookie("JSESSIONID", "root"),
    cookie("JSESSIONTOKEN", "teaching-token"), cookie("route", "node"),
    cookie("iPlanetDirectoryPro", "global-sso", "/", ".zju.edu.cn"),
    cookie("JWTUser", "global-user", "/", ".zju.edu.cn"),
    cookie("JSESSIONPREJSDM", "unneeded", "/jwglxt/xtgl"),
    cookie("JSESSIONID", "spoofed", "/jwglxt", "zdbk.zju.edu.cn.evil.test"),
  ]));
  assert.deepEqual(result, {kind: "ugrs", mode: "token", secret: "teaching", root_session: "root", teaching_token: "teaching-token", route: "node"});
  assert(!JSON.stringify(result).includes("global"));
});
test("ambiguous values, expired sessions and missing paths are rejected", () => {
  for (const cookies of [
    [cookie("JSESSIONID", "one", "/jwglxt"), cookie("JSESSIONID", "two", "/jwglxt")],
    [cookie("JSESSIONID", "root-only")],
    [{...cookie("JSESSIONID", "expired", "/jwglxt"), expirationDate: 1}],
    [{...cookie("JSESSIONID", "partitioned", "/jwglxt"), partitionKey: {topLevelSite: "https://elsewhere.test"}}],
    [cookie("JSESSIONID", "value; injected=cookie", "/jwglxt")],
  ]) assert.throws(() => parseTeachingSession(JSON.stringify(cookies)));
});
test("helper files cannot introduce a password or global SSO field", () => {
  const credentials = {username: "3000000000", kind: "ugrs", mode: "token", secret: "teaching"};
  const format = "zjuical-teaching-session-v1";
  assert.deepEqual(parseTeachingSession(JSON.stringify({format, credentials})), credentials);
  for (const extra of [{password: "secret"}, {iPlanetDirectoryPro: "global"}, {mode: "password"}]) {
    assert.throws(() => parseTeachingSession(JSON.stringify({format, credentials: {...credentials, ...extra}})));
  }
});
test("malformed, unbounded and unrecognized imports fail without reflecting secrets", () => {
  for (const text of ["not-json-sensitive-value", "null", "{}", "1", "[", " ".repeat(131073)]) {
    assert.throws(() => parseTeachingSession(text), error => !error.message.includes("sensitive-value"));
  }
});
