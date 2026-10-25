import json

import requests

APPID = "wx279a10c5972aba69"
SECRET = "ca327e5a70137631a57d20d535cb99a6"
OPENID = "oxWVy3MGsrTtX2yoejt68YUhG-Vc"

token = requests.get(
    "https://api.weixin.qq.com/cgi-bin/token",
    params={"grant_type": "client_credential", "appid": APPID, "secret": SECRET},
    timeout=30,
).json()["access_token"]

text = open(r"D:\workspace2\AIfinance\data\cache\wx_test_msg.txt", encoding="utf-8").read()
payload = {"touser": OPENID, "msgtype": "text", "text": {"content": text}}
# 关键差异：ensure_ascii=False —— body 里直接是 UTF-8 汉字，而非 \uXXXX 转义序列
body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
r = requests.post(
    f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}",
    data=body,
    headers={"Content-Type": "application/json; charset=utf-8"},
    timeout=30,
).json()
print("发送结果:", r, "| 字节数:", len(body))
