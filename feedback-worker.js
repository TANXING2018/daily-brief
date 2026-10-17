// 晨报反馈 Worker（Cloudflare Workers，免费档）
// 机制参照：wechat-official-account-mcp-private（Workers 接微信回调架构）
// + python-telegram-bot ConversationHandler 三段式（入口→状态→回退）。
// 交互（用户 2026-10-18 定）：回复「增加/减少」进入模式 → 回复编号 → 该条权重调整，
// 记录写入 GitHub issue（FEEDBACK|日期|方向|编号），晨报脚本次日读取生效。

const XML_FIELD = (xml, tag) => {
  const m = xml.match(new RegExp(`<${tag}>(?:<!\\[CDATA\\[)?(.*?)(?:\\]\\]>)?</${tag}>`, "s"));
  return m ? m[1].trim() : "";
};

async function sha1Hex(s) {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function today() {
  return new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10); // 北京日期
}

async function ghComment(env, body) {
  const r = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/issues/${env.GH_ISSUE}/comments`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_PAT}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "daily-brief-feedback-worker", // GitHub 硬性要求：无 UA 直接 403
      },
      body: JSON.stringify({ body }),
    },
  );
  if (!r.ok) throw new Error(`github ${r.status}: ${await r.text()}`);
}

// 客服消息回复（复用晨报推送的通道；token 缓存于 KV，微信有效期 7200s）
async function reply(env, openid, text) {
  let token = await env.FBK.get("wx_token");
  if (!token) {
    const r = await fetch(
      `https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=${env.WX_APPID}&secret=${env.WX_SECRET}`,
    ).then(r => r.json());
    token = r.access_token;
    if (!token) throw new Error("wx token: " + JSON.stringify(r));
    await env.FBK.put("wx_token", token, { expirationTtl: 7000 });
  }
  const r = await fetch(
    `https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token=${token}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ touser: openid, msgtype: "text", text: { content: text } }),
    },
  ).then(r => r.json());
  if (r.errcode === 40001 || r.errcode === 42001) {
    await env.FBK.delete("wx_token"); // token 失效，下次重取
  }
}

async function handle(msg, env) {
  const text = (msg.Content || "").trim();
  const who = msg.FromUserName;
  const say = t => reply(env, who, t).catch(e => console.error("reply:", e));

  if (text === "增加" || text === "减少" || text === "增" || text === "减") {
    const mode = text.startsWith("增") ? "up" : "down";
    await env.FBK.put("mode:" + who, mode, { expirationTtl: 86400 });
    await say(
      mode === "up"
        ? "已进入【增加】模式：回复晨报里的编号（如 3），对应条目的同类内容权重上调。发「取消」退出。"
        : "已进入【减少】模式：回复晨报里的编号（如 3），对应条目的同类内容权重下调。发「取消」退出。",
    );
    return;
  }
  if (text === "取消" || text === "退出") {
    await env.FBK.delete("mode:" + who);
    await say("已退出反馈模式 ✓");
    return;
  }
  if (/^\d{1,2}$/.test(text)) {
    const mode = await env.FBK.get("mode:" + who);
    if (!mode) {
      await say("请先回复「增加」或「减少」进入反馈模式，再回复编号。");
      return;
    }
    await env.FBK.delete("mode:" + who);
    await ghComment(env, `FEEDBACK|${today()}|${mode}|${text}`);
    await say(`已记录：今日晨报第 ${text} 条 权重${mode === "up" ? "↑上调" : "↓下调"}，明天生效 ✓`);
    return;
  }
  await ghComment(env, `NOTE|${today()}|${text}`);
  await say("已记录这条备注，会作为筛选调整的参考 ✓");
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/wx-diag") {
      // 诊断端点：返回最近消息与最近验证探针（用 WX_TOKEN 防止外人查看）
      if (url.searchParams.get("k") !== env.WX_TOKEN) return new Response("no", { status: 403 });
      const last = (await env.FBK.get("diag:last")) || "消息：无任何到达";
      const get = (await env.FBK.get("diag:get")) || "验证：无任何到达";
      return new Response(get + "\n" + last);
    }
    if (url.pathname !== "/wx") return new Response("not found", { status: 404 });

    if (request.method === "GET") {
      // 微信服务器验证（标准实现：字典序 sha1 比对）；记录验证探针到达（诊断）
      const q = url.searchParams;
      await env.FBK.put("diag:get", `${new Date().toISOString()} 验证请求到达`);
      const sig = await sha1Hex([env.WX_TOKEN, q.get("timestamp"), q.get("nonce")].sort().join(""));
      return sig === q.get("signature")
        ? new Response(q.get("echostr"))
        : new Response("forbidden", { status: 403 });
    }

    const xml = await request.text();
    // 无条件登记：任何 POST 到达都记录原始体（诊断微信是否推送 + 格式差异）
    await env.FBK.put(
      "diag:last",
      `${new Date().toISOString()} RAW[${xml.slice(0, 100).replace(/\n/g, " ")}]`,
    );
    const msg = {
      FromUserName: XML_FIELD(xml, "FromUserName"),
      Content: XML_FIELD(xml, "Content"),
      MsgId: XML_FIELD(xml, "MsgId"),
    };
    if (!msg.FromUserName) return new Response("success");
    // 同步处理（微信 5 秒窗口内完成，实测 2-3 秒）；错误直接回在响应里便于诊断
    try {
      const seen = "msgid:" + msg.MsgId;
      if (msg.MsgId && (await env.FBK.get(seen))) return new Response("success"); // msgid 排重（微信会重试）
      if (msg.MsgId) await env.FBK.put(seen, "1", { expirationTtl: 3600 });
      await handle(msg, env);
      return new Response("success");
    } catch (e) {
      return new Response("ERR:" + String((e && e.message) || e).slice(0, 300));
    }
  },
};
