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
const ZHIPU_KEY = ENV.ZHIPU_API_KEY;
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
  // 超长拆条（微信单条 2048 字节；1900 分段线=合规内容单条发完，真超长才拆）
  const parts = [];
  let buf = [];
  for (const line of text.split("\n")) {
    if (buf.reduce((n, b) => n + Buffer.byteLength(b, "utf8"), 0) + Buffer.byteLength(line, "utf8") > 1900) {
      if (buf.length) parts.push(buf.join("\n"));
      buf = [line];
    } else buf.push(line);
  }
  if (buf.length) parts.push(buf.join("\n"));
  if (!parts.length) parts.push(text);
  for (let i = 0; i < parts.length; i++) {
    const suffix = parts.length > 1 ? `\n（${i + 1}/${parts.length}）` : "";
    await wxReplyOne(openid, parts[i] + suffix);
  }
}

async function wxReplyOne(openid, text) {
  // 最多两轮：缓存的 token 失效（40001/42001）时清缓存并重取重发，杜绝静默丢消息
  for (let attempt = 0; attempt < 2; attempt++) {
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
    if (j.errcode === 0) return;
    if (j.errcode === 40001 || j.errcode === 42001) {
      await kvDel("wx_token"); // token 失效：清缓存进入下一轮重取
      continue;
    }
    throw new Error("wx send " + JSON.stringify(j).slice(0, 120));
  }
  throw new Error("wx send: token 两轮均失效");
}

// ---- 智谱解读（「详情」功能：新闻全貌+AI分析，标注观点级；多模型降级链防限流）----
const ZHIPU_MODELS = ["glm-5.3-flash", "glm-4.7-flash", "glm-4.5-flash", "glm-4-flash-250414"];
async function zhipuExplain(newsText, headline) {
  let lastErr = null;
  for (const model of ZHIPU_MODELS) {
    try {
      const body = JSON.stringify({
        model,
        temperature: 0.4,
        messages: [
          { role: "system", content: "你是个人金融新闻编辑。解读只陈列事实与分析线索，绝不输出投资建议或收益承诺；判断部分必须标注为分析观点。" },
          { role: "user", content: `用户是宏观/黄金/AI资本开支的个人研究者，关注：美国通胀与加息路径、长端利率、黄金定价、AI资本开支融资结构、欧美债务。请对下面这条晨报新闻做详细说明，输出三段（总长≤550汉字，语言通俗，术语用大白话解释）：\n①事件全貌（时间/主体/关键数字）\n②背景（为什么重要，尽量挂到上述用户关注的主线）\n③分析与观察点（开头标注「分析·观点」：可能的影响、后续该盯什么）\n\n晨报条目标题：${headline}\n原始新闻全文：\n${newsText}` },
        ],
      });
      const r = await req("POST", "https://open.bigmodel.cn/api/paas/v4/chat/completions", {
        headers: { Authorization: `Bearer ${ZHIPU_KEY}`, "Content-Type": "application/json; charset=utf-8", "User-Agent": "daily-brief-scf" },
        body,
      });
      if (r.status === 429 || r.status === 503) throw new Error(`zhipu ${r.status}`);
      if (r.status !== 200) throw new Error(`zhipu ${r.status}: ${r.text.slice(0, 120)}`);
      const j = JSON.parse(r.text);
      const content = j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content;
      if (!content) throw new Error("zhipu 空回复");
      return content.trim();
    } catch (e) {
      lastErr = e;
      console.error(`zhipu [${model}] 失败:`, e && e.message);
    }
  }
  throw lastErr || new Error("zhipu 全部失败");
}

// 拉取当日 BRIEF-D（详情数据）第 idx 条
async function fetchBriefDetail(idx) {
  const r = await req(
    "GET",
    `https://api.github.com/repos/${GH_REPO}/issues/${GH_ISSUE}/comments?per_page=100`,
    { headers: { Authorization: `Bearer ${GH_PAT}`, Accept: "application/vnd.github+json", "User-Agent": "daily-brief-scf" } },
  );
  if (r.status !== 200) throw new Error(`github ${r.status}`);
  const comments = JSON.parse(r.text);
  let latest = null;
  for (const c of comments) if (typeof c.body === "string" && c.body.startsWith("BRIEF-D|")) latest = c.body;
  if (!latest) return null;
  const segs = latest.split("‖");
  for (const seg of segs) {
    const m = seg.match(new RegExp(`^${idx}=`));
    if (m) return seg.slice(m[0].length);
    if (seg.startsWith("BRIEF-D|")) {
      const mm = seg.match(new RegExp(`^BRIEF-D\\|\\d{4}-\\d{2}-\\d{2}\\|${idx}=`));
      if (mm) return seg.slice(mm[0].length);
    }
  }
  return null;
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
  if (text === "详情" || text === "详细") {
    await kvPut("mode:" + who, "detail", 86400);
    await say("已进入【详情】模式：回复晨报里的编号（如 2），我将给出该条新闻的完整信息与解读。发「取消」退出。");
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
      await say("请先回复「增加」「减少」或「详情」进入对应模式，再回复编号。");
      return;
    }
    await kvDel("mode:" + who);
    if (mode === "detail") {
      try {
        const detail = await fetchBriefDetail(text);
        if (!detail) {
          await say(`没找到今天晨报第 ${text} 条的存档（详情数据自 2026-10-18 起生成）。`);
          return;
        }
        await say("正在解读，约需几秒…");
        const headline = detail.split("\n")[0].slice(0, 50);
        const explain = await zhipuExplain(detail, headline);
        await say(`📄 第 ${text} 条 · ${headline}\n\n${explain}`);
      } catch (e) {
        console.error("detail:", e);
        await say(`详情功能出错了：${String(e && e.message || e).slice(0, 120)}\n原始新闻：\n${(await fetchBriefDetail(text).catch(() => "") || "").slice(0, 500)}`);
      }
      return;
    }
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
