from dotenv import load_dotenv
load_dotenv()  # must run before any module reads os.environ (pydantic-ai does this at import time)

import logging
from datetime import date, datetime, timezone

from telegram.ext import ContextTypes

import database as db
from ai_service import generate_daily_recommendation
from bot import create_application
from config import settings
from garmin_service import fetch_garmin_day

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def sync_all_garmin_users(context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    users = await db.get_all_garmin_users()
    logger.info("Garmin sync: %d users", len(users))

    for user in users:
        telegram_id = user["telegram_id"]
        token_path = user["garmin_token_path"]

        try:
            data = await fetch_garmin_day(token_path, date.today())
            if data:
                await db.upsert_activity(
                    telegram_id,
                    data["date"],
                    data["total_calories"],
                    data["active_calories"],
                    data["bmr_calories"],
                    data["steps"],
                )
                logger.info(
                    "Synced Garmin for user %d: %d kcal burned",
                    telegram_id,
                    data["total_calories"],
                )
        except Exception:
            logger.exception("Garmin sync failed for user %d", telegram_id)


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send each user their daily summary + AI recommendation."""
    users = await db.get_all_users()
    logger.info("Daily digest: %d users", len(users))

    for user in users:
        telegram_id = user["telegram_id"]
        first_name = user.get("first_name") or "there"
        has_garmin = bool(user.get("garmin_email"))

        try:
            food = await db.get_daily_food_summary(telegram_id)
            activity = await db.get_daily_activity(telegram_id)

            if food.entry_count == 0:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"👋 Hey {first_name}! You haven't logged any meals today.\n"
                        "Send me a photo of your food to start tracking! 📸"
                    ),
                )
                continue

            total_burned = activity.total_calories if activity else 0
            active_burned = activity.active_calories if activity else 0
            bmr = activity.bmr_calories if activity else 0
            steps = activity.steps if activity else 0

            rec = await generate_daily_recommendation(
                first_name=first_name,
                calories_in=food.total_calories,
                protein_g=food.total_protein_g,
                fat_g=food.total_fat_g,
                carbs_g=food.total_carbs_g,
                food_entries=food.entry_count,
                total_burned=total_burned,
                active_burned=active_burned,
                bmr=bmr,
                steps=steps,
                has_garmin=has_garmin,
            )

            balance = food.total_calories - total_burned
            sign = "+" if balance >= 0 else ""
            balance_emoji = "📈" if balance >= 0 else "📉"

            text = (
                f"🌙 *Evening Summary, {first_name}!*\n\n"
                f"🍽 Consumed: *{food.total_calories} kcal* ({food.entry_count} meals)\n"
                f"🏃 Burned: *{total_burned} kcal*\n"
                f"{balance_emoji} Balance: *{sign}{balance} kcal* ({rec.status})\n\n"
                f"💡 *AI Coach*\n{rec.analysis}\n\n"
                "*Recommendations:*\n"
                + "\n".join(f"• {r}" for r in rec.recommendations)
            )

            await context.bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Daily digest failed for user %d", telegram_id)


async def post_init(app) -> None:
    await db.init_db()
    logger.info("Database initialised at %s", settings.database_path)

    # Register bot command menu visible in Telegram UI
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("status", "Today's intake, activity & calorie balance"),
        BotCommand("summary", "Full AI analysis & meal recommendations"),
        BotCommand("garmin", "Connect your Garmin account"),
        BotCommand("start", "Welcome message & help"),
    ])

    app.job_queue.run_repeating(sync_all_garmin_users, interval=3600, first=10)
    # Send daily digest at 20:00 UTC every day
    app.job_queue.run_daily(send_daily_digest, time=datetime(2000, 1, 1, 20, 0, tzinfo=timezone.utc).timetz())


if __name__ == "__main__":
    import asyncio
    from bot import create_application_with_post_init

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    logger.info("Bot starting…")
    app = create_application_with_post_init(post_init)
    app.run_polling(drop_pending_updates=True)
