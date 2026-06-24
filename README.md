# Calories Track Bot

A Telegram bot that tracks daily calorie intake and activity. Send a food photo → get instant calorie and macro estimates. Optionally connect Garmin to pull burned calories automatically. An AI coach sends a nightly digest and answers on-demand.

## Stack

- **Python** with `python-telegram-bot` for the bot
- **Pydantic AI** + **OpenAI** for food photo analysis and recommendations
- **Garmin Connect** API for activity data (steps, calories burned)
- **SQLite** (`aiosqlite`) for local storage

## Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | yes | Bot token from [@BotFather](https://t.me/BotFather) |
| `OPENAI_API_KEY` | yes | OpenAI API key |
| `DATABASE_PATH` | no | Path to SQLite file (default: `calories.db`) |
| `GARMIN_TOKEN_DIR` | no | Directory for Garmin OAuth tokens (default: `~/.garminconnect`) |

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
python main.py
```

## Bot commands

| Command | Description |
|---|---|
| `/start` | Welcome message and feature overview |
| `/status` | Today's intake, activity, and calorie balance |
| `/summary` | Full AI analysis with macro breakdown and recommendations |
| `/analyze` | Quick food quality review — what went well, what to watch |
| `/workout` | Pre/post workout nutrition advice based on today's meals |
| `/addworkout` | Manually log calories burned in a workout |
| `/setgoals` | Set goal (fat loss / muscle / maintain), activity level, and weight |
| `/editfood` | Edit or delete a logged meal from today |
| `/garmin <email> <password>` | Connect Garmin account for automatic activity sync |

You can also type `calc` as a plain message — same as `/summary`.

## Automated jobs

- **Garmin sync** — runs every hour to pull today's activity for all connected users
- **Evening digest** — sent at 20:00 UTC daily with a summary and AI tips
