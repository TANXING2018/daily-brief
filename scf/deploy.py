"""腾讯云函数部署脚本：SCF 创建/更新 + API 网关（/wx、/wx-diag）+ 发布。
凭据从环境变量读：TENCENT_SECRET_ID / TENCENT_SECRET_KEY / GH_PAT / CF_TOKEN
用法：python deploy.py（在 tools/cloud/scf 目录）
"""
import base64
import io
import json
import os
import sys
import time
import zipfile

from tencentcloud.common import credential
from tencentcloud.common.exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.scf.v20180416 import scf_client, models as scf_models
from tencentcloud.apigateway.v20180808 import apigateway_client, models as apig_models
from tencentcloud.cam.v20190116 import cam_client, models as cam_models

ROLE_NAME = "SCF_InvokedRole"  # SCF 默认运行角色（控制台首建函数时自动创建的那个）

REGION = "ap-guangzhou"
FUNC_NAME = "daily-brief-feedback"

SID = os.environ["TENCENT_SECRET_ID"]
SKEY = os.environ["TENCENT_SECRET_KEY"]
GH_PAT = os.environ["GH_PAT"]
CF_TOKEN = os.environ["CF_TOKEN"]

ENV_VARS = [
    {"Key": "WX_TOKEN", "Value": "fz2026radar"},
    {"Key": "WX_APPID", "Value": "wx279a10c5972aba69"},
    {"Key": "WX_SECRET", "Value": "ca327e5a70137631a57d20d535cb99a6"},
    {"Key": "GH_REPO", "Value": "TANXING2018/daily-brief"},
    {"Key": "GH_ISSUE", "Value": "1"},
    {"Key": "GH_PAT", "Value": GH_PAT},
    {"Key": "ZHIPU_API_KEY", "Value": os.environ.get("ZHIPU_KEY", "")},
    {"Key": "DEEPSEEK_API_KEY", "Value": os.environ["DEEPSEEK_KEY"]},
    {"Key": "CF_ACCOUNT", "Value": "ca74f1a198cb665cb9f6ec43d9445638"},
    {"Key": "CF_KV_NS", "Value": "5f9739157f014da59d98ebe2d9c0c717"},
    {"Key": "CF_TOKEN", "Value": CF_TOKEN},
]


def call(client, action_model_cls, payload: dict):
    """用 from_json_string 组装请求对象并调用。"""
    req = action_model_cls()
    req.from_json_string(json.dumps(payload))
    # 按约定：action 名 = 模型名去掉 Request 后缀
    action = action_model_cls.__name__.removesuffix("Request")
    return getattr(client, action)(req)


def wait_active(scf):
    for _ in range(20):
        info = call(scf, scf_models.GetFunctionRequest, {"FunctionName": FUNC_NAME})
        if info.Status == "Active":
            return
        if info.Status not in ("Creating", "Updating"):
            raise RuntimeError(f"函数状态异常: {info.Status}")
        time.sleep(3)


def main():
    cred = credential.Credential(SID, SKEY)
    prof = ClientProfile()
    scf = scf_client.ScfClient(cred, REGION, prof)
    apig = apigateway_client.ApigatewayClient(cred, REGION, prof)
    cam = cam_client.CamClient(cred, "", prof)  # CAM 无区域

    # 0) 确保 SCF 运行角色存在（新账号没有，API 建函数前必须先建）
    role_arn = None
    try:
        r = call(cam, cam_models.DescribeRoleListRequest, {"Page": 1, "Rp": 100})
        for role in r.List:
            if role.RoleName == ROLE_NAME:
                role_arn = role.RoleArn
                break
    except TencentCloudSDKException as e:
        print("查询角色失败（继续尝试创建）:", e.message[:80])
    if not role_arn:
        call(cam, cam_models.CreateRoleRequest, {
            "RoleName": ROLE_NAME,
            "PolicyDocument": json.dumps({
                "version": "2.0",
                "statement": [{
                    "action": "sts:AssumeRole", "effect": "allow",
                    "principal": {"service": ["scf.qcloud.com", "apigateway.qcloud.com"]},
                }]})})
        r = call(cam, cam_models.DescribeRoleListRequest, {"Page": 1, "Rp": 100})
        role_arn = next(role.RoleArn for role in r.List if role.RoleName == ROLE_NAME)
        print("运行角色已创建:", role_arn)

    # 1) 打包代码
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write("index.js", "index.js")
    zip_b64 = base64.b64encode(buf.getvalue()).decode()

    # 2) 创建或更新函数
    try:
        call(scf, scf_models.CreateFunctionRequest, {
            "FunctionName": FUNC_NAME,
            "Runtime": "Nodejs16.13",
            "Handler": "index.main_handler",
            "Code": {"ZipFile": zip_b64},
            "MemorySize": 256,
            "Timeout": 60,
            "Environment": {"Variables": ENV_VARS},
            "Description": "微信晨报反馈机器人",
        })
        print("函数已创建")
    except TencentCloudSDKException as e:
        if e.code == "ResourceInUse.Function" or "already exist" in e.message.lower():
            wait_active(scf)
            call(scf, scf_models.UpdateFunctionCodeRequest,
                 {"FunctionName": FUNC_NAME, "ZipFile": zip_b64})
            wait_active(scf)
            call(scf, scf_models.UpdateFunctionConfigurationRequest, {
                "FunctionName": FUNC_NAME,
                "Environment": {"Variables": ENV_VARS}, "Timeout": 60})
            print("函数已更新")
        else:
            raise

    wait_active(scf)
    print("函数状态: Active")
    print("（函数 URL 已在控制台开启，地址不变：https://1492481560-j897ak9n7b.ap-guangzhou.tencentscf.com/wx）")


def _unused():
    """API 网关 2025-06 停售、函数 URL 无公开 API（控制台管理）——历史尝试代码已删。"""
    return


if __name__ == "__main__":
    sys.exit(main())
