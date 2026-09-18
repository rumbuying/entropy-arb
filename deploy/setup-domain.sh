#!/usr/bin/env bash
# entropy-arb 控制台域名接入一键脚本（幂等，可重复跑）
#
# 用法（DNS A 记录生效后执行）:
#   ./deploy/setup-domain.sh arb.coinfetcher.xyz
#
# 做的事:
#   1. 写入 nginx HTTP server 块（80，供 certbot 验证 + 临时代理）
#   2. certbot 签发 Let's Encrypt 证书（--nginx 插件）
#   3. 写入最终 HTTPS server 块（443，反代 127.0.0.1:8788，支持 WebSocket）
#   4. nginx -t 校验后 reload，并做无 token 401 / 带 token 200 双向验证
set -eu
DOMAIN="${1:?用法: setup-domain.sh <域名>，例 arb.coinfetcher.xyz}"
TOKEN=$(grep -oP '(?<=--token )\S+' /etc/systemd/system/entropy-console.service)
[ -n "$TOKEN" ] || { echo "未在 unit 里找到 --token"; exit 1; }
HTTP_CONF=/etc/nginx/conf.d/entropy-console-http.conf
HTTPS_CONF=/etc/nginx/conf.d/entropy-console-https.conf

echo "== 1/4 HTTP server 块 ($DOMAIN) =="
cat > "$HTTP_CONF" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:8788;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
    }
}
EOF
nginx -t && systemctl reload nginx

echo "== 2/4 签发证书 =="
if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || \
    certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
fi
CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
KEY=/etc/letsencrypt/live/$DOMAIN/privkey.pem
[ -f "$CERT" ] || { echo "证书签发失败，检查 DNS 是否已生效: dig +short $DOMAIN"; exit 1; }

echo "== 3/4 HTTPS server 块 =="
cat > "$HTTPS_CONF" <<EOF
server {
    listen 443 ssl;
    http2 on;
    server_name $DOMAIN;

    ssl_certificate     $CERT;
    ssl_certificate_key $KEY;

    # 控制台静态页 + API + WebSocket
    location / {
        proxy_pass http://127.0.0.1:8788;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;      # WebSocket
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600s;                     # 长连 WS 不被掐
        proxy_send_timeout 3600s;
    }
}

server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}
EOF
rm -f "$HTTP_CONF"
nginx -t && systemctl reload nginx

echo "== 4/4 验证 =="
sleep 1
code_no_token=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/workers")
code_with=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/meta?token=$TOKEN")
page=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/?token=$TOKEN")
echo "无 token API: $code_no_token (期望 401)"
echo "带 token API: $code_with (期望 200)"
echo "带 token 页面: $page (期望 200)"
[ "$code_no_token" = "401" ] && [ "$code_with" = "200" ] && [ "$page" = "200" ] \
  && echo "全部通过 ✅  访问地址: https://$DOMAIN/?token=$TOKEN" \
  || { echo "验证未通过，检查上方状态码"; exit 1; }
