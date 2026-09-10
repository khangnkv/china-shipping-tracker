# Gunicorn config for the shipping tracker (behind Caddy).
bind = "127.0.0.1:5000"
workers = 2                 # low traffic; SQLite + WAL handles this fine
timeout = 30
graceful_timeout = 30
accesslog = "/var/log/shipping/access.log"
errorlog = "/var/log/shipping/error.log"
loglevel = "info"
