# browser.example.com -> g-browser (127.0.0.1:8098)
# Example only: replace browser.example.com with your own hostname. Nothing
# reads this file at runtime on the reference host, where a tunnel sends the
# hostname straight to 127.0.0.1:8098.
#
# Send everything, /vnc/ included, to the app. vncproxy.py checks the admin
# cookie before it proxies the noVNC client and its websocket to websockify on
# 127.0.0.1:6080. Never proxy straight to 6080: x11vnc runs without a VNC
# password, so that would hand the logged-in browser to anyone with the URL.
# Run certbot after enabling to add the TLS block.

server {
    listen 80;
    server_name browser.example.com;

    location / {
        proxy_pass http://127.0.0.1:8098;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # The live view holds a websocket open for as long as someone watches.
        proxy_buffering off;
        proxy_read_timeout 86400s;
    }
}
