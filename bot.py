import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database as db
from ai_service import analyze_day_food, analyze_food_photo, generate_daily_recommendation, generate_workout_nutrition
from config import settings
from garmin_service import fetch_garmin_day, login_and_save_tokens

# ConversationHandler states
_ASK_GOAL_TYPE, _ASK_ACTIVITY, _ASK_WEIGHT, _ASK_TEXT_GOALS = range(4)
_ASK_WORKOUT_CALORIES = 4
_ASK_WORKOUT_TYPE, _ASK_WORKOUT_MINUTES = 5, 6
_EF_SELECT_ENTRY, _EF_SELECT_ACTION, _EF_SELECT_FIELD, _EF_ENTER_VALUE = 7, 8, 9, 10

_EF_FIELD_LABELS = {
    "calories":    "🔥 Calories (kcal)",
    "protein_g":   "💪 Protein (g)",
    "fat_g":       "🧈 Fat (g)",
    "carbs_g":     "🍞 Carbs (g)",
    "description": "📝 Description",
}

GOAL_LABELS = {
    "fat_loss": "🔥 Lose Fat",
    "muscle_building": "💪 Build Muscle",
    "maintenance": "⚖️ Maintain",
}
ACTIVITY_LABELS = {
    "sedentary": "🪑 Sedentary",
    "light": "🚶 Light",
    "moderate": "🏃 Moderate",
    "high": "⚡ High",
}

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _macro_bar(value: float, total: float, width: int = 10) -> str:
    if total == 0:
        return "░" * width
    filled = round((value / total) * width)
    return "█" * filled + "░" * (width - filled)


def _macro_pcts(protein_g: float, fat_g: float, carbs_g: float) -> tuple[float, float, float]:
    """Return calorie-based macro percentages (protein, fat, carbs)."""
    p_kcal = protein_g * 4
    f_kcal = fat_g * 9
    c_kcal = carbs_g * 4
    total = p_kcal + f_kcal + c_kcal or 1
    return p_kcal / total * 100, f_kcal / total * 100, c_kcal / total * 100


_ACTIVITY_MULTIPLIER = {
    "sedentary": 1.20,
    "light":     1.375,
    "moderate":  1.55,
    "high":      1.725,
}


def _estimate_tdee(weight_kg: float, activity_level: str | None) -> int:
    bmr = weight_kg * 22  # rough BMR without height/age
    return int(bmr * _ACTIVITY_MULTIPLIER.get(activity_level or "moderate", 1.55))


def _target_macros(
    weight_kg: float, total_burned: int, goal_type: str, activity_level: str | None = None
) -> tuple[float, float, float, int]:
    """Return (protein_g, fat_g, carbs_g, target_kcal) for the given profile."""
    protein_per_kg = {"fat_loss": 2.2, "muscle_building": 2.2, "maintenance": 2.0}.get(goal_type, 2.0)
    fat_pct        = {"fat_loss": 0.30, "muscle_building": 0.25, "maintenance": 0.28}.get(goal_type, 0.28)
    calorie_delta  = {"fat_loss": -300,  "muscle_building": +300,  "maintenance": 0}.get(goal_type, 0)

    # Use estimated TDEE as a floor so targets stay sensible when Garmin hasn't fully synced
    base_burned = max(total_burned, _estimate_tdee(weight_kg, activity_level))
    target_kcal = max(1200, base_burned + calorie_delta)
    protein_g = weight_kg * protein_per_kg
    fat_g = (target_kcal * fat_pct) / 9
    carbs_kcal = max(0, target_kcal - protein_g * 4 - fat_g * 9)
    carbs_g = carbs_kcal / 4
    return protein_g, fat_g, carbs_g, target_kcal


async def _ensure_user(update: Update) -> None:
    user = update.effective_user
    await db.upsert_user(user.id, user.username, user.first_name)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ensure_user(update)
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Hey {name}! I'm your personal calorie tracker.\n\n"
        "Here's what I can do:\n"
        "• Send me a *photo of your food* (with optional caption) → I'll estimate calories & macros\n"
        "• /status — today's intake, activity & calorie balance\n"
        "• /analyze — AI review of today's food choices (good/bad + next meal tip)\n"
        "• /workout — pre & post workout nutrition advice based on today's meals\n"
        "• /summary — full AI analysis & meal recommendations\n"
        "• /setgoals — set your nutrition & fitness goals for the AI coach\n"
        "• /editfood — edit or delete a logged meal\n"
        "• /addworkout — manually log calories burned in a workout\n"
        "• /garmin — connect your Garmin account for workout data\n\n"
        "I'll also send you an *evening digest* at 8 PM with your daily summary & tips. 🌙\n\n"
        "Let's get started! 🥗",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /garmin
# ---------------------------------------------------------------------------

async def cmd_garmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ensure_user(update)
    args = context.args

    if len(args) != 2:
        await update.message.reply_text(
            "Usage: `/garmin your@email.com yourpassword`\n\n"
            "⚠️ Send this in a private chat. Credentials are used once to obtain OAuth tokens and are not stored.",
            parse_mode="Markdown",
        )
        return

    email, password = args[0], args[1]
    msg = await update.message.reply_text("🔄 Connecting to Garmin…")

    token_dir = Path(settings.garmin_token_dir).expanduser()
    token_path = str(token_dir / f"user_{update.effective_user.id}.json")

    ok = await login_and_save_tokens(email, password, token_path)
    if not ok:
        await msg.edit_text("❌ Could not connect to Garmin. Check your email and password.")
        return

    await db.save_garmin_token(update.effective_user.id, email, token_path)

    # Immediately sync today
    data = await fetch_garmin_day(token_path)
    if data:
        await db.upsert_activity(
            update.effective_user.id,
            data["date"],
            data["total_calories"],
            data["active_calories"],
            data["bmr_calories"],
            data["steps"],
        )

    await msg.edit_text(
        "✅ Garmin connected! Today's activity synced.\n"
        "Data will refresh every hour automatically."
    )


# ---------------------------------------------------------------------------
# /setgoals — ConversationHandler
# ---------------------------------------------------------------------------

async def cmd_setgoals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ensure_user(update)
    keyboard = [
        [InlineKeyboardButton(label, callback_data=key)]
        for key, label in GOAL_LABELS.items()
    ]
    await update.message.reply_text(
        "🎯 *Set your goals* — Step 1 of 3\n\nWhat's your main goal?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return _ASK_GOAL_TYPE


async def _setgoals_goal_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["goal_type"] = query.data
    keyboard = [
        [InlineKeyboardButton(label, callback_data=key)]
        for key, label in ACTIVITY_LABELS.items()
    ]
    await query.edit_message_text(
        f"✅ Goal: *{GOAL_LABELS[query.data]}*\n\n"
        "🎯 *Step 2 of 3* — What's your daily activity level?\n\n"
        "• Sedentary — desk job, little exercise\n"
        "• Light — 1-3 workouts/week\n"
        "• Moderate — 3-5 workouts/week\n"
        "• High — intense daily training",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return _ASK_ACTIVITY


async def _setgoals_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["activity_level"] = query.data
    await query.edit_message_text(
        f"✅ Activity: *{ACTIVITY_LABELS[query.data]}*\n\n"
        "🎯 *Step 3 of 3* — What's your body weight in kg?\n\n"
        "Reply with a number, e.g. `82`",
        parse_mode="Markdown",
    )
    return _ASK_WEIGHT


async def _setgoals_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")
    try:
        weight_kg = float(text)
        if not (30 <= weight_kg <= 300):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid weight in kg (e.g. `82`).", parse_mode="Markdown")
        return _ASK_WEIGHT

    context.user_data["weight_kg"] = weight_kg
    await update.message.reply_text(
        f"✅ Weight: *{weight_kg:.1f} kg*\n\n"
        "Any extra goals or notes for the AI coach?\n"
        "_(e.g. 'run 3x per week, avoid sugar') — or /skip_",
        parse_mode="Markdown",
    )
    return _ASK_TEXT_GOALS


async def _setgoals_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    goals_text = update.message.text.strip() if update.message.text != "/skip" else None
    return await _finish_setgoals(update, context, goals_text)


async def _setgoals_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _finish_setgoals(update, context, None)


async def _finish_setgoals(update: Update, context: ContextTypes.DEFAULT_TYPE, goals_text: str | None) -> int:
    ud = context.user_data
    await db.save_user_profile(
        telegram_id=update.effective_user.id,
        weight_kg=ud.get("weight_kg"),
        goal_type=ud.get("goal_type"),
        activity_level=ud.get("activity_level"),
        goals=goals_text,
    )
    goal_label = GOAL_LABELS.get(ud.get("goal_type", ""), ud.get("goal_type", ""))
    activity_label = ACTIVITY_LABELS.get(ud.get("activity_level", ""), ud.get("activity_level", ""))
    summary = (
        f"✅ *Profile saved!*\n\n"
        f"🎯 Goal: {goal_label}\n"
        f"🏃 Activity: {activity_label}\n"
        f"⚖️ Weight: {ud.get('weight_kg', '?'):.1f} kg\n"
    )
    if goals_text:
        summary += f"📝 Notes: {goals_text}\n"
    summary += "\nYour /summary will now include personalised macro targets."
    await update.message.reply_text(summary, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def _setgoals_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.", parse_mode="Markdown")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /addworkout — ConversationHandler
# ---------------------------------------------------------------------------

async def cmd_addworkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ensure_user(update)
    await update.message.reply_text(
        "🏋️ *Log a workout*\n\nHow many calories did you burn? (e.g. `350`)",
        parse_mode="Markdown",
    )
    return _ASK_WORKOUT_CALORIES


async def _addworkout_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        calories = int(float(text.replace(",", ".")))
        if calories <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a positive number, e.g. `350`.", parse_mode="Markdown")
        return _ASK_WORKOUT_CALORIES

    await db.add_manual_workout(update.effective_user.id, calories)
    await update.message.reply_text(
        f"✅ Workout logged: *{calories} kcal* burned today.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def _addworkout_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ensure_user(update)
    telegram_id = update.effective_user.id
    user = await db.get_user(telegram_id)
    activity = await db.get_daily_activity(telegram_id)
    food = await db.get_daily_food_summary(telegram_id)

    if user and user.get("garmin_email"):
        garmin_line = f"🟢 Garmin connected ({user['garmin_email']})"
    else:
        garmin_line = "🔴 Garmin not connected — use /garmin email password"

    # Food section
    if food.entry_count > 0:
        p_pct, f_pct, c_pct = _macro_pcts(food.total_protein_g, food.total_fat_g, food.total_carbs_g)
        food_section = (
            f"🍽 *Today's Intake* — {food.entry_count} meal{'s' if food.entry_count != 1 else ''}\n"
            f"  🔥 Calories: *{food.total_calories} kcal*\n"
            f"  💪 Protein: {food.total_protein_g:.1f}g ({p_pct:.0f}%)\n"
            f"  🧈 Fat:     {food.total_fat_g:.1f}g ({f_pct:.0f}%)\n"
            f"  🍞 Carbs:   {food.total_carbs_g:.1f}g ({c_pct:.0f}%)"
        )
    else:
        food_section = "🍽 *Today's Intake*\n  No meals logged yet — send a food photo to start"

    # Activity section
    if activity:
        activity_section = (
            f"🏃 *Activity*\n"
            f"  🔥 Burned: {activity.total_calories} kcal\n"
            f"  👣 Steps:  {activity.steps:,}"
        )
    else:
        activity_section = "🏃 *Activity*\n  No data for today yet"

    # Balance
    if food.entry_count > 0 and activity:
        balance = food.total_calories - activity.total_calories
        sign = "+" if balance >= 0 else ""
        emoji = "📈" if balance >= 0 else "📉"
        balance_line = f"{emoji} *Balance: {sign}{balance} kcal* ({'surplus' if balance >= 0 else 'deficit'})"
    elif food.entry_count > 0:
        balance_line = "📊 *Balance*\n  Connect Garmin to see calorie deficit/surplus"
    else:
        balance_line = ""

    parts = [garmin_line, "", food_section, "", activity_section]
    if balance_line:
        parts += ["", balance_line]
    parts.append("\n_Type *calc* for full AI analysis & recommendations_")

    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Photo handler — food analysis
# ---------------------------------------------------------------------------

async def handle_food_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ensure_user(update)

    # Download the highest-res photo
    photo = update.message.photo[-1]
    file = await photo.get_file()
    buf = BytesIO()
    await file.download_to_memory(buf)
    image_bytes = buf.getvalue()

    comment = update.message.caption or ""
    msg = await update.message.reply_text("🔍 Analyzing your food…")

    try:
        analysis = await analyze_food_photo(image_bytes, comment)
    except Exception as exc:
        logger.exception("Food analysis failed")
        await msg.edit_text(f"❌ Analysis failed: {exc}")
        return

    await db.log_food(
        telegram_id=update.effective_user.id,
        calories=analysis.calories,
        protein_g=analysis.protein_g,
        fat_g=analysis.fat_g,
        carbs_g=analysis.carbs_g,
        description=analysis.food_description,
        image_description=analysis.image_description,
    )

    total_kcal = analysis.calories or 1
    p_pct = analysis.protein_g * 4 / total_kcal * 100
    f_pct = analysis.fat_g * 9 / total_kcal * 100
    c_pct = analysis.carbs_g * 4 / total_kcal * 100

    await msg.edit_text(
        f"🍽 *{analysis.food_description}*\n\n"
        f"🔥 Calories: *{analysis.calories} kcal*\n\n"
        f"💪 Protein: {analysis.protein_g:.1f}g ({p_pct:.0f}%)\n"
        f"🧈 Fat:     {analysis.fat_g:.1f}g ({f_pct:.0f}%)\n"
        f"🍞 Carbs:   {analysis.carbs_g:.1f}g ({c_pct:.0f}%)\n\n"
        f"Confidence: {analysis.confidence}\n"
        "✅ Logged!",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /summary (and "calc" message) — daily summary + AI recommendations
# ---------------------------------------------------------------------------

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_calc(update, context)


async def handle_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ensure_user(update)
    telegram_id = update.effective_user.id
    first_name = update.effective_user.first_name

    msg = await update.message.reply_text("📊 Calculating your daily stats…")

    food = await db.get_daily_food_summary(telegram_id)
    activity = await db.get_daily_activity(telegram_id)
    user = await db.get_user(telegram_id)
    has_garmin = bool(user and user.get("garmin_email"))
    user_goals = user.get("goals") if user else None

    total_burned = activity.total_calories if activity else 0
    active_burned = activity.active_calories if activity else 0
    bmr = activity.bmr_calories if activity else 0
    steps = activity.steps if activity else 0

    # Build stats block
    balance = food.total_calories - total_burned
    balance_sign = "+" if balance >= 0 else ""
    balance_emoji = "📈" if balance >= 0 else "📉"

    p_pct, f_pct, c_pct = _macro_pcts(food.total_protein_g, food.total_fat_g, food.total_carbs_g)
    p_kcal = food.total_protein_g * 4
    f_kcal = food.total_fat_g * 9
    c_kcal = food.total_carbs_g * 4
    total_macro_kcal = p_kcal + f_kcal + c_kcal or 1

    stats_text = (
        f"📅 *Daily Summary*\n"
        f"{'─' * 28}\n\n"
        f"🍽 *INTAKE* ({food.entry_count} meals)\n"
        f"  Calories: *{food.total_calories} kcal*\n"
        f"  💪 Protein: {food.total_protein_g:.1f}g  {_macro_bar(p_kcal, total_macro_kcal)} {p_pct:.0f}%\n"
        f"  🧈 Fat:     {food.total_fat_g:.1f}g  {_macro_bar(f_kcal, total_macro_kcal)} {f_pct:.0f}%\n"
        f"  🍞 Carbs:   {food.total_carbs_g:.1f}g  {_macro_bar(c_kcal, total_macro_kcal)} {c_pct:.0f}%\n\n"
    )

    if activity:
        stats_text += (
            f"🏃 *EXPENDITURE*\n"
            f"  Total burned: *{total_burned} kcal*\n"
            f"  Passive (BMR): {bmr} kcal\n"
            f"  Workouts:      {active_burned} kcal\n"
            f"  Steps: {steps:,}\n\n"
        )
    else:
        stats_text += (
            "🏃 *EXPENDITURE*\n"
            "  No Garmin data — use /garmin to connect\n\n"
        )

    stats_text += f"{balance_emoji} *Balance: {balance_sign}{balance} kcal*\n\n"

    # Target macros block (requires profile; uses estimated TDEE if Garmin hasn't synced)
    weight_kg = user.get("weight_kg") if user else None
    goal_type = user.get("goal_type") if user else None
    if weight_kg and goal_type:
        t_protein, t_fat, t_carbs, t_kcal = _target_macros(
            weight_kg, total_burned, goal_type, user.get("activity_level") if user else None
        )
        goal_label = GOAL_LABELS.get(goal_type, goal_type)

        def _pct(actual: float, target: float) -> str:
            return f"{actual / target * 100:.0f}%" if target else "—"

        stats_text += (
            f"🎯 *TARGETS* ({goal_label})\n"
            f"  Calories: {food.total_calories} / {t_kcal} kcal\n"
            f"  {_macro_bar(food.total_calories, t_kcal)}\n\n"
            f"  💪 Protein: {food.total_protein_g:.0f} / {t_protein:.0f}g\n"
            f"  {_macro_bar(food.total_protein_g, t_protein)}\n\n"
            f"  🧈 Fat:     {food.total_fat_g:.0f} / {t_fat:.0f}g\n"
            f"  {_macro_bar(food.total_fat_g, t_fat)}\n\n"
            f"  🍞 Carbs:   {food.total_carbs_g:.0f} / {t_carbs:.0f}g\n"
            f"  {_macro_bar(food.total_carbs_g, t_carbs)}\n\n"
        )
    elif not weight_kg or not goal_type:
        stats_text += "_Set your profile with /setgoals to see macro targets_\n\n"

    stats_text += "⏳ Generating recommendations…"

    await msg.edit_text(stats_text, parse_mode="Markdown")

    try:
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
            user_goals=user_goals,
            weight_kg=weight_kg,
            goal_type=goal_type,
            activity_level=user.get("activity_level") if user else None,
        )

        eod = rec.projected_eod_balance_kcal
        eod_sign = "+" if eod >= 0 else ""
        eod_label = "surplus" if eod > 0 else ("deficit" if eod < 0 else "balanced")
        rec_text = (
            f"\n💡 *AI Coach — {rec.status}*\n"
            f"{'─' * 28}\n\n"
            f"📊 *Projected by midnight:* {eod_sign}{eod} kcal ({eod_label})\n\n"
            f"{rec.analysis}\n\n"
            "*Recommendations:*\n"
            + "\n".join(f"• {r}" for r in rec.recommendations)
        )

        await msg.edit_text(stats_text.replace("⏳ Generating recommendations…", rec_text), parse_mode="Markdown")

    except Exception as exc:
        logger.exception("Recommendation generation failed")
        await msg.edit_text(
            stats_text.replace("⏳ Generating recommendations…", f"⚠️ Could not generate recommendations: {exc}"),
            parse_mode="Markdown",
        )


# ---------------------------------------------------------------------------
# /workout — pre/post workout nutrition advice
# ---------------------------------------------------------------------------

async def cmd_workout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ensure_user(update)
    await update.message.reply_text(
        "🏋️ *Workout planner*\n\nWhat workout are you going to do?\n"
        "_(e.g. 'strength training', 'running 5km', 'HIIT', 'cycling')_",
        parse_mode="Markdown",
    )
    return _ASK_WORKOUT_TYPE


async def _workout_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["workout_type"] = update.message.text.strip()
    await update.message.reply_text(
        f"Got it — *{context.user_data['workout_type']}* 💪\n\n"
        "In how many *minutes* does it start?",
        parse_mode="Markdown",
    )
    return _ASK_WORKOUT_MINUTES


async def _workout_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        minutes = int(float(text))
        if minutes < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a number of minutes, e.g. `30`.", parse_mode="Markdown")
        return _ASK_WORKOUT_MINUTES

    telegram_id = update.effective_user.id
    workout_type = context.user_data.pop("workout_type", "workout")
    context.user_data.clear()

    msg = await update.message.reply_text("🔍 Analysing your food and preparing suggestions…")

    food = await db.get_daily_food_summary(telegram_id)
    entries = await db.get_daily_food_entries(telegram_id)
    user = await db.get_user(telegram_id)

    descriptions = [e.description for e in entries if e.description]

    try:
        suggestion = await generate_workout_nutrition(
            workout_type=workout_type,
            minutes_until_workout=minutes,
            food_descriptions=descriptions,
            calories_in=food.total_calories,
            protein_g=food.total_protein_g,
            fat_g=food.total_fat_g,
            carbs_g=food.total_carbs_g,
            goal_type=user.get("goal_type") if user else None,
            weight_kg=user.get("weight_kg") if user else None,
        )
    except Exception as exc:
        logger.exception("Workout nutrition generation failed")
        await msg.edit_text(f"❌ Failed to generate suggestions: {exc}")
        return ConversationHandler.END

    parts = [
        f"🏋️ *{workout_type}* — starts in {minutes} min\n",
        "🍌 *Pre-workout:*",
        suggestion.pre_workout,
        "\n🥩 *Post-workout:*",
        suggestion.post_workout,
    ]
    if suggestion.heads_up:
        parts.append(f"\n⚠️ {suggestion.heads_up}")

    await msg.edit_text("\n".join(parts), parse_mode="Markdown")
    return ConversationHandler.END


async def _workout_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /analyze — food quality analysis for the day
# ---------------------------------------------------------------------------

async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ensure_user(update)
    telegram_id = update.effective_user.id

    food = await db.get_daily_food_summary(telegram_id)
    if food.entry_count == 0:
        await update.message.reply_text("No meals logged today yet — send a food photo first! 📸")
        return

    entries = await db.get_daily_food_entries(telegram_id)
    activity = await db.get_daily_activity(telegram_id)
    user = await db.get_user(telegram_id)

    total_burned = activity.total_calories if activity else 0
    weight_kg = user.get("weight_kg") if user else None
    goal_type = user.get("goal_type") if user else None

    target_kcal = 2000
    if weight_kg and goal_type:
        _, _, _, target_kcal = _target_macros(
            weight_kg, total_burned, goal_type, user.get("activity_level") if user else None
        )

    now = datetime.now()
    sleep_hour = 23
    hours_until_sleep = max(0, sleep_hour - now.hour)
    calories_remaining = max(0, target_kcal - food.total_calories)

    descriptions = [e.description for e in entries if e.description]

    msg = await update.message.reply_text("🔍 Analysing your day…")

    try:
        analysis = await analyze_day_food(
            food_descriptions=descriptions,
            calories_in=food.total_calories,
            protein_g=food.total_protein_g,
            fat_g=food.total_fat_g,
            carbs_g=food.total_carbs_g,
            total_burned=total_burned,
            hours_until_sleep=hours_until_sleep,
            calories_remaining=calories_remaining,
            user_goals=user.get("goals") if user else None,
            goal_type=goal_type,
            weight_kg=weight_kg,
        )
    except Exception as exc:
        logger.exception("Day analysis failed")
        await msg.edit_text(f"❌ Analysis failed: {exc}")
        return

    parts = ["🥗 *Today's Food Analysis*\n"]
    parts.append("✅ *What went well:*")
    parts.extend(f"  • {p}" for p in analysis.positives)
    parts.append("\n⚠️ *Watch out for:*")
    parts.extend(f"  • {n}" for n in analysis.negatives)
    if analysis.next_meal_suggestion:
        parts.append(f"\n🍽 *Next meal suggestion:*\n  {analysis.next_meal_suggestion}")
    else:
        parts.append("\n🌙 Eating window is closing — good job, rest well!")

    await msg.edit_text("\n".join(parts), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /editfood — edit or delete a today's food entry
# ---------------------------------------------------------------------------

def _entry_short(entry) -> str:
    desc = (entry.description or "unnamed")[:30]
    return f"{desc} — {entry.calories} kcal"


def _entry_detail(entry) -> str:
    return (
        f"*{entry.description or 'unnamed'}*\n"
        f"  🔥 {entry.calories} kcal\n"
        f"  💪 Protein: {entry.protein_g:.1f}g\n"
        f"  🧈 Fat: {entry.fat_g:.1f}g\n"
        f"  🍞 Carbs: {entry.carbs_g:.1f}g"
    )


async def cmd_editfood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ensure_user(update)
    telegram_id = update.effective_user.id
    entries = await db.get_daily_food_entries(telegram_id)

    if not entries:
        await update.message.reply_text("No meals logged today — nothing to edit! 📸")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(f"{i+1}. {_entry_short(e)}", callback_data=f"ef_entry:{e.id}")]
        for i, e in enumerate(entries)
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ef_cancel")])

    await update.message.reply_text(
        "✏️ *Edit food* — Step 1 of 3\n\nWhich meal do you want to edit or delete?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return _EF_SELECT_ENTRY


async def _ef_select_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "ef_cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    entry_id = int(query.data.split(":")[1])
    context.user_data["ef_entry_id"] = entry_id

    entry = await db.get_food_entry(entry_id, update.effective_user.id)
    if not entry:
        await query.edit_message_text("❌ Entry not found.")
        return ConversationHandler.END

    context.user_data["ef_entry"] = entry

    keyboard = [
        [InlineKeyboardButton("✏️ Edit", callback_data="ef_action:edit"),
         InlineKeyboardButton("🗑 Delete", callback_data="ef_action:delete")],
        [InlineKeyboardButton("❌ Cancel", callback_data="ef_cancel")],
    ]
    await query.edit_message_text(
        f"Selected:\n{_entry_detail(entry)}\n\nWhat do you want to do?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return _EF_SELECT_ACTION


async def _ef_select_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "ef_cancel":
        await query.edit_message_text("Cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    action = query.data.split(":")[1]
    entry_id = context.user_data["ef_entry_id"]
    telegram_id = update.effective_user.id

    if action == "delete":
        await db.delete_food_entry(entry_id, telegram_id)
        await query.edit_message_text("🗑 Meal deleted.")
        context.user_data.clear()
        return ConversationHandler.END

    # edit — show field selector
    return await _show_field_selector(query, context)


async def _show_field_selector(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    entry = context.user_data.get("ef_entry")
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"ef_field:{field}")]
        for field, label in _EF_FIELD_LABELS.items()
    ]
    keyboard.append([InlineKeyboardButton("✅ Done", callback_data="ef_done")])

    await query.edit_message_text(
        f"✏️ *Editing meal*\n\n{_entry_detail(entry)}\n\nWhich field do you want to change?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return _EF_SELECT_FIELD


async def _ef_select_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "ef_done":
        await query.edit_message_text("✅ Done editing.")
        context.user_data.clear()
        return ConversationHandler.END

    field = query.data.split(":")[1]
    context.user_data["ef_field"] = field
    label = _EF_FIELD_LABELS[field]

    if field == "description":
        prompt = f"Enter a new description:"
    else:
        entry = context.user_data["ef_entry"]
        current = getattr(entry, field)
        prompt = f"Enter new value for *{label}* (current: `{current}`):"

    await query.edit_message_text(prompt, parse_mode="Markdown")
    return _EF_ENTER_VALUE


async def _ef_enter_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    field = context.user_data["ef_field"]
    entry_id = context.user_data["ef_entry_id"]
    telegram_id = update.effective_user.id

    # Parse and validate
    kwargs: dict = {}
    try:
        if field == "description":
            kwargs["description"] = text
        elif field == "calories":
            val = int(float(text.replace(",", ".")))
            if val < 0:
                raise ValueError
            kwargs["calories"] = val
        else:
            val = float(text.replace(",", "."))
            if val < 0:
                raise ValueError
            kwargs[field] = val
    except ValueError:
        label = _EF_FIELD_LABELS[field]
        await update.message.reply_text(
            f"Invalid value. Please enter a valid number for {label}:",
            parse_mode="Markdown",
        )
        return _EF_ENTER_VALUE

    await db.update_food_entry(entry_id, telegram_id, **kwargs)

    # Refresh cached entry
    entry = await db.get_food_entry(entry_id, telegram_id)
    context.user_data["ef_entry"] = entry

    # Ask what to edit next
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"ef_field:{f}")]
        for f, label in _EF_FIELD_LABELS.items()
    ]
    keyboard.append([InlineKeyboardButton("✅ Done", callback_data="ef_done")])

    await update.message.reply_text(
        f"✅ Updated!\n\n{_entry_detail(entry)}\n\nAnything else to change?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return _EF_SELECT_FIELD


async def _ef_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _add_handlers(app: Application) -> None:
    setgoals_conv = ConversationHandler(
        entry_points=[CommandHandler("setgoals", cmd_setgoals)],
        states={
            _ASK_GOAL_TYPE: [CallbackQueryHandler(_setgoals_goal_type)],
            _ASK_ACTIVITY:  [CallbackQueryHandler(_setgoals_activity)],
            _ASK_WEIGHT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, _setgoals_weight)],
            _ASK_TEXT_GOALS: [
                CommandHandler("skip", _setgoals_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _setgoals_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", _setgoals_cancel)],
    )

    addworkout_conv = ConversationHandler(
        entry_points=[CommandHandler("addworkout", cmd_addworkout)],
        states={
            _ASK_WORKOUT_CALORIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, _addworkout_calories)],
        },
        fallbacks=[CommandHandler("cancel", _addworkout_cancel)],
    )

    workout_conv = ConversationHandler(
        entry_points=[CommandHandler("workout", cmd_workout)],
        states={
            _ASK_WORKOUT_TYPE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, _workout_type)],
            _ASK_WORKOUT_MINUTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, _workout_minutes)],
        },
        fallbacks=[CommandHandler("cancel", _workout_cancel)],
    )

    editfood_conv = ConversationHandler(
        entry_points=[CommandHandler("editfood", cmd_editfood)],
        states={
            _EF_SELECT_ENTRY:  [CallbackQueryHandler(_ef_select_entry)],
            _EF_SELECT_ACTION: [CallbackQueryHandler(_ef_select_action)],
            _EF_SELECT_FIELD:  [CallbackQueryHandler(_ef_select_field)],
            _EF_ENTER_VALUE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _ef_enter_value)],
        },
        fallbacks=[CommandHandler("cancel", _ef_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("garmin", cmd_garmin))
    app.add_handler(setgoals_conv)
    app.add_handler(addworkout_conv)
    app.add_handler(workout_conv)
    app.add_handler(editfood_conv)
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(MessageHandler(filters.PHOTO, handle_food_photo))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^calc$"), handle_calc)
    )


def create_application() -> Application:
    app = Application.builder().token(settings.telegram_token).build()
    _add_handlers(app)
    return app


def create_application_with_post_init(post_init) -> Application:
    app = Application.builder().token(settings.telegram_token).post_init(post_init).build()
    _add_handlers(app)
    return app
