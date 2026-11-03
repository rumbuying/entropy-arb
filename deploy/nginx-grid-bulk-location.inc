# Include next to the existing grid location in the HTTPS server.
# Separate account entry while the running HL execution version is frozen.
location = /grid/bulk {
    return 308 /grid/bulk/;
}

location ^~ /grid/bulk/ {
    if ($entropy_grid_origin_allowed = 0) {
        return 403;
    }
    auth_basic "Entropy Grid";
    auth_basic_user_file /etc/nginx/entropy-grid.htpasswd;
    auth_delay 1s;
    client_max_body_size 64k;
    access_log off;

    proxy_pass http://127.0.0.1:8790/;
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:8790;
    proxy_set_header Origin http://127.0.0.1:8790;
    proxy_set_header Authorization "";
    proxy_set_header Accept-Encoding "";
    proxy_set_header Connection "";
    proxy_read_timeout 60s;
    sub_filter_once off;
    sub_filter_types application/javascript text/javascript text/css;
    sub_filter '/static/' '/grid/bulk/static/';
    sub_filter '/api/grid' '/grid/bulk/api/grid';
    sub_filter 'entropyConsoleToken' 'entropyBulkConsoleToken';
}
