#!/usr/bin/env bash
# 门店混剪 · 腾讯云轻量服务器（Ubuntu 22.04 / 24.04）一键安装
# 用法：在 mixcut 目录里运行  sudo bash install.sh
# 改密码、填密钥：重新运行一次即可
set -euo pipefail

cd "$(dirname "$0")"
APP_DIR="$(pwd)"
ENV_FILE=/etc/mixcut.env
PIP_MIRROR=https://mirrors.cloud.tencent.com/pypi/simple

if [[ $EUID -ne 0 ]]; then echo "请用 sudo 运行：sudo bash install.sh"; exit 1; fi

echo "== 1/4 安装系统软件 =="
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip ffmpeg curl >/dev/null

echo "== 2/4 安装 Python 依赖 =="
python3 -m venv .venv
.venv/bin/pip install -q -i "$PIP_MIRROR" --upgrade pip
.venv/bin/pip install -q -i "$PIP_MIRROR" -r requirements.txt

echo "== 3/4 填写配置 =="
[[ -f $ENV_FILE ]] && source "$ENV_FILE"
ask() {  # ask 变量名 提示 [是否可空]
  local name=$1 tip=$2 cur=${!1:-}
  local shown=${cur:+（直接回车保留现在的）}
  read -r -p "$tip$shown：" val
  val=${val:-$cur}
  if [[ -z $val && ${3:-} != optional ]]; then echo "不能为空"; ask "$@"; return; fi
  printf -v "$name" '%s' "$val"
}
ask ACCESS_PASSWORD "设置访问密码（打开网页时要输入）"
ask DASHSCOPE_API_KEY "阿里云百炼 API Key（sk- 开头；还没有就直接回车，以后再填）" optional
IP=$(curl -s -m 3 http://metadata.tencentyun.com/latest/meta-data/public-ipv4 || true)
PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-${IP:+http://$IP}}
ask PUBLIC_BASE_URL "服务器网址（例如 http://1.2.3.4）"
TTS_MODEL=${TTS_MODEL:-cosyvoice-v3-plus}

umask 077
cat > "$ENV_FILE" <<EOF
ACCESS_PASSWORD=$ACCESS_PASSWORD
DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY
PUBLIC_BASE_URL=$PUBLIC_BASE_URL
TTS_MODEL=$TTS_MODEL
MIXCUT_DATA=$APP_DIR/data
EOF

echo "== 4/4 设为开机自动运行 =="
cat > /etc/systemd/system/mixcut.service <<EOF
[Unit]
Description=门店混剪
After=network.target

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/gunicorn -w 2 --threads 4 --timeout 180 -b 0.0.0.0:80 server:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now mixcut >/dev/null
systemctl restart mixcut
sleep 2

if systemctl is-active --quiet mixcut; then
  echo
  echo "✅ 装好了。用浏览器打开：$PUBLIC_BASE_URL"
  echo "   用户名随便填，密码是刚才设置的访问密码"
  echo "   打不开的话：到腾讯云控制台 → 轻量应用服务器 → 防火墙，放行 TCP 80 端口"
  [[ -z $DASHSCOPE_API_KEY ]] && echo "   还没填阿里云 API Key，声音复刻暂时不能用；拿到后重新运行 sudo bash install.sh"
else
  echo "❌ 启动失败，查看原因：journalctl -u mixcut -n 50"
  exit 1
fi
