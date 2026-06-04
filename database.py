import json
import os
import uuid
from datetime import datetime
import pymysql
import pymysql.cursors

_SENTIMENT_DIMS = [
    'work_life_balance', 'job_satisfaction', 'stress_anxiety', 'self_confidence',
    'empathy', 'frustration_disengagement', 'growth_mindset', 'psychological_safety',
]

_SENTIMENT_SEEDS = [
    ('work_life_balance',         'Boundary-setting and balance language',                   'Positive'),
    ('job_satisfaction',          'Fulfilment and motivation signals',                       'Positive'),
    ('stress_anxiety',            'Urgency, overwhelm, and pressure signals',                'Negative'),
    ('self_confidence',           'Assertive vs self-doubting phrases',                      'Positive'),
    ('empathy',                   "References to others' feelings and perspectives",         'Positive'),
    ('frustration_disengagement', 'Cynical, dismissive, or disengaged patterns',             'Negative'),
    ('growth_mindset',            'Effort, learning, and challenge framing',                 'Positive'),
    ('psychological_safety',      'Willingness to share failures and fears openly',          'Positive'),
]


def get_connection():
    return pymysql.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', '3306')),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', ''),
        database=os.environ.get('DB_NAME', 'ai_coach'),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
    )


def init_db():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_coach_sessions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    session_id VARCHAR(36) UNIQUE NOT NULL,
                    user_id VARCHAR(36) NOT NULL,
                    user_name VARCHAR(255) DEFAULT NULL,
                    email VARCHAR(255) DEFAULT NULL,
                    org_name VARCHAR(255) DEFAULT NULL,
                    org_slug VARCHAR(255) DEFAULT NULL,
                    cohort_id VARCHAR(36) DEFAULT NULL,
                    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_id (user_id),
                    INDEX idx_org_slug (org_slug)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # Add columns for existing tables that predate this schema
            for col, definition in [
                ('user_name', 'VARCHAR(255) DEFAULT NULL'),
                ('email', 'VARCHAR(255) DEFAULT NULL'),
                ('org_name', 'VARCHAR(255) DEFAULT NULL'),
                ('org_slug', 'VARCHAR(255) DEFAULT NULL'),
                ('cohort_id', 'VARCHAR(36) DEFAULT NULL'),
            ]:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_coach_sessions' AND COLUMN_NAME = %s",
                    (col,),
                )
                if cur.fetchone()['cnt'] == 0:
                    cur.execute(f"ALTER TABLE ai_coach_sessions ADD COLUMN {col} {definition}")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_coach_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    session_id VARCHAR(36) NOT NULL,
                    user_id VARCHAR(36) NOT NULL,
                    role ENUM('user', 'assistant') NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_id (user_id),
                    INDEX idx_session_id (session_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS org_sentiment (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    org_slug VARCHAR(255) NOT NULL UNIQUE,
                    sentiment_data JSON NOT NULL,
                    calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_org_slug (org_slug)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS org_sentiment_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    org_slug VARCHAR(255) NOT NULL,
                    scores JSON NOT NULL,
                    calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_org_slug_date (org_slug, calculated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentiment (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    description TEXT,
                    type ENUM('Positive', 'Negative') NOT NULL,
                    INDEX idx_name (name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentiment_score (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    sentiment_id INT NOT NULL,
                    score TINYINT UNSIGNED NOT NULL,
                    calculated_at DATETIME NOT NULL,
                    user_id VARCHAR(36) NOT NULL,
                    FOREIGN KEY (sentiment_id) REFERENCES sentiment(id),
                    INDEX idx_user_date (user_id, calculated_at),
                    INDEX idx_sentiment_user (sentiment_id, user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentiment_analysis_cursor (
                    org_slug VARCHAR(255) PRIMARY KEY,
                    last_message_id INT NOT NULL DEFAULT 0,
                    last_analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            for name, description, stype in _SENTIMENT_SEEDS:
                cur.execute(
                    "INSERT IGNORE INTO sentiment (name, description, type) VALUES (%s, %s, %s)",
                    (name, description, stype),
                )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id VARCHAR(36) PRIMARY KEY,
                    nexa_access TINYINT(1) DEFAULT 0,
                    first_name VARCHAR(255) DEFAULT NULL,
                    last_name VARCHAR(255) DEFAULT NULL,
                    email VARCHAR(255) DEFAULT NULL,
                    org_id VARCHAR(255) DEFAULT NULL,
                    cohort_id VARCHAR(255) DEFAULT NULL,
                    cohort_name VARCHAR(255) DEFAULT NULL,
                    country VARCHAR(100) DEFAULT NULL,
                    first_login DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_login DATETIME DEFAULT CURRENT_TIMESTAMP,
                    access_last_date DATETIME DEFAULT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            for col, definition in [
                ('first_login',   'DATETIME DEFAULT CURRENT_TIMESTAMP AFTER nexa_access'),
                ('access_last_date', 'DATETIME DEFAULT NULL AFTER last_login'),
                ('first_name',    'VARCHAR(255) DEFAULT NULL AFTER nexa_access'),
                ('last_name',     'VARCHAR(255) DEFAULT NULL AFTER first_name'),
                ('email',         'VARCHAR(255) DEFAULT NULL AFTER last_name'),
                ('org_id',        'VARCHAR(255) DEFAULT NULL AFTER email'),
                ('cohort_id',     'VARCHAR(255) DEFAULT NULL AFTER org_id'),
                ('cohort_name',   'VARCHAR(255) DEFAULT NULL AFTER cohort_id'),
                ('country',       'VARCHAR(100) DEFAULT NULL AFTER cohort_name'),
            ]:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'user_settings' AND COLUMN_NAME = %s",
                    (col,),
                )
                if cur.fetchone()['cnt'] == 0:
                    cur.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {definition}")
            # Backfill access_last_date for existing rows that predate this column
            cur.execute(
                "UPDATE user_settings SET access_last_date = DATE_ADD(first_login, INTERVAL 7 DAY) "
                "WHERE access_last_date IS NULL"
            )
        conn.commit()
    finally:
        conn.close()


def create_chat_session(session_id: str, user_id: str, user_name: str = None, email: str = None,
                        org_name: str = None, org_slug: str = None, cohort_id: str = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_coach_sessions (session_id, user_id, user_name, email, org_name, org_slug, cohort_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (session_id, user_id, user_name, email, org_name, org_slug, cohort_id),
            )
        conn.commit()
    finally:
        conn.close()


def save_message(session_id: str, user_id: str, role: str, content: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_coach_messages (session_id, user_id, role, content) VALUES (%s, %s, %s, %s)",
                (session_id, user_id, role, content),
            )
        conn.commit()
    finally:
        conn.close()


def get_user_history(user_id: str, limit: int = 20) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content, created_at
                FROM ai_coach_messages
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()
            return list(reversed(rows))
    finally:
        conn.close()


def has_previous_sessions(user_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM ai_coach_messages WHERE user_id = %s", (user_id,)
            )
            row = cur.fetchone()
            return row['cnt'] > 0
    finally:
        conn.close()


def get_session_highlights(user_id: str, max_sessions: int = 5) -> list:
    """
    For each of the user's recent sessions returns the opening topic (first user
    message) and the closing takeaway (last assistant message). Used to build a
    concise history summary for the AI prompt rather than replaying raw messages.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, DATE(MIN(created_at)) AS session_date
                FROM ai_coach_messages
                WHERE user_id = %s
                GROUP BY session_id
                ORDER BY MIN(created_at) DESC
                LIMIT %s
                """,
                (user_id, max_sessions),
            )
            sessions = cur.fetchall()

            highlights = []
            for s in sessions:
                sid = s['session_id']
                date = str(s['session_date'])

                cur.execute(
                    """
                    SELECT content FROM ai_coach_messages
                    WHERE session_id = %s AND role = 'user'
                    ORDER BY created_at ASC LIMIT 1
                    """,
                    (sid,),
                )
                first_user = cur.fetchone()
                if not first_user:
                    continue

                cur.execute(
                    """
                    SELECT content FROM ai_coach_messages
                    WHERE session_id = %s AND role = 'assistant'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (sid,),
                )
                last_assistant = cur.fetchone()

                highlights.append({
                    'date': date,
                    'topic': first_user['content'][:250],
                    'takeaway': last_assistant['content'][:350] if last_assistant else '',
                })

            return list(reversed(highlights))  # oldest first
    finally:
        conn.close()


def get_org_analytics(org_slug: str) -> dict:
    """Returns overview stats and per-user activity for the given organisation."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    s.user_id,
                    MAX(s.user_name)    AS user_name,
                    MAX(s.email)        AS email,
                    COUNT(DISTINCT s.session_id) AS session_count,
                    SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS message_count,
                    MAX(m.created_at)   AS last_active,
                    MAX(us.country)     AS country,
                    MAX(us.cohort_name) AS cohort_name
                FROM ai_coach_sessions s
                LEFT JOIN ai_coach_messages m ON s.session_id = m.session_id
                LEFT JOIN user_settings us ON s.user_id = us.user_id
                WHERE s.org_slug = %s
                GROUP BY s.user_id
                ORDER BY last_active DESC
            """, (org_slug,))
            users = cur.fetchall()
            for u in users:
                if u['last_active']:
                    u['last_active'] = str(u['last_active'])
                u['message_count'] = int(u['message_count'] or 0)

            return {
                'total_users': len(users),
                'total_sessions': sum(u['session_count'] for u in users),
                'total_messages': sum(u['message_count'] for u in users),
                'users': users,
            }
    finally:
        conn.close()



def get_org_messages_by_user(org_slug: str, limit_per_user: int = 200) -> dict:
    """Returns {user_id: [message, ...]} for every user who has posted in the org."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.user_id, m.content
                FROM ai_coach_messages m
                JOIN ai_coach_sessions s ON m.session_id = s.session_id
                WHERE s.org_slug = %s AND m.role = 'user'
                ORDER BY s.user_id, m.created_at DESC
                """,
                (org_slug,),
            )
            rows = cur.fetchall()
        result: dict[str, list] = {}
        counts: dict[str, int] = {}
        for row in rows:
            uid = row['user_id']
            if uid not in result:
                result[uid] = []
                counts[uid] = 0
            if counts[uid] < limit_per_user:
                result[uid].append(row['content'])
                counts[uid] += 1
        return result
    finally:
        conn.close()


def get_all_org_slugs() -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT org_slug FROM ai_coach_sessions "
                "WHERE org_slug IS NOT NULL AND org_slug != ''"
            )
            return [row['org_slug'] for row in cur.fetchall()]
    finally:
        conn.close()


def get_org_user_messages(org_slug: str, limit: int = 1000) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.content
                FROM ai_coach_messages m
                JOIN ai_coach_sessions s ON m.session_id = s.session_id
                WHERE s.org_slug = %s AND m.role = 'user'
                ORDER BY m.created_at DESC
                LIMIT %s
                """,
                (org_slug, limit),
            )
            return [row['content'] for row in cur.fetchall()]
    finally:
        conn.close()


def get_org_sentiment(org_slug: str) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sentiment_data, calculated_at FROM org_sentiment WHERE org_slug = %s",
                (org_slug,),
            )
            row = cur.fetchone()
            if not row:
                return None
            data = row['sentiment_data']
            if isinstance(data, str):
                data = json.loads(data)
            data['calculated_at'] = str(row['calculated_at'])
            return data
    finally:
        conn.close()


def upsert_org_sentiment(org_slug: str, sentiment_data: dict):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO org_sentiment (org_slug, sentiment_data, calculated_at)
                VALUES (%s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    sentiment_data = VALUES(sentiment_data),
                    calculated_at = NOW()
                """,
                (org_slug, json.dumps(sentiment_data)),
            )
        conn.commit()
    finally:
        conn.close()


def insert_org_sentiment_history(org_slug: str, sentiment_data: dict):
    """Append a score snapshot to the history table. Keeps only the 8 dimension scores."""
    _DIMS = _SENTIMENT_DIMS
    scores = {
        dim: sentiment_data[dim]['score']
        for dim in _DIMS
        if isinstance(sentiment_data.get(dim), dict) and 'score' in sentiment_data[dim]
    }
    if not scores:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO org_sentiment_history (org_slug, scores, calculated_at) VALUES (%s, %s, NOW())",
                (org_slug, json.dumps(scores)),
            )
        conn.commit()
    finally:
        conn.close()


def get_org_sentiment_history(org_slug: str, limit: int = 12) -> list:
    """Return up to `limit` snapshots oldest-first: [{date, dim: score, ...}, ...]."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scores, calculated_at FROM org_sentiment_history "
                "WHERE org_slug = %s ORDER BY calculated_at DESC LIMIT %s",
                (org_slug, limit),
            )
            rows = cur.fetchall()
        result = []
        for row in reversed(rows):
            scores = row['scores']
            if isinstance(scores, str):
                scores = json.loads(scores)
            scores['date'] = str(row['calculated_at'])[:10]
            result.append(scores)
        return result
    finally:
        conn.close()


def upsert_user_login(user_id: str, first_name: str = None, last_name: str = None,
                      email: str = None, org_id: str = None, cohort_id: str = None,
                      cohort_name: str = None, country: str = None) -> dict:
    """Create or refresh a user_settings row; enables nexa_access. Returns nexa_access and access_last_date."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _fn = first_name or None
            _ln = last_name  or None
            _em = email      or None
            _oi = org_id     or None
            _ci = cohort_id  or None
            _cn = cohort_name or None
            _co = country    or None
            cur.execute(
                """
                INSERT INTO user_settings
                    (user_id, nexa_access, first_name, last_name, email,
                     org_id, cohort_id, cohort_name, country,
                     first_login, last_login, access_last_date)
                VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), DATE_ADD(NOW(), INTERVAL 7 DAY))
                ON DUPLICATE KEY UPDATE
                    last_login  = NOW(),
                    nexa_access = 1,
                    first_name  = COALESCE(%s, first_name),
                    last_name   = COALESCE(%s, last_name),
                    email       = COALESCE(%s, email),
                    org_id      = COALESCE(%s, org_id),
                    cohort_id   = COALESCE(%s, cohort_id),
                    cohort_name = COALESCE(%s, cohort_name),
                    country     = COALESCE(%s, country)
                """,
                (user_id, _fn, _ln, _em, _oi, _ci, _cn, _co,
                 _fn, _ln, _em, _oi, _ci, _cn, _co),
            )
            conn.commit()
            cur.execute(
                "SELECT nexa_access, access_last_date FROM user_settings WHERE user_id = %s", (user_id,)
            )
            row = cur.fetchone()
        return {
            'nexa_access': bool(row['nexa_access']) if row else True,
            'access_last_date': str(row['access_last_date']) if row and row['access_last_date'] else None,
        }
    finally:
        conn.close()


def get_user_access_settings(user_id: str) -> dict:
    """Returns nexa_access and access_last_date for the given user."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nexa_access, access_last_date FROM user_settings WHERE user_id = %s", (user_id,)
            )
            row = cur.fetchone()
        return {
            'nexa_access': bool(row['nexa_access']) if row else False,
            'access_last_date': str(row['access_last_date']) if row and row['access_last_date'] else None,
        }
    finally:
        conn.close()


def get_user_nexa_access(user_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nexa_access FROM user_settings WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return bool(row['nexa_access']) if row else False
    finally:
        conn.close()


def set_user_nexa_access(user_id: str, value: bool):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_settings (user_id, nexa_access)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE nexa_access = VALUES(nexa_access)
                """,
                (user_id, 1 if value else 0),
            )
        conn.commit()
    finally:
        conn.close()


def set_user_access_till(user_id: str, access_till: str):
    """Set access_last_date for a user. access_till is a date string 'YYYY-MM-DD'."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_settings (user_id, access_last_date)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE access_last_date = VALUES(access_last_date)
                """,
                (user_id, access_till),
            )
        conn.commit()
    finally:
        conn.close()


def disable_inactive_users(days: int = 30) -> int:
    """Disable nexa_access for users who haven't logged in for `days` days. Returns count disabled."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_settings
                SET nexa_access = 0
                WHERE nexa_access = 1
                  AND last_login < DATE_SUB(NOW(), INTERVAL %s DAY)
                """,
                (days,),
            )
            affected = cur.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


def get_sentiment_cursor(org_slug: str) -> int:
    """Returns the last analyzed message_id for the org (0 if never analyzed)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_message_id FROM sentiment_analysis_cursor WHERE org_slug = %s",
                (org_slug,),
            )
            row = cur.fetchone()
            return int(row['last_message_id']) if row else 0
    finally:
        conn.close()


def update_sentiment_cursor(org_slug: str, last_message_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sentiment_analysis_cursor (org_slug, last_message_id)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE last_message_id = VALUES(last_message_id)
                """,
                (org_slug, last_message_id),
            )
        conn.commit()
    finally:
        conn.close()


def get_org_max_message_id(org_slug: str) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(m.id) AS max_id
                FROM ai_coach_messages m
                JOIN ai_coach_sessions s ON m.session_id = s.session_id
                WHERE s.org_slug = %s AND m.role = 'user'
                """,
                (org_slug,),
            )
            row = cur.fetchone()
            return int(row['max_id']) if row and row['max_id'] else 0
    finally:
        conn.close()


def get_org_users_with_new_messages(org_slug: str, after_message_id: int) -> list:
    """Returns user_ids that have at least one message with id > after_message_id."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT s.user_id
                FROM ai_coach_messages m
                JOIN ai_coach_sessions s ON m.session_id = s.session_id
                WHERE s.org_slug = %s AND m.role = 'user' AND m.id > %s
                """,
                (org_slug, after_message_id),
            )
            return [row['user_id'] for row in cur.fetchall()]
    finally:
        conn.close()


def get_org_user_messages_after(org_slug: str, after_message_id: int, limit: int = 1000) -> list:
    """Returns user message contents with id > after_message_id for org-level LLM analysis."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.content
                FROM ai_coach_messages m
                JOIN ai_coach_sessions s ON m.session_id = s.session_id
                WHERE s.org_slug = %s AND m.role = 'user' AND m.id > %s
                ORDER BY m.id DESC
                LIMIT %s
                """,
                (org_slug, after_message_id, limit),
            )
            return [row['content'] for row in cur.fetchall()]
    finally:
        conn.close()


def insert_user_sentiment_scores(user_id: str, scores: dict, run_ts: str):
    """Insert one sentiment_score row per dimension for a single user."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM sentiment")
            dim_ids = {row['name']: row['id'] for row in cur.fetchall()}
            for dim, score in scores.items():
                dim_id = dim_ids.get(dim)
                if not dim_id:
                    continue
                cur.execute(
                    "INSERT INTO sentiment_score (sentiment_id, score, calculated_at, user_id) VALUES (%s, %s, %s, %s)",
                    (dim_id, max(0, min(100, int(score))), run_ts, user_id),
                )
        conn.commit()
    finally:
        conn.close()


def get_org_sentiment_data(org_slug: str, trend_limit: int = 12) -> dict | None:
    """
    Returns aggregated sentiment data for the org keyed by dimension name.
    Scores and bands are derived from latest per-user sentiment_score rows.
    Insights come from org_sentiment (LLM-generated).
    Trend is avg score per dimension grouped by run date.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Latest score per (user, dimension) for all org users
            cur.execute(
                """
                SELECT ss.user_id, s.name AS dim_name, ss.score
                FROM sentiment_score ss
                JOIN sentiment s ON ss.sentiment_id = s.id
                INNER JOIN (
                    SELECT user_id, sentiment_id, MAX(calculated_at) AS latest_at
                    FROM sentiment_score
                    WHERE user_id IN (
                        SELECT DISTINCT user_id FROM ai_coach_sessions WHERE org_slug = %s
                    )
                    GROUP BY user_id, sentiment_id
                ) latest ON ss.user_id = latest.user_id
                         AND ss.sentiment_id = latest.sentiment_id
                         AND ss.calculated_at = latest.latest_at
                """,
                (org_slug,),
            )
            latest_rows = cur.fetchall()

            if not latest_rows:
                return None

            # All-time trend grouped by date per dimension
            cur.execute(
                """
                SELECT DATE(ss.calculated_at) AS run_date,
                       s.name AS dim_name,
                       ROUND(AVG(ss.score)) AS avg_score
                FROM sentiment_score ss
                JOIN sentiment s ON ss.sentiment_id = s.id
                WHERE ss.user_id IN (
                    SELECT DISTINCT user_id FROM ai_coach_sessions WHERE org_slug = %s
                )
                GROUP BY DATE(ss.calculated_at), s.name
                ORDER BY run_date ASC
                """,
                (org_slug,),
            )
            trend_rows = cur.fetchall()

        # Aggregate latest scores → avg + bands per dimension
        from collections import defaultdict
        dim_scores: dict[str, list] = defaultdict(list)
        for row in latest_rows:
            dim_scores[row['dim_name']].append(row['score'])

        # Build trend dict, keeping only last trend_limit dates per dimension
        dim_trends: dict[str, list] = defaultdict(list)
        for row in trend_rows:
            dim_trends[row['dim_name']].append({
                'date': str(row['run_date']),
                'score': int(row['avg_score']),
            })
        for dim in dim_trends:
            dim_trends[dim] = dim_trends[dim][-trend_limit:]

        # Get LLM-generated insights from existing org_sentiment table
        insight_data = get_org_sentiment(org_slug) or {}

        result: dict = {}
        for dim in _SENTIMENT_DIMS:
            scores = dim_scores.get(dim, [])
            if not scores:
                continue
            n = len(scores)
            avg = round(sum(scores) / n)
            bands = {
                'very_high':  round(sum(1 for s in scores if s > 75)        / n * 100),
                'moderate':   round(sum(1 for s in scores if 50 < s <= 75)  / n * 100),
                'low':        round(sum(1 for s in scores if 25 < s <= 50)  / n * 100),
                'negligible': round(sum(1 for s in scores if s <= 25)       / n * 100),
            }
            old_entry = insight_data.get(dim)
            result[dim] = {
                'score': avg,
                'insight': old_entry.get('insight', '') if isinstance(old_entry, dict) else '',
                'bands': bands,
                'trend': dim_trends.get(dim, []),
            }

        result['messages_analyzed'] = insight_data.get('messages_analyzed', 0)
        result['users_analyzed'] = len(set(row['user_id'] for row in latest_rows))
        result['calculated_at'] = insight_data.get('calculated_at', '')
        return result
    finally:
        conn.close()


def get_all_users_admin() -> list:
    """Returns all users across all orgs with stats and nexa_access, for weace_super_admin."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    s.user_id,
                    MAX(s.user_name)  AS user_name,
                    MAX(s.email)      AS email,
                    MAX(s.org_name)   AS org_name,
                    MAX(s.cohort_id)  AS cohort_id,
                    MAX(s.started_at) AS last_login,
                    COUNT(DISTINCT s.session_id) AS session_count,
                    SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS chat_count,
                    COALESCE(us.nexa_access, 1) AS nexa_access,
                    us.first_login AS first_login,
                    us.access_last_date AS access_last_date
                FROM ai_coach_sessions s
                LEFT JOIN ai_coach_messages m ON s.session_id = m.session_id
                LEFT JOIN user_settings us ON s.user_id = us.user_id
                GROUP BY s.user_id
                ORDER BY last_login DESC
            """)
            users = cur.fetchall()
            for u in users:
                if u['last_login']:
                    u['last_login'] = str(u['last_login'])
                if u['first_login']:
                    u['first_login'] = str(u['first_login'])
                if u['access_last_date']:
                    u['access_last_date'] = str(u['access_last_date'])
                u['chat_count'] = int(u['chat_count'] or 0)
                u['nexa_access'] = bool(u['nexa_access'])
            return users
    finally:
        conn.close()
