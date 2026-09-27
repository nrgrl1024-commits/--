#!/usr/bin/env bash
# 一键安装：在一台全新的 Ubuntu 22.04 / 24.04 云服务器上，进入项目目录后运行
#   sudo bash deploy/install.sh
# 会依次：安装 ffmpeg 等依赖 → 建 Python 环境 → 填写密钥 → 初始化飞书表格 → 设为开机自动运行。
# 可以重复运行；已经填过的密钥不会被覆盖。
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=fengzai-video
PIP_MIRROR="https://mirrors.aliyun.com/pypi/simple"

step() { echo; echo "==== $* ===="; }

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行：sudo bash deploy/install.sh"
  exit 1
fi

step "1/5 安装系统依赖（ffmpeg、Python、中文字体）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ffmpeg fontconfig fonts-wqy-zenhei git

step "2/5 创建 Python 环境并安装依赖"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip -i "$PIP_MIRROR"
.venv/bin/pip install -q -r requirements.txt -i "$PIP_MIRROR"

step "3/5 填写密钥"
touch .env
chmod 600 .env
ask() {  # ask 变量名 说明
  local key="$1" hint="$2" value
  if grep -q "^${key}=." .env; then
    echo "  ${key} 已填写，跳过"
    return
  fi
  read -r -p "  请输入 ${hint}：" value
  sed -i "/^${key}=/d" .env
  echo "${key}=${value}" >> .env
}
ask FEISHU_APP_ID "飞书 App ID（cli_ 开头）"
ask FEISHU_APP_SECRET "飞书 App Secret"
ask FEISHU_APP_TOKEN "多维表格链接里 base/ 后面那一串"
ask ARK_API_KEY "豆包（火山方舟）API Key"
ask ARK_MODEL "豆包模型名（doubao- 开头或 ep- 开头）"
[ -f config.yaml ] || cp config.example.yaml config.yaml

step "4/5 初始化飞书表格（建「门店资料」「母版」「今日任务」，给各门店页补字段）"
if ! .venv/bin/python -m fengzai_video setup; then
  echo
  echo "初始化失败。常见原因：应用没发布、没把应用加进多维表格、权限没开通、密钥填错。"
  echo "改正后重新运行本脚本即可；要改密钥请编辑 $APP_DIR/.env"
  exit 1
fi

step "5/5 设为开机自动运行"
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=蜂仔翻新门店短视频流水线
After=network-online.target

[Service]
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python -m fengzai_video watch --interval 120
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ${SERVICE}

echo
echo "安装完成，系统已经在后台运行。"
echo "  查看运行日志：journalctl -u ${SERVICE} -f"
echo "  重启：        systemctl restart ${SERVICE}"
echo "  停止：        systemctl stop ${SERVICE}"
echo
echo "下一步：到飞书「门店资料」页核对城市，填好即梦主播和企微群机器人；"
echo "字体和背景音乐放进 ${APP_DIR}/assets/fonts 和 assets/music 后，执行 systemctl restart ${SERVICE}。"
