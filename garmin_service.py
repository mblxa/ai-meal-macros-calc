import asyncio
import logging
from datetime import date
from functools import partial
from pathlib import Path

from garminconnect import Garmin, GarminConnectAuthenticationError

logger = logging.getLogger(__name__)


def _connect_with_tokens(tokenstore_path: str) -> Garmin:
    api = Garmin()
    api.login(tokenstore_path)
    return api


def _connect_and_save_tokens(email: str, password: str, tokenstore_path: str) -> Garmin:
    Path(tokenstore_path).parent.mkdir(parents=True, exist_ok=True)
    api = Garmin(email=email, password=password)
    api.login(tokenstore_path)
    return api


def _fetch_stats(api: Garmin, day: date) -> dict:
    return api.get_stats(day.isoformat())


async def login_and_save_tokens(email: str, password: str, tokenstore_path: str) -> bool:
    """Authenticate with email/password, save OAuth tokens to *tokenstore_path*. Returns True on success."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None, partial(_connect_and_save_tokens, email, password, tokenstore_path)
        )
        return True
    except GarminConnectAuthenticationError:
        logger.warning("Garmin auth failed for %s", email)
        return False
    except Exception as exc:
        logger.warning("Garmin login error: %s", exc)
        return False


async def fetch_garmin_day(tokenstore_path: str, day: date | None = None) -> dict | None:
    """Return calories summary for *day* (defaults to today) using saved OAuth tokens."""
    target = day or date.today()
    loop = asyncio.get_event_loop()

    try:
        api = await loop.run_in_executor(
            None, partial(_connect_with_tokens, tokenstore_path)
        )
        stats = await loop.run_in_executor(None, partial(_fetch_stats, api, target))
    except GarminConnectAuthenticationError:
        logger.warning("Garmin token auth failed (tokenstore: %s)", tokenstore_path)
        return None
    except Exception as exc:
        logger.warning("Garmin fetch error: %s", exc)
        return None

    total_kcal = int(stats.get("totalKilocalories") or stats.get("totalKiloCalories") or 0)
    active_kcal = int(stats.get("activeKilocalories") or stats.get("activeKiloCalories") or 0)
    bmr_kcal = int(stats.get("bmrKilocalories") or stats.get("bmrKiloCalories") or 0)
    steps = int(stats.get("totalSteps") or 0)

    if bmr_kcal == 0 and total_kcal > active_kcal:
        bmr_kcal = total_kcal - active_kcal

    return {
        "date": target,
        "total_calories": total_kcal,
        "active_calories": active_kcal,
        "bmr_calories": bmr_kcal,
        "steps": steps,
    }
