# Include inside the taoli.coinfetcher.xyz HTTPS server.
# The password file must be readable by nginx workers (root:nginx 0640).

# Unified homepage for the three services (arb console, HL grid, bulk grid).
# The arbitrage console keeps its canonical tokened URL: a request to / with
# a token query is proxied straight to it (behind the same password); every
# other request to / is the basic-auth navigation portal. deploy/home-portal/
# carries an __ARB_TOKEN__ placeholder that the deploy step substitutes from
# the server-side console-token.env, so the secret never enters the repo.
location = / {
    if ($entropy_grid_origin_allowed = 0) {
        return 403;
    }
    if ($arg_token != "") {
        proxy_pass http://127.0.0.1:8788;
    }
    auth_basic "Entropy";
    auth_basic_user_file /etc/nginx/entropy-grid.htpasswd;
    auth_delay 1s;
    root /var/www/entropy-home-portal;
    try_files /index.html =404;
    access_log off;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}

location = /grid {
    return 308 /;
}

# The homepage above replaces this portal; old bookmarks land on it.
location = /grid/ {
    return 308 /;
}

# The Hyperliquid console keeps its backend untouched; only its public
# index path moved below the portal. Assets and API still flow through
# the prefix location below, exactly as before the move.
location = /grid/hl {
    return 308 /grid/hl/;
}

location = /grid/hl/ {
    if ($entropy_grid_origin_allowed = 0) {
        return 403;
    }
    auth_basic "Entropy Grid";
    auth_basic_user_file /etc/nginx/entropy-grid.htpasswd;
    auth_delay 1s;
    client_max_body_size 64k;
    access_log off;

    proxy_pass http://127.0.0.1:8789/;
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:8789;
    proxy_set_header Origin http://127.0.0.1:8789;
    proxy_set_header Authorization "";
    proxy_set_header Accept-Encoding "";
    proxy_set_header Connection "";
    proxy_read_timeout 180s;

    sub_filter_once off;
    sub_filter_types application/javascript text/javascript text/css;
    sub_filter '/static/' '/grid/static/';
    sub_filter '/api/grid' '/grid/api/grid';
    sub_filter 'entropyConsoleToken' 'entropyGridConsoleToken';
}

location ^~ /grid/ {
    if ($entropy_grid_origin_allowed = 0) {
        return 403;
    }
    auth_basic "Entropy Grid";
    auth_basic_user_file /etc/nginx/entropy-grid.htpasswd;
    auth_delay 1s;
    client_max_body_size 64k;
    access_log off;

    proxy_pass http://127.0.0.1:8789/;
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:8789;
    proxy_set_header Origin http://127.0.0.1:8789;
    proxy_set_header Authorization "";
    proxy_set_header Accept-Encoding "";
    proxy_set_header Connection "";
    proxy_read_timeout 180s;

    # Publish the existing independent UI under its own path. The runtime
    # and source assets remain unchanged while a live grid is running.
    sub_filter_once off;
    sub_filter_types application/javascript text/javascript text/css;
    sub_filter '/static/' '/grid/static/';
    sub_filter '/api/grid' '/grid/api/grid';
    sub_filter 'entropyConsoleToken' 'entropyGridConsoleToken';
}
