"""Entry point: boot the development server.

The application factory lives in ``app/__init__.py`` (``create_app``); this thin
module only loads config and starts the server. Run with ``python main.py``.
"""

import sys
import subprocess
from pathlib import Path

from app import config as app_config
from app import create_app


def main() -> None:
    cfg = app_config.load_config()
    app = create_app()
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8080))
    debug = bool(cfg.get("debug", False))
    print(f"Serving on http://{host}:{port} (debug={debug})", file=sys.stderr)
    # Separate event loop/process for streaming and WebSockets. WSGI production
    # deployments run service_gateway.py as a separate managed service instead.
    gateway = subprocess.Popen([sys.executable, str(Path(__file__).parent / "service_gateway.py")])
    try:
        app.run(host=host, port=port, debug=debug, use_reloader=False)
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)


if __name__ == "__main__":
    main()
