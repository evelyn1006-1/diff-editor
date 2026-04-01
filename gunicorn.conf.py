from pathlib import Path

from app_runtime import EDITOR_BIND

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

LOG_DIR.mkdir(parents=True, exist_ok=True)

bind = EDITOR_BIND
workers = 2
timeout = 600  # 10 minutes
worker_class = "geventwebsocket.gunicorn.workers.GeventWebSocketWorker"
forwarded_allow_ips = "127.0.0.1"

loglevel = "info"
capture_output = True
errorlog = str(LOG_DIR / "server.log")
accesslog = str(LOG_DIR / "access.log")
