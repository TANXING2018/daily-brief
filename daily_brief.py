"""云端晨报：三源快讯初筛 → 仪表核对 → LLM 按画像凝练 → Server酱推送微信。

部署与密钥配置见同目录 README-部署.md。GitHub Actions 每日 UTC 00:30（北京 08:30）触发。

本地空跑（不调模型不推送，只打印组装好的提示词供检查）：
    python daily_brief.py --dry-run

词表与 D:/workspace2/AIfinance/tools/fin/news_radar.py 保持同步（改一处改两处）。
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

# 快讯源时间均为北京时间；Actions runner 是 UTC，统一换算
NOW = datetime.utcnow() + timedelta(hours=8)

ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
# 免费模型档位随平台改名会失效（2026-10：glm-4-flash 已不存在），按序自动降级
ZHIPU_MODELS = [m for m in [os.environ.get("ZHIPU_MODEL"), "glm-4.7-flash", "glm-4.5-flash", "glm-4-flash-250414"] if m]

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(HERE, "profile.md")

# ---------------------------------------------------------------- 画像关键词（与本地 news_radar.py 同步）
# 2026-10-18 机制改版：词表分数不再是硬门槛，只是「画像匹配」的参考信号与
# LLM 失败时的降级排序（借鉴 TrendRadar 的回退设计）。黑名单只拦真垃圾，
# 行情播报/产品新闻交给 LLM 的增量性维度自然压低。

TIER1 = {
    "黄金", "金价", "央行购金",
    "意德利差", "BTP",
    "甲骨文", "CDS", "信用利差", "高收益债",
    "穆迪", "惠誉", "标普全球评级", "下调评级", "上调评级", "评级展望",
    "FOMC", "美联储决议", "联邦公开市场",
    "美债收益率", "10年期美债", "30年期美债", "国债拍卖", "期限溢价",
    "逆回购", "RRP",
}
TIER2 = {
    "通胀", "CPI", "PCE", "加息", "降息", "利率决议", "美联储", "联储", "鲍威尔",
    "美债", "国债", "收益率曲线", "实际利率",
    "债务", "赤字", "违约", "衰退", "银行", "金融危机", "商业地产", "硅谷银行",
    "法国", "意大利", "德国", "欧洲央行", "欧元区", "政局", "预算", "勒庞", "马克龙",
    "AI", "人工智能", "资本开支", "capex", "科技股", "纳斯达克", "算力", "数据中心",
    "美元指数", "日元", "套息交易", "汇率",
    "中期选举", "大选", "财政刺激", "财政政策", "贝森特", "特朗普",
    "熔断", "暴跌", "暴涨", "历史新高", "历史新低",
    "A股", "沪深", "港股", "恒生",
}
TIER3 = {
    "标普", "道指", "纳指", "美股", "股市", "原油", "油价", "比特币", "加密",
    "GDP", "就业", "非农", "失业率", "PMI", "中国经济", "中国央行",
    "IMF", "世界银行", "G20", "G7",
}
BLACKLIST = re.compile(
    r"娱乐|体育|明星|球队|联赛|彩票|游戏|手游|直播|带货|综艺|电影|电视剧|演唱会"
    r"|涨停板|连板|打板|妖股|龙虎榜|新股申购|中签|打新"
    r"|明星基金|基金经理|私募大佬"
    r"|回购股份|回购计划|增持计划|员工持股计划"
)


@dataclass
class Item:
    title: str
    body: str
    ts: datetime
    source: str
    score: int = 0
    hits: list = None

    def __post_init__(self):
        self.hits = self.hits or []


def _sig(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)


def _parse_ts(s) -> datetime:
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min


def _hits(text: str):
    score, hits = 0, []
    for kw in TIER1:
        if kw in text:
            score += 5; hits.append(kw)
    for kw in TIER2:
        if kw in text:
            score += 3; hits.append(kw)
    for kw in TIER3:
        if kw in text:
            score += 1; hits.append(kw)
    return score, hits


def fetch_and_rank(hours: float = 36.0, limit: int = 40) -> list[Item]:
    import akshare as ak
    items = []

    def add(df, t_col, b_col, ts_fn, src):
        for _, r in df.iterrows():
            items.append(Item(str(r[t_col]), str(r[b_col]), ts_fn(r), src))

    src_status = []
    for name, loader in [
        ("东财", lambda: ak.stock_info_global_em()),
        ("财联社", lambda: ak.stock_info_global_cls()),
        ("新浪", lambda: ak.stock_info_global_sina()),
    ]:
        try:
            df = loader()
        except Exception as e:
            src_status.append(f"{name}源不可用({type(e).__name__})")
            continue
        if name == "东财":
            add(df, "标题", "摘要", lambda r: _parse_ts(r["发布时间"]), "东财")
        elif name == "财联社":
            rows = []
            for _, r in df.iterrows():
                t = str(r.get("标题") or "").strip() or str(r.get("内容") or "")[:40]
                rows.append((t, str(r.get("内容") or ""), f"{r['发布日期']} {r['发布时间']}", "财联社"))
            for t, b, ts, s in rows:
                items.append(Item(t, b, _parse_ts(ts), s))
        else:
            for _, r in df.iterrows():
                b = str(r["内容"]).strip()
                items.append(Item(b[:40], b, _parse_ts(r["时间"]), "新浪"))

    for msg in src_status:
        print(f"⚠ {msg}", file=sys.stderr)

    # 同源去重（跨源完全重复留给模型层合并为"多源"）
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: x.ts, reverse=True):
        keys = {_sig(it.title)[:20], _sig(it.body)[:30]}
        if any(len(k) >= 6 and k in seen for k in keys):
            continue
        seen.update(k for k in keys if len(k) >= 6)
        uniq.append(it)

    cutoff = NOW - timedelta(hours=hours)
    ranked = []
    for it in uniq:
        if it.ts != datetime.min and it.ts < cutoff:
            continue
        if BLACKLIST.search(it.title + " " + it.body):
            continue
        s_t, h_t = _hits(it.title)
        s_b, h_b = _hits(it.body)
        it.score = s_t * 2 + s_b
        it.hits = sorted(set(h_t + h_b))
        ranked.append(it)  # 无硬门槛：词表分仅作 LLM 失败时的降级排序
    ranked.sort(key=lambda x: (-x.score, -x.ts.timestamp()))
    return ranked[:limit]


def gauges() -> str:
    """路标仪表实测：金价与美债 10Y。拉不到就如实标注，不硬凑。"""
    lines = []
    try:
        import akshare as ak
        df = ak.futures_foreign_hist(symbol="GC").tail(2)
        closes = "；".join(f"{r['date']} 收 {r['close']}" for _, r in df.iterrows())
        lines.append(f"- 金价 COMEX：{closes}（路标分界 3900 / 5446）")
    except Exception as e:
        lines.append(f"- 金价：未观测到（拉取失败 {type(e).__name__}）")
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", timeout=30)
        rows = [l for l in r.text.splitlines()[1:] if l.strip() and l.split(",")[-1] != "."]
        last = rows[-1].split(",")
        lines.append(f"- 美债 10Y：{last[1]}%（FRED 截止 {last[0]}，数据有滞后；失效线 = 收盘 <4.8% 站稳一周）")
    except Exception as e:
        lines.append(f"- 美债 10Y：未观测到（拉取失败 {type(e).__name__}）")
    return "\n".join(lines)


def build_prompt(ranked: list[Item], gauge_text: str, feedback_hints: str = "") -> str:
    profile = open(PROFILE_PATH, encoding="utf-8").read()
    listing = []
    for it in ranked:
        listing.append(f"[{it.score}分] {it.title}\n  {it.body[:180]}\n  ({it.ts:%m-%d %H:%M} · {it.source} · 命中:{'/'.join(it.hits[:6])})")
    return f"""# 我的筛选画像（打分制标准、路标表、输出格式——严格遵循）

{profile}

# 今日候选清单（机械层宽召回，方括号内词表分仅是画像匹配的参考信号之一）

{chr(10).join(listing)}

# 路标仪表（工具实测，行情数字以这里为准、快讯口径须标注）

{gauge_text}
{(chr(10) + feedback_hints + chr(10)) if feedback_hints else ''}
# 任务

今天是 {NOW:%Y-%m-%d}。按画像第一节的打分制处理：对清单逐条打「画像匹配/重要性/增量性」
三维分，总分=画像×0.5+重要性×0.3+增量×0.2。取总分最高的做一句话头条（≤60字）；
次条按总分降序**取尽所有合格条目（目标 5~15 条：新闻日多给、安静日少给甚至为零，
宁缺毋滥）**。**输出硬预算：整份晨报 ≤620 个汉字（微信单条消息上限）——条数多时
对同主题新闻归纳整合成一条综合快讯（而非逐条罗列），每条尽量 ≤40 字，语言通俗易懂
（术语换大白话，如"期限溢价"→"买长期债要多给的补偿"）。宁可精编整合，绝不输出长文。**
全部总分<4 就写"今日无增量，路标无触发"。
铁律：新闻=线索不是证据；观点级标"观点"且不进头条；跨源完全重复合并为一条并标"多源"；
**路标对照板块默认省略，仅当有路标触发（标⚠）或明显逼近阈值时才输出，且只列异常项**。
**不要输出分数本身**，只按画像第五节格式输出晨报。"""


def llm_brief(prompt: str) -> str:
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {"Authorization": f"Bearer {ZHIPU_API_KEY}", "Content-Type": "application/json"}
    last_err = None
    for model in ZHIPU_MODELS:
        payload = {
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": "你是个人金融新闻编辑。你编的晨报只陈列线索与路标对照，绝不输出投资建议或收益承诺。"},
                {"role": "user", "content": prompt},
            ],
        }
        for attempt in (1, 2):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=180)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"].strip()
                if model != ZHIPU_MODELS[0]:
                    print(f"ℹ 实际使用模型：{model}", file=sys.stderr)
                return content
            except Exception as e:
                body = ""
                resp = getattr(e, "response", None)
                if resp is not None:
                    body = resp.text[:300].replace("\n", " ")
                last_err = f"{type(e).__name__} {body}"
                print(f"⚠ LLM [{model}] 第 {attempt} 次失败：{last_err}", file=sys.stderr)
                if resp is not None and '"code":"1211"' in body:
                    break  # 模型不存在：换下一个候选，不重试
                if attempt == 2:
                    break
                time.sleep(10)
    raise RuntimeError(f"所有模型候选均失败，最后错误：{last_err}")


# ---------------------------------------------------------------- 反馈闭环（用户 2026-10-18 定：
# 微信回复「增加/减少」→ 编号 → feedback-worker.js 存 issue；晨报读取近期反馈调打分）
GH_TOKEN = os.environ.get("GH_FEEDBACK_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_REPO = "TANXING2018/daily-brief"


def _gh(method: str, path: str, token: str, json_body=None) -> requests.Response:
    return requests.request(method, f"https://api.github.com{path}",
                            headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/vnd.github+json"},
                            json=json_body, timeout=30)


def feedback_issue(token: str) -> int:
    """找「晨报反馈」issue，没有则建。返回 issue 号。"""
    r = _gh("GET", f"/repos/{GH_REPO}/issues?state=open&per_page=50", token)
    for it in r.json():
        if it.get("title") == "晨报反馈":
            return it["number"]
    r2 = _gh("POST", f"/repos/{GH_REPO}/issues", token,
             {"title": "晨报反馈", "body": "反馈通道：Worker 写入 FEEDBACK/NOTE，晨报脚本写 BRIEF 编号映射。勿删。"})
    r2.raise_for_status()
    return r2.json()["number"]


def post_brief_map(brief: str, token: str) -> None:
    """推送成功后把当日编号→标题映射写进反馈 issue（供编号反馈定位新闻）。"""
    try:
        n = feedback_issue(token)
        entries = []
        m = re.search(r"^\*\*一句话头条\*\*：(.+)", brief, re.M)
        if m:
            entries.append(f"0={strip_src_lite(m.group(1))[:50]}")
        sub = brief.split("**次条**", 1)[-1]
        for i, line in enumerate(sub.splitlines()):
            s = line.strip()
            if s[:2] in ("- ", "· ", "* "):
                entries.append(f"{len(entries)}={strip_src_lite(s.lstrip('-·* '))[:50]}")
        _gh("POST", f"/repos/{GH_REPO}/issues/{n}/comments", token,
            {"body": f"BRIEF|{NOW:%Y-%m-%d}|" + "|".join(entries)})
        print(f"已写当日编号映射到 issue #{n}")
    except Exception as e:
        print(f"⚠ 编号映射写入失败（不影响晨报）：{type(e).__name__} {str(e)[:80]}", file=sys.stderr)


def strip_src_lite(t: str) -> str:
    return re.sub(r"（[^（）]*(?:\d{1,2}-\d{1,2}|\d{1,2}:\d{2})[^（）]*）", "", t).strip()


def fetch_feedback_hints(token: str) -> str:
    """读反馈 issue：最近 BRIEF 映射 + 其后的 FEEDBACK/NOTE → 打分调整提示。"""
    try:
        n = feedback_issue(token)
        r = _gh("GET", f"/repos/{GH_REPO}/issues/{n}/comments?per_page=100", token)
        comments = [c["body"] for c in r.json()]
        brief_map = None
        for c in comments:
            if c.startswith("BRIEF|"):
                brief_map = c  # 取最新一条
        feedbacks = [c for c in comments if c.startswith(("FEEDBACK|", "NOTE|"))]
        if not feedbacks:
            return ""
        heads = dict(kv.split("=", 1) for kv in brief_map.split("|")[2:] if "=" in kv) if brief_map else {}
        lines = []
        for f in feedbacks[-8:]:  # 只取最近 8 条，避免 prompt 膨胀
            parts = f.split("|")
            if parts[0] == "FEEDBACK" and len(parts) >= 4:
                title = heads.get(parts[3], "（当日条目）")
                lines.append(f"- {parts[1]} 用户对「{title}」点了{parts[2] == 'up' and '增加' or '减少'}")
            elif parts[0] == "NOTE" and len(parts) >= 3:
                lines.append(f"- {parts[1]} 用户备注：「{parts[2][:60]}」")
        if not lines:
            return ""
        return ("# 近期用户反馈（打分调整依据：将反馈条目归纳为其主题/同类，"
                "后续同类新闻的画像匹配分相应上调或下调）\n" + "\n".join(lines))
    except Exception as e:
        print(f"⚠ 反馈读取失败（不影响晨报）：{type(e).__name__} {str(e)[:80]}", file=sys.stderr)
        return ""


def push_wechat(brief: str) -> None:
    title = re.search(r"^\*\*一句话头条\*\*：(.+)", brief, re.M)
    t = (title.group(1).strip()[:100] if title else f"个人头条 · {NOW:%Y-%m-%d}")
    r = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
        data={"title": f"📡 {t}", "desp": brief},
        timeout=30,
    )
    r.raise_for_status()
    print(f"已推送微信：{r.json().get('code')} {r.json().get('message', '')}")


def push_mail(brief: str) -> None:
    """QQ 邮箱 SMTP 推送（MAil_USER/MAIL_PASS 来自 Actions Secrets）。"""
    import smtplib
    from email.mime.text import MIMEText

    user = os.environ.get("MAIL_USER", "")
    pwd = os.environ.get("MAIL_PASS", "")
    to = os.environ.get("MAIL_TO", user)
    title = re.search(r"^\*\*一句话头条\*\*：(.+)", brief, re.M)
    subject = f"📡 {title.group(1).strip()[:60]}" if title else f"📡 个人头条 · {NOW:%Y-%m-%d}"
    msg = MIMEText(brief, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    print(f"已推送邮件至 {to}")


def push_weixin_test(brief: str) -> None:
    """微信测试号推送。主通道=客服消息（普通聊天气泡，全文可读）；
    若被 48h 互动窗口拦截（45015），降级模板消息卡片并带 url 跳转全文。"""
    appid = os.environ["WEIXIN_APPID"]
    secret = os.environ["WEIXIN_SECRET"]
    openid = os.environ["WEIXIN_OPENID"]
    tpl = os.environ.get("WEIXIN_TEMPLATE_ID", "")

    r = requests.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={"grant_type": "client_credential", "appid": appid, "secret": secret},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError(f"取 access_token 失败：{r.text[:120]}")

    def plain(md: str) -> str:
        s = re.sub(r"\*\*|##|^\s*[-*]\s*", "", md, flags=re.M).strip()
        return re.sub(r"\n{3,}", "\n\n", s)

    def chat_format(md: str) -> str:
        """晨报 markdown → 微信聊天窗友好排版：分隔线/📌头条/①次条/🎯路标块。
        用户 2026-10-17 定：删来源+时点括注求简洁（观点/多源等性质标注保留）。"""
        def strip_src(t: str) -> str:
            return re.sub(r"（[^（）]*(?:\d{1,2}-\d{1,2}|\d{1,2}:\d{2})[^（）]*）", "", t).strip()

        circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
        out, section, sub_n = [], "head", 0
        for raw in md.splitlines():
            s = raw.strip()
            if not s:
                continue
            clean = s.replace("**", "")
            if clean.startswith("## "):
                out += [clean[3:].strip(), "━" * 16, ""]
            elif clean.startswith("一句话头条"):
                head = clean.split("：", 1)[-1] if "：" in clean else clean.split(":", 1)[-1]
                out += [f"📌 {strip_src(head)}", ""]
                section = "sub_head"
            elif clean.startswith("次条"):
                section = "sub"
            elif clean.startswith("路标对照"):
                out += ["", "🎯 路标对照"]
                section = "landmark"
            elif s[:2] in ("- ", "· ", "* "):
                body = strip_src(s.lstrip("-·* ").strip())
                if section == "sub":
                    out.append(f"{circled[min(sub_n, 19)]} {body}")
                    sub_n += 1
                else:
                    out.append(f"· {body}")
            elif section in ("sub", "landmark", "sub_head"):
                out.append(clean)  # 段内续行
            else:
                out.append(clean)
        return "\n".join(out).strip()

    def wx_post(url: str, payload: dict) -> dict:
        # 微信接口对 \uXXXX 转义 JSON 有渲染 bug（2026-10 实测显示为 u3010 码），
        # 必须用原始 UTF-8 汉字 body 直传
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r = requests.post(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, timeout=30)
        r.raise_for_status()
        return r.json()

    # 主通道：客服文本消息（单条上限 2048 字节≈680 汉字；5~15 条动态条数下超长自动拆条续发）
    text = chat_format(brief)
    parts, buf = [], []
    for line in text.split("\n"):
        if sum(len(b.encode("utf-8")) for b in buf) + len(line.encode("utf-8")) > 900:
            if buf:
                parts.append("\n".join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        parts.append("\n".join(buf))
    ok = True
    for i, part in enumerate(parts, 1):
        suffix = f"\n（{i}/{len(parts)}）" if len(parts) > 1 else ""
        j1 = wx_post(
            f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}",
            {"touser": openid, "msgtype": "text", "text": {"content": part + suffix}},
        )
        if j1.get("errcode") != 0:
            print(f"⚠ 客服消息第{i}条失败 errcode={j1.get('errcode')} {j1.get('errmsg','')[:80]}，尝试模板消息兜底", file=sys.stderr)
            ok = False
            break
    if ok:
        print(f"已推送微信（客服消息，{len(parts)} 段共 {sum(len(p.encode('utf-8')) for p in parts)} 字节）")
        return

    # 降级：模板消息卡片（正文会被折叠，url 跳转 GitHub Actions 全文）

    # 降级：模板消息卡片（正文会被折叠，url 跳转 GitHub Actions 全文）
    if not tpl:
        raise RuntimeError("客服消息失败且未配 WEIXIN_TEMPLATE_ID")
    m = re.search(r"^\*\*一句话头条\*\*：(.+)", brief, re.M)
    headline = plain(m.group(1)) if m else f"个人头条 {NOW:%Y-%m-%d}"
    body_part = brief.split("**次条**", 1)[-1]
    content = plain(("**次条**" if body_part != brief else "") + body_part)[:400]
    j2 = wx_post(
        f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={token}",
        {
            "touser": openid,
            "template_id": tpl,
            "url": "https://github.com/TANXING2018/daily-brief/actions",
            "data": {
                "title": {"value": f"📡 {headline[:60]}"},
                "content": {"value": content or "今日无增量（点击查看全文）"},
            },
        },
    )
    if j2.get("errcode") != 0:
        raise RuntimeError(f"模板消息兜底也失败：{j2}")
    print("已推送微信（模板消息卡片+链接兜底）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不调模型不推送，打印提示词")
    ap.add_argument("--hours", type=float, default=36.0)
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    ranked = fetch_and_rank(args.hours, args.limit)
    if not ranked:
        brief = f"## 📡 个人头条 · {NOW:%Y-%m-%d}\n\n今日三个快讯源均未取到数据（源不可用）。"
    else:
        prompt = build_prompt(ranked, gauges(), fetch_feedback_hints(GH_TOKEN) if GH_TOKEN else "")
        if args.dry_run or not ZHIPU_API_KEY:
            print("=== DRY RUN：以下为将发给模型的提示词 ===\n")
            print(prompt)
            return 0
        try:
            brief = llm_brief(prompt)
        except Exception as e:
            # 降级保障（借 TrendRadar 机制）：LLM 全挂时按词表分出机械版，推送不中断
            print(f"⚠ LLM 全部候选失败，降级为机械版晨报：{e}", file=sys.stderr)
            top = ranked[:5]
            lines = [
                f"## 📡 个人头条 · {NOW:%Y-%m-%d}", "",
                "**一句话头条**：〔机械降级版〕" + top[0].title, "",
                "**次条**：",
            ]
            lines += [f"- {it.title}（{it.source} {it.ts:%m-%d %H:%M}）" for it in top[1:]]
            lines += ["", "**路标对照**：模型不可用，本版无仪表核对与凝练，详情见 GitHub Actions 日志"]
            brief = "\n".join(lines)

    print(brief)  # Actions 日志里留全文，推送失败时手机 GitHub App 可查
    if not args.dry_run:
        pushed = False
        if all(os.environ.get(k) for k in ("WEIXIN_APPID", "WEIXIN_SECRET", "WEIXIN_OPENID", "WEIXIN_TEMPLATE_ID")):
            try:
                push_weixin_test(brief)
                pushed = True
            except Exception as e:
                print(f"⚠ 微信测试号推送失败：{type(e).__name__} {str(e)[:100]}", file=sys.stderr)
        if SERVERCHAN_KEY:
            try:
                push_wechat(brief)
                pushed = True
            except Exception as e:
                print(f"⚠ 微信推送失败：{type(e).__name__} {str(e)[:100]}", file=sys.stderr)
        if os.environ.get("MAIL_USER") and os.environ.get("MAIL_PASS"):
            try:
                push_mail(brief)
                pushed = True
            except Exception as e:
                print(f"⚠ 邮件推送失败：{type(e).__name__} {str(e)[:100]}", file=sys.stderr)
        if not pushed:
            print("⚠ 未配置任何推送渠道（WEIXIN_* / SERVERCHAN_KEY / MAIL_*），仅打印", file=sys.stderr)
        if pushed and GH_TOKEN:
            post_brief_map(brief, GH_TOKEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
