// 腾讯云函数版：微信晨报反馈机器人（从 Cloudflare Worker 移植，2026-10-18）
// 架构：微信服务器 → API网关 → 本函数 → {Cloudflare KV(模式状态) + GitHub issue(反馈) + 微信客服回复}
// 部署：serverless.yml + `sls deploy`（详见 README-部署.md 第八节）

"use strict";

const https = require("https");

// ---- 配置（部署时由环境变量注入）----
const ENV = process.env;
const WX_TOKEN = ENV.WX_TOKEN;
const WX_APPID = ENV.WX_APPID;
const WX_SECRET = ENV.WX_SECRET;
const GH_REPO = ENV.GH_REPO;
const GH_ISSUE = ENV.GH_ISSUE;
const GH_PAT = ENV.GH_PAT;
// Cloudflare KV（复用原 Worker 的存储，模式状态跨云共享）
const CF_API = "https://api.cloudflare.com/client/v4";
const CF_ACCOUNT = ENV.CF_ACCOUNT;
const CF_KV_NS = ENV.CF_KV_NS; // namespace id
const CF_TOKEN = ENV.CF_TOKEN;

// ---- 零依赖 HTTP 封装 ----
function req(method, url, { headers = {}, body = null } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const r = https.request(
      { hostname: u.hostname, path: u.pathname + u.search, method, headers, timeout: 15000 },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode, text: data }));
      },
    );
    r.on("error", reject);
    r.on("timeout", () => r.destroy(new Error("timeout")));
    if (body) r.write(body);
    r.end();
  });
}

const crypto = require("crypto");
async function sha1Hex(s) {
  return crypto.createHash("sha1").update(s).digest("hex");
}

// ---- Cloudflare KV REST ----
const kvUrl = (key) => `${CF_API}/accounts/${CF_ACCOUNT}/storage/kv/namespaces/${CF_KV_NS}/values/${encodeURIComponent(key)}`;
const kvHeaders = { Authorization: `Bearer ${CF_TOKEN}` };
async function kvGet(key) {
  const r = await req("GET", kvUrl(key), { headers: kvHeaders });
  return r.status === 200 ? r.text : null;
}
async function kvPut(key, value, ttlSeconds) {
  const q = ttlSeconds ? `?expiration_ttl=${ttlSeconds}` : "";
  const r = await req("PUT", kvUrl(key) + q, {
    headers: { ...kvHeaders, "Content-Type": "text/plain" },
    body: value,
  });
  if (r.status !== 200) throw new Error(`kvPut ${r.status}: ${r.text.slice(0, 120)}`);
}
async function kvDel(key) {
  await req("DELETE", kvUrl(key), { headers: kvHeaders });
}

// ---- GitHub / 微信 ----
async function ghComment(body) {
  const r = await req(`POST`, `https://api.github.com/repos/${GH_REPO}/issues/${GH_ISSUE}/comments`, {
    headers: {
      Authorization: `Bearer ${GH_PAT}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "daily-brief-feedback-scf",
    },
    body: JSON.stringify({ body }),
  });
  if (r.status !== 201) throw new Error(`github ${r.status}: ${r.text.slice(0, 150)}`);
}

async function wxReply(openid, text) {
  let token = await kvGet("wx_token");
  if (!token) {
    const r = JSON.parse(
      (await req("GET", `https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=${WX_APPID}&secret=${WX_SECRET}`)).text,
    );
    token = r.access_token;
    if (!token) throw new Error("wx token: " + JSON.stringify(r).slice(0, 120));
    await kvPut("wx_token", token, 7000);
  }
  const r = await req(
    "POST",
    `https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token=${token}`,
    {
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ touser: openid, msgtype: "text", text: { content: text } }),
    },
  );
  const j = JSON.parse(r.text);
  if (j.errcode === 40001 || j.errcode === 42001) {
    await kvDel("wx_token"); // 失效则下次重取
  }
}

// ---- 业务（与 Worker 版一致）----
const XML_FIELD = (xml, tag) => {
  const m = xml.match(new RegExp(`<${tag}>(?:<!\\[CDATA\\[)?([\\s\\S]*?)(?:\\]\\]>)?</${tag}>`));
  return m ? m[1].trim() : "";
};

function today() {
  return new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10);
}

async function handle(msg) {
  const text = (msg.Content || "").trim();
  const who = msg.FromUserName;
  const say = (t) => wxReply(who, t).catch((e) => console.error("reply:", e));

  if (text === "增加" || text === "减少" || text === "增" || text === "减") {
    const mode = text.startsWith("增") ? "up" : "down";
    await kvPut("mode:" + who, mode, 86400);
    await say(
      mode === "up"
        ? "已进入【增加】模式：回复晨报里的编号（如 3），对应条目的同类内容权重上调。发「取消」退出。"
        : "已进入【减少】模式：回复晨报里的编号（如 3），对应条目的同类内容权重下调。发「取消」退出。",
    );
    return;
  }
  if (text === "取消" || text === "退出") {
    await kvDel("mode:" + who);
    await say("已退出反馈模式 ✓");
    return;
  }
  if (/^\d{1,2}$/.test(text)) {
    const mode = await kvGet("mode:" + who);
    if (!mode) {
      await say("请先回复「增加」或「减少」进入反馈模式，再回复编号。");
      return;
    }
    await kvDel("mode:" + who);
    await ghComment(`FEEDBACK|${today()}|${mode}|${text}`);
    await say(`已记录：今日晨报第 ${text} 条 权重${mode === "up" ? "↑上调" : "↓下调"}，明天生效 ✓`);
    return;
  }
  await ghComment(`NOTE|${today()}|${text}`);
  await say("已记录这条备注，会作为筛选调整的参考 ✓");
}

// ---- SCF 入口（API 网关透传格式）----
exports.main_handler = async (event) => {
  // 诊断端点
  if ((event.path || "").endsWith("/wx-diag")) {
    const k = (event.queryString || {}).k;
    if (k !== WX_TOKEN) return { isBase64Encoded: false, statusCode: 403, headers: {}, body: "no" };
    const last = (await kvGet("diag:last")) || "消息：无任何到达";
    return { isBase64Encoded: false, statusCode: 200, headers: {}, body: last };
  }
  if ((event.path || "").endsWith("/wx")) {
    if (event.httpMethod === "GET") {
      // 微信验证（与 Worker 版一致）；记录验证探针
      const q = event.queryString || {};
      await kvPut("diag:get", `${new Date().toISOString()} 验证请求到达(SCF)`).catch(() => {});
      const sig = await sha1Hex([WX_TOKEN, q.timestamp, q.nonce].sort().join(""));
      return sig === q.signature
        ? { isBase64Encoded: false, statusCode: 200, headers: {}, body: q.echostr }
        : { isBase64Encoded: false, statusCode: 403, headers: {}, body: "forbidden" };
    }
    // POST：消息推送。API网关透传时 body 可能 base64
    let xml = event.body || "";
    if (event.isBase64Encoded) xml = Buffer.from(xml, "base64").toString("utf-8");
    await kvPut("diag:last", `${new Date().toISOString()} RAW[${xml.slice(0, 100).replace(/\n/g, " ")}]`).catch(() => {});
    const msg = {
      FromUserName: XML_FIELD(xml, "FromUserName"),
      Content: XML_FIELD(xml, "Content"),
      MsgId: XML_FIELD(xml, "MsgId"),
    };
    if (!msg.FromUserName) return { isBase64Encoded: false, statusCode: 200, headers: {}, body: "success" };
    try {
      const seen = "msgid:" + msg.MsgId;
      if (msg.MsgId && (await kvGet(seen))) return { isBase64Encoded: false, statusCode: 200, headers: {}, body: "success" };
      if (msg.MsgId) await kvPut(seen, "1", 3600);
      await handle(msg);
    } catch (e) {
      console.error("handle:", e);
    }
    return { isBase64Encoded: false, statusCode: 200, headers: {}, body: "success" };
  }
  return { isBase64Encoded: false, statusCode: 404, headers: {}, body: "not found" };
};
