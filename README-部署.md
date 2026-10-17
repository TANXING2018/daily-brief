# 云端晨报部署指引（GitHub Actions 免费版）

把每日新闻晨报搬到 GitHub 的免费服务器上跑，电脑关机也照常推送到微信。
母本在本目录（`daily_brief.py` / `github-workflow-daily-brief.yml` / `requirements.txt`），
部署时另加一份画像副本，共 **4 个文件**传到 GitHub。

**原理**：GitHub Actions 每天北京时间 08:30（UTC 00:30，可能延迟几分钟到几十分钟）
在它的服务器上跑 `daily_brief.py`：抓三源快讯 → 关键词初筛 → 拉金价/美债仪表 →
调智谱大模型按你的画像凝练 → Server酱推送到你微信。

**费用**：全部 0 元。GitHub Public 仓库 Actions 无限免费分钟；智谱 flash 系模型免费档
（2026-10 实况：`glm-4-flash` 已下线，脚本内置降级链 glm-4.7-flash→glm-4.5-flash→glm-4-flash-250414，
平台改名/拥挤自动换挡）。

> **2026-10-17 已部署完成**：仓库 `github.com/TANXING2018/daily-brief`（用户选 Public）。
> 推送主通道=**微信测试号客服消息**（普通聊天气泡，晨报全文直读）。两个实测坑：
> ①模板消息正文会被微信折叠不显示，弃用为兜底（带 url 跳 GitHub Actions 全文）；
> ②**微信接口对 \uXXXX 转义 JSON 有渲染 bug（显示成 u3010 码），必须 ensure_ascii=False
> 原始 UTF-8 body 直传**（daily_brief.py 的 wx_post 函数）。测试号四把钥匙
> WEIXIN_APPID/SECRET/OPENID/TEMPLATE_ID 已配。Server酱（用户网络打不开 ftqq 域名）与
> QQ 邮箱通道（MAIL_USER/MAIL_PASS）代码保留未启用。

---

## 一、注册三个账号、拿三把钥匙（只能本人做）

1. **GitHub**（<https://github.com>，已有跳过）
2. **智谱开放平台**（<https://bigmodel.cn>）：注册 → 控制台 → API Keys →
   新建并复制 key（形如 `xxxx.xxxx`）。免费模型无需充值。
3. **Server酱**（<https://sctapi.ftqq.com>，或搜"Server酱"）：微信扫码登录 →
   复制 **SendKey**（形如 `SCTxxxx`）。
   先在它网页上手动发一条测试消息，确认微信能收到再往下走。

## 二、建仓库、传文件（网页操作，不用装任何软件）

1. GitHub 首页右上 **+** → New repository：
   - Repository name 随意（如 `daily-brief`）；
   - 选 **Private**（必选！画像里有你的个人研判，不能公开）；
   - 勾选 Add a README → Create。
2. 进仓库 → **Add file → Create new file**，逐个创建（内容从本目录复制）：
   | 仓库里的路径 | 内容来源 |
   |---|---|
   | `daily_brief.py` | 本目录 `daily_brief.py` |
   | `profile.md` | `research/news-radar-画像.md` 复制一份 |
   | `requirements.txt` | 本目录 `requirements.txt` |
   | `.github/workflows/daily-brief.yml` | 本目录 `github-workflow-daily-brief.yml` |
   - 网页建嵌套目录：在文件名输入框直接输 `.github/workflows/daily-brief.yml` 即可；
   - 每个文件粘贴内容后 Commit changes。
3. （可选）Actions 默认分支才生效——只用 main 分支即可。

## 三、填钥匙（Secrets）

仓库页 → **Settings → Secrets and variables → Actions** → New repository secret，加两个：

| Name | Secret |
|---|---|
| `ZHIPU_API_KEY` | 智谱的 key |
| `SERVERCHAN_KEY` | Server酱的 SendKey |

钥匙只存在这里，不会出现在代码里，仓库即使泄露也不会丢钥匙。

## 四、测试与日常

1. 仓库页 → **Actions** 标签 → 左侧 `daily-brief` → 右侧 **Run workflow** 手动触发一次；
2. 等几分钟，微信应收到「📡 …」开头的晨报；
3. 之后每天 08:30 左右自动来。没收到时：Actions 页看该次运行的红色日志，
   点开能看到完整输出（晨报全文会打印在日志里，手机 GitHub App 也能看）。

## 五、常见问题

- **源不可用告警**：GitHub 服务器在海外，访问东财/财联社/新浪接口偶有失败。
  脚本会注明哪个源挂了；三源全挂会推送"源不可用"。若连续多天如此，
  考虑改走国内云函数方案（腾讯云 SCF / 阿里云 FC，控制台建定时触发器跑同一个脚本）。
- **想改推送时间**：编辑仓库里 `.github/workflows/daily-brief.yml` 的 cron
  （UTC 时间 = 北京时间 − 8 小时），如北京 12:00 = `0 4 * * *`。
- **想改筛选标准/路标**：改本地 `research/news-radar-画像.md` 后，把新内容
  同步到仓库里的 `profile.md`（网页打开 → 铅笔图标 → 粘贴 → Commit）。
  关键词词表在 `daily_brief.py` 顶部，同样两边同步。
- **模型换挡**：默认 `glm-4-flash`（免费）。想用更强模型：Settings →
  Secrets and variables → **Variables** → New variable：Name=`ZHIPU_MODEL`，
  Value=模型名（收费档按 token 计费，晨报一天一次费用极小）。

## 六、与本地版（ZCode 定时晨报）的关系

两套并存不冲突：云端版保证关机也有推送；本地版（每天 8:30 的 ZCode 自动化）
有完整会话上下文与 MCP 工具，质量略高。觉得重复可在 ZCode 里删掉本地那条定时任务。
