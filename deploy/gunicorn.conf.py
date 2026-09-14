# Gunicorn config for the shipping tracker (behind Caddy).
bind = "127.0.0.1:5000"
workers = 3                 # low traffic; extra headroom so one slow outbound call (LINE/Anthropic) doesn't block everything else
timeout = 30
graceful_timeout = 30
accesslog = "/var/log/shipping/access.log"
errorlog = "/var/log/shipping/error.log"
loglevel = "info"
