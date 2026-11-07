"""Production gateway entry point: python service_gateway.py (separate process)."""
from app.config import load_config
from app.db import init_db
from app.service_gateway import run

if __name__ == "__main__":
    init_db()
    run(load_config())
