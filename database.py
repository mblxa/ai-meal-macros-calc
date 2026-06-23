import aiosqlite
from datetime import date
from dataclasses import dataclass
from config import settings


@dataclass
class FoodEntry:
    id: int
    telegram_id: int
    date: str
    calories: int
    protein_g: float
    fat_g: float
    carbs_g: float
    description: str


@dataclass
class ActivityEntry:
    date: str
    total_calories: int
    active_calories: int
    bmr_calories: int
    steps: int


@dataclass
class DailyFoodSummary:
    total_calories: int
    total_protein_g: float
    total_fat_g: float
    total_carbs_g: float
    entry_count: int


async def init_db() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id      INTEGER PRIMARY KEY,
                username         TEXT,
                first_name       TEXT,
                garmin_email     TEXT,
                garmin_token_path TEXT,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS food_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                date        DATE NOT NULL,
                calories    INTEGER NOT NULL,
                protein_g   REAL NOT NULL DEFAULT 0,
                fat_g       REAL NOT NULL DEFAULT 0,
                carbs_g     REAL NOT NULL DEFAULT 0,
                description       TEXT,
                image_description TEXT,
                logged_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS activity_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER NOT NULL,
                date            DATE NOT NULL,
                total_calories  INTEGER NOT NULL DEFAULT 0,
                active_calories INTEGER NOT NULL DEFAULT 0,
                bmr_calories    INTEGER NOT NULL DEFAULT 0,
                steps           INTEGER NOT NULL DEFAULT 0,
                synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_id, date),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS manual_workouts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                date        DATE NOT NULL,
                calories    INTEGER NOT NULL,
                logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            );
        """)
        await db.commit()
        # migrate existing databases that still have garmin_password
        try:
            await db.execute("ALTER TABLE users ADD COLUMN garmin_token_path TEXT")
            await db.commit()
        except Exception:
            pass  # column already exists
        try:
            await db.execute("ALTER TABLE users DROP COLUMN garmin_password")
            await db.commit()
        except Exception:
            pass  # column already gone or SQLite version too old
        try:
            await db.execute("ALTER TABLE users ADD COLUMN goals TEXT")
            await db.commit()
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN weight_kg REAL")
            await db.commit()
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN goal_type TEXT")
            await db.commit()
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN activity_level TEXT")
            await db.commit()
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE food_logs ADD COLUMN image_description TEXT")
            await db.commit()
        except Exception:
            pass


async def upsert_user(telegram_id: int, username: str | None, first_name: str | None) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO users (telegram_id, username, first_name)
               VALUES (?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   username = excluded.username,
                   first_name = excluded.first_name""",
            (telegram_id, username, first_name),
        )
        await db.commit()


async def get_user(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_user_profile(
    telegram_id: int,
    weight_kg: float | None = None,
    goal_type: str | None = None,
    activity_level: str | None = None,
    goals: str | None = None,
) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """UPDATE users SET weight_kg = ?, goal_type = ?, activity_level = ?, goals = ?
               WHERE telegram_id = ?""",
            (weight_kg, goal_type, activity_level, goals, telegram_id),
        )
        await db.commit()


async def save_garmin_token(telegram_id: int, email: str, token_path: str) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """UPDATE users SET garmin_email = ?, garmin_token_path = ?
               WHERE telegram_id = ?""",
            (email, token_path, telegram_id),
        )
        await db.commit()


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_garmin_users() -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE garmin_token_path IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def log_food(
    telegram_id: int,
    calories: int,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    description: str,
    image_description: str = "",
    log_date: date | None = None,
) -> None:
    entry_date = (log_date or date.today()).isoformat()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO food_logs (telegram_id, date, calories, protein_g, fat_g, carbs_g, description, image_description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (telegram_id, entry_date, calories, protein_g, fat_g, carbs_g, description, image_description),
        )
        await db.commit()


async def get_daily_food_summary(telegram_id: int, day: date | None = None) -> DailyFoodSummary:
    entry_date = (day or date.today()).isoformat()
    async with aiosqlite.connect(settings.database_path) as db:
        async with db.execute(
            """SELECT
                   COALESCE(SUM(calories), 0)  AS total_calories,
                   COALESCE(SUM(protein_g), 0) AS total_protein_g,
                   COALESCE(SUM(fat_g), 0)     AS total_fat_g,
                   COALESCE(SUM(carbs_g), 0)   AS total_carbs_g,
                   COUNT(*)                     AS entry_count
               FROM food_logs
               WHERE telegram_id = ? AND date = ?""",
            (telegram_id, entry_date),
        ) as cursor:
            row = await cursor.fetchone()
            return DailyFoodSummary(
                total_calories=row[0],
                total_protein_g=row[1],
                total_fat_g=row[2],
                total_carbs_g=row[3],
                entry_count=row[4],
            )


async def upsert_activity(
    telegram_id: int,
    activity_date: date,
    total_calories: int,
    active_calories: int,
    bmr_calories: int,
    steps: int,
) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO activity_logs (telegram_id, date, total_calories, active_calories, bmr_calories, steps)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id, date) DO UPDATE SET
                   total_calories  = excluded.total_calories,
                   active_calories = excluded.active_calories,
                   bmr_calories    = excluded.bmr_calories,
                   steps           = excluded.steps,
                   synced_at       = CURRENT_TIMESTAMP""",
            (telegram_id, activity_date.isoformat(), total_calories, active_calories, bmr_calories, steps),
        )
        await db.commit()


async def add_manual_workout(telegram_id: int, active_calories: int, activity_date: date | None = None) -> None:
    entry_date = (activity_date or date.today()).isoformat()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO manual_workouts (telegram_id, date, calories) VALUES (?, ?, ?)",
            (telegram_id, entry_date, active_calories),
        )
        await db.commit()


async def get_daily_food_entries(telegram_id: int, day: date | None = None) -> list[FoodEntry]:
    entry_date = (day or date.today()).isoformat()
    async with aiosqlite.connect(settings.database_path) as db:
        async with db.execute(
            """SELECT id, telegram_id, date, calories, protein_g, fat_g, carbs_g, description
               FROM food_logs WHERE telegram_id = ? AND date = ? ORDER BY logged_at""",
            (telegram_id, entry_date),
        ) as cursor:
            rows = await cursor.fetchall()
            return [FoodEntry(*row) for row in rows]


async def delete_food_entry(entry_id: int, telegram_id: int) -> bool:
    async with aiosqlite.connect(settings.database_path) as db:
        cursor = await db.execute(
            "DELETE FROM food_logs WHERE id = ? AND telegram_id = ?",
            (entry_id, telegram_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_food_entry(
    entry_id: int,
    telegram_id: int,
    calories: int | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    carbs_g: float | None = None,
    description: str | None = None,
) -> bool:
    fields, values = [], []
    if calories is not None:
        fields.append("calories = ?"); values.append(calories)
    if protein_g is not None:
        fields.append("protein_g = ?"); values.append(protein_g)
    if fat_g is not None:
        fields.append("fat_g = ?"); values.append(fat_g)
    if carbs_g is not None:
        fields.append("carbs_g = ?"); values.append(carbs_g)
    if description is not None:
        fields.append("description = ?"); values.append(description)
    if not fields:
        return False
    values.extend([entry_id, telegram_id])
    async with aiosqlite.connect(settings.database_path) as db:
        cursor = await db.execute(
            f"UPDATE food_logs SET {', '.join(fields)} WHERE id = ? AND telegram_id = ?",
            values,
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_food_entry(entry_id: int, telegram_id: int) -> FoodEntry | None:
    async with aiosqlite.connect(settings.database_path) as db:
        async with db.execute(
            "SELECT id, telegram_id, date, calories, protein_g, fat_g, carbs_g, description "
            "FROM food_logs WHERE id = ? AND telegram_id = ?",
            (entry_id, telegram_id),
        ) as cursor:
            row = await cursor.fetchone()
            return FoodEntry(*row) if row else None


async def get_daily_activity(telegram_id: int, day: date | None = None) -> ActivityEntry | None:
    entry_date = (day or date.today()).isoformat()
    async with aiosqlite.connect(settings.database_path) as db:
        async with db.execute(
            "SELECT * FROM activity_logs WHERE telegram_id = ? AND date = ?",
            (telegram_id, entry_date),
        ) as cursor:
            garmin_row = await cursor.fetchone()

        async with db.execute(
            "SELECT COALESCE(SUM(calories), 0) FROM manual_workouts WHERE telegram_id = ? AND date = ?",
            (telegram_id, entry_date),
        ) as cursor:
            manual_row = await cursor.fetchone()

    manual_calories = manual_row[0] if manual_row else 0

    if not garmin_row and manual_calories == 0:
        return None

    garmin_total = garmin_row[3] if garmin_row else 0
    garmin_active = garmin_row[4] if garmin_row else 0
    garmin_bmr = garmin_row[5] if garmin_row else 0
    steps = garmin_row[6] if garmin_row else 0

    return ActivityEntry(
        date=entry_date,
        total_calories=garmin_total + manual_calories,
        active_calories=garmin_active + manual_calories,
        bmr_calories=garmin_bmr,
        steps=steps,
    )
