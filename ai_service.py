import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent, BinaryContent

logger = logging.getLogger(__name__)

_TOKEN_LOG = Path(__file__).parent / "token_usage.log"


def _log_tokens(used: int) -> None:
    try:
        current = int(_TOKEN_LOG.read_text().strip()) if _TOKEN_LOG.exists() else 0
        _TOKEN_LOG.write_text(str(current + used))
    except Exception:
        logger.warning("Failed to update token_usage.log")


class FoodAnalysis(BaseModel):
    calories: int = Field(description="Estimated total calories in kcal")
    protein_g: float = Field(description="Estimated protein in grams")
    fat_g: float = Field(description="Estimated fat in grams")
    carbs_g: float = Field(description="Estimated carbohydrates in grams")
    food_description: str = Field(description="Brief description of what was identified")
    image_description: str = Field(
        description="Detailed description of the food visible in the image (up to 200 words): "
                    "describe each dish, ingredients, cooking method, portion sizes, and presentation."
    )
    confidence: str = Field(description="low | medium | high — how confident the estimate is")


class DailyRecommendation(BaseModel):
    status: str = Field(description="One-line status: surplus / deficit / balanced")
    projected_eod_balance_kcal: int = Field(
        description="Projected calorie balance at end of day (midnight), accounting for remaining passive burn. Negative = deficit, positive = surplus."
    )
    analysis: str = Field(description="2-3 sentence analysis of the day so far, referencing the projected end-of-day balance")
    recommendations: list[str] = Field(description="3-4 actionable recommendations for the rest of the day")


class DayFoodAnalysis(BaseModel):
    positives: list[str] = Field(description="2-3 things the user did well today food-wise")
    negatives: list[str] = Field(description="1-2 things to improve or watch out for")
    next_meal_suggestion: str | None = Field(
        description="If there is still a reasonable meal window before sleep (2+ hours), suggest what to eat next. "
                    "Otherwise set to null. Be specific: food name, rough portion, macros benefit."
    )


_food_agent: Agent[None, FoodAnalysis] = Agent(
    "openai:gpt-4o",
    output_type=FoodAnalysis,
    system_prompt=(
        "You are a professional nutritionist and dietitian. "
        "When given a food photo and optional user comment, estimate the total calories "
        "and macronutrients (protein, fat, carbohydrates) as accurately as possible. "
        "Account for portion size visible in the image. Be realistic — do not underestimate. "
        "If the image quality makes estimation difficult, note it in confidence."
    ),
)

_recommendation_agent: Agent[None, DailyRecommendation] = Agent(
    "openai:gpt-4o",
    output_type=DailyRecommendation,
    system_prompt=(
        "You are a personal nutrition and fitness coach. "
        "Given a user's daily calorie intake and expenditure data, provide a brief analysis "
        "and practical recommendations for the rest of the day. "
        "Be specific, encouraging, and actionable. "
        "Consider both food choices and activity opportunities. "
        "If the user has set personal goals, tailor your entire analysis and recommendations toward achieving those goals."
    ),
)


class WorkoutNutrition(BaseModel):
    pre_workout: str = Field(
        description="Specific pre-workout meal/snack suggestion with food name, portion size, timing relative to workout, and why it suits what's already been eaten today."
    )
    post_workout: str = Field(
        description="Specific post-workout meal/snack suggestion with food name, portion size, and what macros it covers given what was already eaten today."
    )
    heads_up: str | None = Field(
        description="One short sentence warning if today's intake is already low/high in something that matters for this workout type. Null if nothing notable."
    )


_day_analysis_agent: Agent[None, DayFoodAnalysis] = Agent(
    "openai:gpt-4o",
    output_type=DayFoodAnalysis,
    system_prompt=(
        "You are a concise nutrition coach. Analyse the user's food log for the day. "
        "Highlight what they did well (e.g. good protein, fibre, meal timing) and what needs improvement "
        "(e.g. excess sugar, low protein, processed food). "
        "If eating time remains before sleep, suggest one specific next meal that fits their remaining macro budget. "
        "Keep each bullet to one short sentence. Total response must fit in ~200 words."
    ),
)


async def analyze_day_food(
    food_descriptions: list[str],
    calories_in: int,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    total_burned: int,
    hours_until_sleep: int,
    calories_remaining: int,
    user_goals: str | None = None,
    goal_type: str | None = None,
    weight_kg: float | None = None,
) -> DayFoodAnalysis:
    meals_text = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(food_descriptions)) or "  (none logged)"
    profile_lines = []
    if goal_type:
        profile_lines.append(f"Goal: {goal_type.replace('_', ' ')}")
    if weight_kg:
        profile_lines.append(f"Weight: {weight_kg:.0f} kg")
    if user_goals:
        profile_lines.append(f"Notes: {user_goals}")
    profile_section = ("\nUSER PROFILE:\n" + "\n".join(f"  {p}" for p in profile_lines)) if profile_lines else ""

    prompt = (
        f"MEALS TODAY:\n{meals_text}\n"
        f"\nMACROS: {calories_in} kcal | protein {protein_g:.0f}g | fat {fat_g:.0f}g | carbs {carbs_g:.0f}g"
        f"\nCALORIES BURNED: {total_burned} kcal"
        f"\nHOURS UNTIL SLEEP: {hours_until_sleep}"
        f"\nCALORIES REMAINING IN BUDGET: {calories_remaining} kcal"
        f"{profile_section}"
    )

    result = await _day_analysis_agent.run(prompt)
    _log_tokens(result.usage().total_tokens)
    return result.output


_workout_nutrition_agent: Agent[None, WorkoutNutrition] = Agent(
    "openai:gpt-4o",
    output_type=WorkoutNutrition,
    system_prompt=(
        "You are a sports nutritionist. Given what a user has eaten today and the workout they're about to do, "
        "suggest a specific pre-workout snack/meal and a specific post-workout meal. "
        "Be concrete: name the food, give a portion, explain the timing for pre-workout. "
        "Take into account what macros are already covered so recommendations complement the day, not duplicate it. "
        "Keep each suggestion to 2-3 sentences max."
    ),
)


async def generate_workout_nutrition(
    workout_type: str,
    minutes_until_workout: int,
    food_descriptions: list[str],
    calories_in: int,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    goal_type: str | None = None,
    weight_kg: float | None = None,
) -> WorkoutNutrition:
    meals_text = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(food_descriptions)) or "  (none logged yet)"
    profile_lines = []
    if goal_type:
        profile_lines.append(f"Goal: {goal_type.replace('_', ' ')}")
    if weight_kg:
        profile_lines.append(f"Weight: {weight_kg:.0f} kg")
    profile_section = ("\nUSER PROFILE:\n" + "\n".join(f"  {p}" for p in profile_lines) + "\n") if profile_lines else ""

    prompt = (
        f"UPCOMING WORKOUT: {workout_type}\n"
        f"STARTS IN: {minutes_until_workout} minutes\n"
        f"{profile_section}"
        f"\nEATEN TODAY:\n{meals_text}"
        f"\nTODAY'S MACROS SO FAR: {calories_in} kcal | protein {protein_g:.0f}g | fat {fat_g:.0f}g | carbs {carbs_g:.0f}g\n"
        "\nSuggest what to eat before and after the workout based on the above."
    )

    result = await _workout_nutrition_agent.run(prompt)
    _log_tokens(result.usage().total_tokens)
    return result.output


async def analyze_food_photo(image_bytes: bytes, user_comment: str = "") -> FoodAnalysis:
    prompt_parts: list = [BinaryContent(data=image_bytes, media_type="image/jpeg")]

    if user_comment:
        prompt_parts.append(
            f"User's comment about this food: '{user_comment}'. "
            "Use this to refine your estimate (e.g. specific dish name, serving size, ingredients)."
        )
    else:
        prompt_parts.append(
            "Please identify the food items and estimate total calories and macronutrients."
        )

    result = await _food_agent.run(prompt_parts)
    _log_tokens(result.usage().total_tokens)
    return result.output


async def generate_daily_recommendation(
    first_name: str,
    calories_in: int,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    food_entries: int,
    total_burned: int,
    active_burned: int,
    bmr: int,
    steps: int,
    has_garmin: bool,
    user_goals: str | None = None,
    weight_kg: float | None = None,
    goal_type: str | None = None,
    activity_level: str | None = None,
) -> DailyRecommendation:
    now = datetime.now()
    hours_left = max(0, 22 - now.hour)  # assume 10pm is end of eating window
    balance = calories_in - total_burned
    projected_passive_burn = int((bmr / 24) * hours_left)
    projected_balance = balance - projected_passive_burn

    activity_section = ""
    if has_garmin:
        activity_section = (
            f"- Total calories burned: {total_burned} kcal\n"
            f"  - Passive/BMR: {bmr} kcal\n"
            f"  - Active/workouts: {active_burned} kcal\n"
            f"  - Steps: {steps:,}\n"
        )
    else:
        activity_section = "- No activity tracker connected (Garmin not linked)\n"

    profile_parts = []
    if weight_kg:
        profile_parts.append(f"Weight: {weight_kg:.1f} kg")
    if goal_type:
        profile_parts.append(f"Goal: {goal_type.replace('_', ' ')}")
    if activity_level:
        profile_parts.append(f"Activity level: {activity_level}")
    if user_goals:
        profile_parts.append(f"Notes: {user_goals}")
    goals_section = ("USER PROFILE:\n" + "\n".join(f"- {p}" for p in profile_parts) + "\n\n") if profile_parts else ""

    prompt = (
        f"User: {first_name}\n"
        f"Current time: {now.strftime('%H:%M')}, approximately {hours_left} hours left in the eating day\n\n"
        f"{goals_section}"
        f"TODAY'S INTAKE ({food_entries} meals logged):\n"
        f"- Calories consumed: {calories_in} kcal\n"
        f"- Protein: {protein_g:.1f}g\n"
        f"- Fat: {fat_g:.1f}g\n"
        f"- Carbohydrates: {carbs_g:.1f}g\n\n"
        f"TODAY'S EXPENDITURE:\n"
        f"{activity_section}\n"
        f"Current balance: {'+' if balance >= 0 else ''}{balance} kcal "
        f"({'surplus' if balance >= 0 else 'deficit'})\n"
        f"Projected end-of-day balance (after ~{projected_passive_burn} kcal passive burn remaining): "
        f"{'+' if projected_balance >= 0 else ''}{projected_balance} kcal "
        f"({'surplus' if projected_balance >= 0 else 'deficit'})\n\n"
        "Provide analysis and recommendations for the remaining hours of the day. "
        "Use the projected end-of-day balance as the primary signal, not the current snapshot balance."
    )

    result = await _recommendation_agent.run(prompt)
    _log_tokens(result.usage().total_tokens)
    return result.output
