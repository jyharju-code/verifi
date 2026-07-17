"""Central configuration. Everything comes from environment variables.

Load order: process env wins, then .env in the repo root (dev convenience).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://verifi:verifi@localhost:5432/verifi")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://verifi.cloud")

# polling for local development, webhook in production
BOT_MODE = os.environ.get("BOT_MODE", "polling")
BOT_PORT = int(os.environ.get("BOT_PORT", "8701"))
# 127.0.0.1 on bare metal; 0.0.0.0 inside a container where the docker network isolates it.
BOT_LISTEN = os.environ.get("BOT_LISTEN", "127.0.0.1")
CORE_API_PORT = int(os.environ.get("CORE_API_PORT", "8700"))

# Flat commission for free tier verifies (paid verifies use instances.associate_commission).
FREE_COMMISSION_USD = float(os.environ.get("FREE_COMMISSION_USD", "0.50"))

# Phase 2 switch: when true, payout.py actually runs awal. Until then it only prints.
PAYOUTS_AUTO = os.environ.get("PAYOUTS_AUTO", "false").lower() == "true"

# Agent-facing wait budget for a synchronous verify response.
VERIFY_WAIT_TIMEOUT_S = int(os.environ.get("VERIFY_WAIT_TIMEOUT_S", "110"))

# Long random token for the /admin dashboard. Empty disables the dashboard.
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
