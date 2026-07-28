import json
import logging
import os
import uuid
from datetime import datetime
import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)


def _execute(cur, sql, params=None):
    """Execute a query and log it at DEBUG level."""
    if params is not None:
        logger.info("SQL: %s | params: %s", sql.strip(), params)
    else:
        logger.info("SQL: %s", sql.strip())
    return cur.execute(sql, params)

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
            _execute(cur,"""
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
                _execute(cur,
                    "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_coach_sessions' AND COLUMN_NAME = %s",
                    (col,),
                )
                if cur.fetchone()['cnt'] == 0:
                    _execute(cur,f"ALTER TABLE ai_coach_sessions ADD COLUMN {col} {definition}")
            _execute(cur,"""
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
            # Thumbs up/down a user leaves on a Nexa reply. One row per message —
            # re-rating overwrites, and clearing a rating deletes the row.
            _execute(cur,"""
                CREATE TABLE IF NOT EXISTS ai_coach_message_feedback (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    message_id INT NOT NULL UNIQUE,
                    user_id VARCHAR(36) NOT NULL,
                    rating TINYINT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_user_id (user_id),
                    INDEX idx_rating (rating)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            _execute(cur,"""
                CREATE TABLE IF NOT EXISTS org_sentiment (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    org_slug VARCHAR(255) NOT NULL UNIQUE,
                    sentiment_data JSON NOT NULL,
                    calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_org_slug (org_slug)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            _execute(cur,"""
                CREATE TABLE IF NOT EXISTS org_sentiment_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    org_slug VARCHAR(255) NOT NULL,
                    scores JSON NOT NULL,
                    calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_org_slug_date (org_slug, calculated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            _execute(cur,"""
                CREATE TABLE IF NOT EXISTS org_custom_content (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    org_slug VARCHAR(255) NOT NULL UNIQUE,
                    content TEXT,
                    updated_by VARCHAR(36),
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_org_slug (org_slug)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            _execute(cur,"""
                CREATE TABLE IF NOT EXISTS sentiment (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    description TEXT,
                    type ENUM('Positive', 'Negative') NOT NULL,
                    INDEX idx_name (name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            _execute(cur,"""
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
            _execute(cur,"""
                CREATE TABLE IF NOT EXISTS sentiment_analysis_cursor (
                    org_slug VARCHAR(255) PRIMARY KEY,
                    last_message_id INT NOT NULL DEFAULT 0,
                    last_analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            for name, description, stype in _SENTIMENT_SEEDS:
                _execute(cur,
                    "INSERT IGNORE INTO sentiment (name, description, type) VALUES (%s, %s, %s)",
                    (name, description, stype),
                )
            _execute(cur,"""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id VARCHAR(36) PRIMARY KEY,
                    nexa_access_cached TINYINT(1) DEFAULT 0,
                    first_name VARCHAR(255) DEFAULT NULL,
                    last_name VARCHAR(255) DEFAULT NULL,
                    email VARCHAR(255) DEFAULT NULL,
                    org_id VARCHAR(255) DEFAULT NULL,
                    org_name VARCHAR(255) DEFAULT NULL,
                    cohort_id VARCHAR(255) DEFAULT NULL,
                    cohort_name VARCHAR(255) DEFAULT NULL,
                    country VARCHAR(100) DEFAULT NULL,
                    level_name VARCHAR(255) DEFAULT NULL,
                    gender VARCHAR(50) DEFAULT NULL,
                    functional_areas TEXT DEFAULT NULL,
                    industry_types TEXT DEFAULT NULL,
                    language VARCHAR(50) DEFAULT NULL,
                    first_login DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_login DATETIME DEFAULT CURRENT_TIMESTAMP,
                    access_last_date DATETIME DEFAULT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            for col, definition in [
                ('first_login',      'DATETIME DEFAULT CURRENT_TIMESTAMP AFTER nexa_access_cached'),
                ('access_last_date', 'DATETIME DEFAULT NULL AFTER last_login'),
                ('first_name',       'VARCHAR(255) DEFAULT NULL AFTER nexa_access_cached'),
                ('last_name',        'VARCHAR(255) DEFAULT NULL AFTER first_name'),
                ('email',            'VARCHAR(255) DEFAULT NULL AFTER last_name'),
                ('org_id',           'VARCHAR(255) DEFAULT NULL AFTER email'),
                ('org_name',         'VARCHAR(255) DEFAULT NULL AFTER org_id'),
                ('cohort_id',        'VARCHAR(255) DEFAULT NULL AFTER org_name'),
                ('cohort_name',      'VARCHAR(255) DEFAULT NULL AFTER cohort_id'),
                ('country',          'VARCHAR(100) DEFAULT NULL AFTER cohort_name'),
                ('level_name',        'VARCHAR(255) DEFAULT NULL AFTER country'),
                ('gender',            'VARCHAR(50) DEFAULT NULL AFTER level_name'),
                ('functional_areas',  'TEXT DEFAULT NULL AFTER gender'),
                ('industry_types',    'TEXT DEFAULT NULL AFTER functional_areas'),
                ('language',          'VARCHAR(50) DEFAULT NULL AFTER industry_types'),
            ]:
                _execute(cur,
                    "SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'user_settings' AND COLUMN_NAME = %s",
                    (col,),
                )
                if cur.fetchone()['cnt'] == 0:
                    _execute(cur,f"ALTER TABLE user_settings ADD COLUMN {col} {definition}")
            # nexa_access is no longer an editable flag — access comes from the WeAce
            # user-config API at login. Rename the old column into a read-only cache.
            _execute(cur,
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'user_settings' "
                "AND COLUMN_NAME IN ('nexa_access', 'nexa_access_cached')"
            )
            _nexa_cols = {r['COLUMN_NAME'] for r in cur.fetchall()}
            if 'nexa_access' in _nexa_cols and 'nexa_access_cached' not in _nexa_cols:
                _execute(cur, "ALTER TABLE user_settings "
                              "CHANGE COLUMN nexa_access nexa_access_cached TINYINT(1) DEFAULT 0")
            elif 'nexa_access' in _nexa_cols:
                _execute(cur, "ALTER TABLE user_settings DROP COLUMN nexa_access")

            # Backfill access_last_date for existing rows that predate this column
            _execute(cur,
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
            _execute(cur,
                "INSERT INTO ai_coach_sessions (session_id, user_id, user_name, email, org_name, org_slug, cohort_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (session_id, user_id, user_name, email, org_name, org_slug, cohort_id),
            )
        conn.commit()
    finally:
        conn.close()


def save_message(session_id: str, user_id: str, role: str, content: str) -> int | None:
    """Persist a message and return its new id (used to attach feedback)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "INSERT INTO ai_coach_messages (session_id, user_id, role, content) VALUES (%s, %s, %s, %s)",
                (session_id, user_id, role, content),
            )
            message_id = cur.lastrowid
        conn.commit()
        return message_id
    finally:
        conn.close()


def set_message_feedback(message_id: int, user_id: str, rating: int) -> bool:
    """Record (or clear, with rating 0) a thumbs up/down on one of the user's own
    assistant messages. Returns False if the message isn't theirs or isn't a reply."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "SELECT id FROM ai_coach_messages WHERE id = %s AND user_id = %s AND role = 'assistant'",
                (message_id, user_id),
            )
            if not cur.fetchone():
                return False
            if rating == 0:
                _execute(cur, "DELETE FROM ai_coach_message_feedback WHERE message_id = %s", (message_id,))
            else:
                _execute(cur,
                    """
                    INSERT INTO ai_coach_message_feedback (message_id, user_id, rating)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE rating = VALUES(rating), user_id = VALUES(user_id)
                    """,
                    (message_id, user_id, rating),
                )
        conn.commit()
        return True
    finally:
        conn.close()


def get_message_feedback(user_id: str, message_ids: list) -> dict:
    """Map of message_id -> rating for the given messages, so restored history
    shows the thumbs the user already gave."""
    if not message_ids:
        return {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            placeholders = ', '.join(['%s'] * len(message_ids))
            _execute(cur,
                f"""
                SELECT message_id, rating
                FROM ai_coach_message_feedback
                WHERE user_id = %s AND message_id IN ({placeholders})
                """,
                (user_id, *message_ids),
            )
            return {r['message_id']: r['rating'] for r in cur.fetchall()}
    finally:
        conn.close()


def get_user_history(user_id: str, limit: int = 20) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                """
                SELECT id, role, content, created_at
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


def get_user_history_before(user_id: str, before_id: int, limit: int = 20) -> list:
    """Page older messages for infinite scroll: up to `limit` messages with an
    id lower than `before_id`, returned oldest-first."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                """
                SELECT id, role, content, created_at
                FROM ai_coach_messages
                WHERE user_id = %s AND id < %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (user_id, before_id, limit),
            )
            rows = cur.fetchall()
            return list(reversed(rows))
    finally:
        conn.close()


def is_resumable_session(session_id: str, user_id: str, max_age_hours: int = 12) -> bool:
    """True if this session belongs to the user and is recent enough to carry on
    with — lets a page reload (e.g. coming back from the dashboard) continue the
    same conversation instead of opening a new one.

    A long conversation stays resumable as long as it is still being used: the
    window applies to the last message when there is one, otherwise to the time
    the session opened."""
    if not session_id or not user_id:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "SELECT COUNT(*) AS cnt FROM ai_coach_sessions s "
                "WHERE s.session_id = %s AND s.user_id = %s "
                "AND (s.started_at > DATE_SUB(NOW(), INTERVAL %s HOUR) "
                "     OR EXISTS (SELECT 1 FROM ai_coach_messages m "
                "                WHERE m.session_id = s.session_id "
                "                AND m.created_at > DATE_SUB(NOW(), INTERVAL %s HOUR)))",
                (session_id, user_id, max_age_hours, max_age_hours),
            )
            return cur.fetchone()['cnt'] > 0
    finally:
        conn.close()


def has_previous_sessions(user_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "SELECT COUNT(*) AS cnt FROM ai_coach_messages WHERE user_id = %s", (user_id,)
            )
            row = cur.fetchone()
            return row['cnt'] > 0
    finally:
        conn.close()


def get_session_highlights(user_id: str, max_sessions: int = 50) -> list:
    """
    For each of the user's recent sessions returns the opening topic (first user
    message) and the closing takeaway (last assistant message). Used to build a
    concise history summary for the AI prompt rather than replaying raw messages.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # One round trip: the derived table picks the user's most recent
            # sessions and, per session, the row ids of the first user message
            # and the last assistant message; the joins then fetch just those two
            # rows by primary key. Ids stand in for created_at ordering — they're
            # auto-increment, so they break the DATETIME's per-second ties.
            _execute(cur,
                """
                SELECT s.session_date,
                       first_user.content      AS topic,
                       last_assistant.content  AS takeaway
                FROM (
                    SELECT DATE(MIN(created_at)) AS session_date,
                           MIN(created_at) AS started_at,
                           MIN(CASE WHEN role = 'user' THEN id END) AS first_user_id,
                           MAX(CASE WHEN role = 'assistant' THEN id END) AS last_assistant_id
                    FROM ai_coach_messages
                    WHERE user_id = %s
                    GROUP BY session_id
                    HAVING first_user_id IS NOT NULL
                    ORDER BY started_at DESC
                    LIMIT %s
                ) AS s
                JOIN ai_coach_messages AS first_user
                  ON first_user.id = s.first_user_id
                LEFT JOIN ai_coach_messages AS last_assistant
                  ON last_assistant.id = s.last_assistant_id
                ORDER BY s.started_at ASC
                """,
                (user_id, max_sessions),
            )

            return [
                {
                    'date': str(row['session_date']),
                    'topic': row['topic'][:250],
                    'takeaway': (row['takeaway'] or '')[:350],
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


def get_org_analytics(org_id: str, date_from=None, date_to=None, gender=None, level_name=None,
                      cohort_name=None, industry_type=None) -> dict:
    """Returns overview stats and per-user activity for the given organisation."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            params = [org_id]
            date_clause = ""
            if date_from:
                date_clause += " AND DATE(s.started_at) >= %s"
                params.append(date_from)
            if date_to:
                date_clause += " AND DATE(s.started_at) <= %s"
                params.append(date_to)
            attr_clause = ""
            if gender:
                attr_clause += " AND us.gender = %s"
                params.append(gender)
            if level_name:
                attr_clause += " AND us.level_name = %s"
                params.append(level_name)
            if cohort_name:
                attr_clause += " AND us.cohort_name = %s"
                params.append(cohort_name)
            if industry_type:
                attr_clause += " AND JSON_CONTAINS(us.industry_types, %s)"
                params.append(json.dumps(industry_type))

            query = f"""
                SELECT
                    s.user_id,
                    MAX(s.user_name)    AS user_name,
                    MAX(s.email)        AS email,
                    COUNT(DISTINCT s.session_id) AS session_count,
                    SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS message_count,
                    MAX(m.created_at)   AS last_active,
                    MIN(s.started_at)   AS first_session_at,
                    MAX(us.country)     AS country,
                    MAX(us.cohort_name) AS cohort_name
                FROM ai_coach_sessions s
                LEFT JOIN ai_coach_messages m ON s.session_id = m.session_id
                LEFT JOIN user_settings us ON s.user_id = us.user_id
                WHERE us.org_id = %s{date_clause}{attr_clause}
                GROUP BY s.user_id
                ORDER BY last_active DESC
            """
            
            _execute(cur,query, params)
            users = cur.fetchall()
            first_session_date = None
            for u in users:
                if u['last_active']:
                    u['last_active'] = str(u['last_active'])
                if u.get('first_session_at'):
                    d = str(u['first_session_at'])[:10]
                    if first_session_date is None or d < first_session_date:
                        first_session_date = d
                    del u['first_session_at']
                u['message_count'] = int(u['message_count'] or 0)

            return {
                'total_users': len(users),
                'total_sessions': sum(u['session_count'] for u in users),
                'total_messages': sum(u['message_count'] for u in users),
                'first_session_date': first_session_date,
                'users': users,
            }
    finally:
        conn.close()



def get_org_messages_by_user(org_slug: str, limit_per_user: int = 200) -> dict:
    """Returns {user_id: [message, ...]} for every user who has posted in the org."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
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
            _execute(cur,
                "SELECT DISTINCT org_slug FROM ai_coach_sessions "
                "WHERE org_slug IS NOT NULL AND org_slug != ''"
            )
            return [row['org_slug'] for row in cur.fetchall()]
    finally:
        conn.close()


def get_all_orgs() -> list:
    """Returns distinct organisations (slug + display name) that have coaching sessions."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "SELECT org_slug, MAX(org_name) AS org_name "
                "FROM ai_coach_sessions "
                "WHERE org_slug IS NOT NULL AND org_slug != '' "
                "GROUP BY org_slug ORDER BY org_name"
            )
            return [{'slug': r['org_slug'], 'name': r['org_name'] or r['org_slug']}
                    for r in cur.fetchall()]
    finally:
        conn.close()


def get_org_user_messages(org_slug: str, limit: int = 1000) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
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
            _execute(cur,
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


def get_org_custom_content(org_slug: str) -> str | None:
    """Corporate-specific knowledge/content that Nexa weaves into its replies for
    users of this organisation. Returns None when nothing has been set."""
    if not org_slug:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "SELECT content FROM org_custom_content WHERE org_slug = %s",
                (org_slug,),
            )
            row = cur.fetchone()
            return (row['content'] if row else None)
    finally:
        conn.close()


def upsert_org_custom_content(org_slug: str, content: str, updated_by: str = None):
    """Create or replace an organisation's custom coaching content."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                """
                INSERT INTO org_custom_content (org_slug, content, updated_by)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    content = VALUES(content),
                    updated_by = VALUES(updated_by)
                """,
                (org_slug, content, updated_by),
            )
        conn.commit()
    finally:
        conn.close()


def upsert_org_sentiment(org_slug: str, sentiment_data: dict):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
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
            _execute(cur,
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
            _execute(cur,
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
                      email: str = None, org_id: str = None, org_name: str = None,
                      cohort_id: str = None, cohort_name: str = None, country: str = None,
                      level_name: str = None, gender: str = None,
                      functional_areas: list = None, industry_types: list = None,
                      nexa_access: bool = False) -> dict:
    """Create or refresh a user_settings row at login.

    nexa_access comes from the WeAce user-config API and is stored only as a
    read-only cache for the admin dashboard. Returns access_last_date."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _fn = first_name  or None
            _ln = last_name   or None
            _em = email       or None
            _oi = org_id      or None
            _on = org_name    or None
            _ci = cohort_id   or None
            _cn = cohort_name or None
            _co = country     or None
            _lv = level_name  or None
            _gd = gender      or None
            _fa = json.dumps(functional_areas) if functional_areas else None
            _it = json.dumps(industry_types)   if industry_types   else None
            _execute(cur,
                """
                INSERT INTO user_settings
                    (user_id, nexa_access_cached, first_name, last_name, email,
                     org_id, org_name, cohort_id, cohort_name, country, level_name, gender,
                     functional_areas, industry_types,
                     first_login, last_login, access_last_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), DATE_ADD(NOW(), INTERVAL 7 DAY))
                ON DUPLICATE KEY UPDATE
                    last_login         = NOW(),
                    nexa_access_cached = VALUES(nexa_access_cached),
                    first_name        = COALESCE(%s, first_name),
                    last_name         = COALESCE(%s, last_name),
                    email             = COALESCE(%s, email),
                    org_id            = COALESCE(%s, org_id),
                    org_name          = COALESCE(%s, org_name),
                    cohort_id         = COALESCE(%s, cohort_id),
                    cohort_name       = COALESCE(%s, cohort_name),
                    country           = COALESCE(%s, country),
                    level_name        = COALESCE(%s, level_name),
                    gender            = COALESCE(%s, gender),
                    functional_areas  = COALESCE(%s, functional_areas),
                    industry_types    = COALESCE(%s, industry_types)
                """,
                (user_id, 1 if nexa_access else 0,
                 _fn, _ln, _em, _oi, _on, _ci, _cn, _co, _lv, _gd, _fa, _it,
                 _fn, _ln, _em, _oi, _on, _ci, _cn, _co, _lv, _gd, _fa, _it),
            )
            conn.commit()
            _execute(cur,
                "SELECT access_last_date FROM user_settings WHERE user_id = %s", (user_id,)
            )
            row = cur.fetchone()
        return {
            'access_last_date': str(row['access_last_date']) if row and row['access_last_date'] else None,
        }
    finally:
        conn.close()


def get_user_identity(user_id: str) -> dict:
    """Name / email / org fields stored at login.

    Lets a session be resumed (e.g. coming back from the dashboard) without
    re-fetching the profile APIs when the in-memory token cache is cold.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "SELECT first_name, last_name, email, org_id, org_name, cohort_id "
                "FROM user_settings WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_user_profile_context(user_id: str) -> dict:
    """Returns stored profile context fields used to personalise AI prompts."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "SELECT level_name, gender, country, cohort_name, functional_areas, industry_types "
                "FROM user_settings WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return {}
        result = dict(row)
        for key in ('functional_areas', 'industry_types'):
            val = result.get(key)
            if isinstance(val, str):
                try:
                    result[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    result[key] = []
            elif not val:
                result[key] = []
        return result
    finally:
        conn.close()


def get_user_access_settings(user_id: str) -> dict:
    """Returns the cached nexa access flag and access_last_date for the given user."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                "SELECT nexa_access_cached, access_last_date FROM user_settings WHERE user_id = %s",
                (user_id,)
            )
            row = cur.fetchone()
        return {
            'nexa_access': bool(row['nexa_access_cached']) if row else False,
            'access_last_date': str(row['access_last_date']) if row and row['access_last_date'] else None,
        }
    finally:
        conn.close()


def get_user_language(user_id: str) -> str | None:
    """Returns the user's preferred coaching language, or None if never set."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur, "SELECT language FROM user_settings WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        return (row['language'] or None) if row else None
    finally:
        conn.close()


def set_user_language(user_id: str, language: str):
    """Store the user's preferred coaching language."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                """
                INSERT INTO user_settings (user_id, language)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE language = VALUES(language)
                """,
                (user_id, language),
            )
        conn.commit()
    finally:
        conn.close()


def get_sentiment_cursor(org_slug: str) -> int:
    """Returns the last analyzed message_id for the org (0 if never analyzed)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
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
            _execute(cur,
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
            _execute(cur,
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
            _execute(cur,
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
            _execute(cur,
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
            _execute(cur,"SELECT id, name FROM sentiment")
            dim_ids = {row['name']: row['id'] for row in cur.fetchall()}
            for dim, score in scores.items():
                dim_id = dim_ids.get(dim)
                if not dim_id:
                    continue
                _execute(cur,
                    "INSERT INTO sentiment_score (sentiment_id, score, calculated_at, user_id) VALUES (%s, %s, %s, %s)",
                    (dim_id, max(0, min(100, int(score))), run_ts, user_id),
                )
        conn.commit()
    finally:
        conn.close()


def get_org_filter_options(org_slug: str) -> dict:
    """Returns distinct gender, level_name, and industry_types values for users in the org."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _execute(cur,
                """
                SELECT DISTINCT us.gender
                FROM user_settings us
                JOIN ai_coach_sessions s ON us.user_id = s.user_id
                WHERE s.org_slug = %s AND us.gender IS NOT NULL AND us.gender != ''
                ORDER BY us.gender
                """,
                (org_slug,),
            )
            genders = [row['gender'] for row in cur.fetchall()]

            _execute(cur,
                """
                SELECT DISTINCT us.level_name
                FROM user_settings us
                JOIN ai_coach_sessions s ON us.user_id = s.user_id
                WHERE s.org_slug = %s AND us.level_name IS NOT NULL AND us.level_name != ''
                ORDER BY us.level_name
                """,
                (org_slug,),
            )
            level_names = [row['level_name'] for row in cur.fetchall()]

            _execute(cur,
                """
                SELECT us.industry_types
                FROM user_settings us
                JOIN ai_coach_sessions s ON us.user_id = s.user_id
                WHERE s.org_slug = %s AND us.industry_types IS NOT NULL AND us.industry_types != ''
                  AND us.industry_types != '[]'
                """,
                (org_slug,),
            )
            seen: set = set()
            for row in cur.fetchall():
                val = row['industry_types']
                try:
                    items = json.loads(val) if isinstance(val, str) else (val or [])
                except (json.JSONDecodeError, ValueError):
                    items = []
                for item in items:
                    if item:
                        seen.add(item)
            industry_types = sorted(seen)

        return {'genders': genders, 'level_names': level_names, 'industry_types': industry_types}
    finally:
        conn.close()


def get_org_sentiment_data(org_slug, trend_limit: int = 12, date_from=None, date_to=None,
                           gender=None, level_name=None, cohort_name=None) -> dict | None:
    """
    Returns aggregated sentiment data for the org keyed by dimension name.
    Pass org_slug=None to aggregate across all organisations (weace_super_admin view).
    Scores and bands are derived from latest per-user sentiment_score rows.
    Insights come from org_sentiment (LLM-generated); omitted for the all-orgs view.
    Trend is avg score per dimension grouped by run date.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Build user filter subquery. org_slug=None means all organisations (weace_super_admin).
            if gender or level_name or cohort_name:
                user_subquery = ("SELECT DISTINCT s.user_id FROM ai_coach_sessions s "
                                 "JOIN user_settings us ON s.user_id = us.user_id")
                conds: list = []
                user_subquery_params: list = []
                if org_slug:
                    conds.append("s.org_slug = %s")
                    user_subquery_params.append(org_slug)
                if gender:
                    conds.append("us.gender = %s")
                    user_subquery_params.append(gender)
                if level_name:
                    conds.append("us.level_name = %s")
                    user_subquery_params.append(level_name)
                if cohort_name:
                    conds.append("us.cohort_name = %s")
                    user_subquery_params.append(cohort_name)
                if conds:
                    user_subquery += " WHERE " + " AND ".join(conds)
            elif org_slug:
                user_subquery = "SELECT DISTINCT user_id FROM ai_coach_sessions WHERE org_slug = %s"
                user_subquery_params = [org_slug]
            else:
                user_subquery = "SELECT DISTINCT user_id FROM ai_coach_sessions"
                user_subquery_params = []

            # Build optional date filter for sentiment_score.calculated_at
            score_date_clause = ""
            score_date_params: list = []
            if date_from:
                score_date_clause += " AND DATE(calculated_at) >= %s"
                score_date_params.append(date_from)
            if date_to:
                score_date_clause += " AND DATE(calculated_at) <= %s"
                score_date_params.append(date_to)

            # Latest score per (user, dimension) within date range
            _execute(cur,
                f"""
                SELECT ss.user_id, s.name AS dim_name, ss.score
                FROM sentiment_score ss
                JOIN sentiment s ON ss.sentiment_id = s.id
                INNER JOIN (
                    SELECT user_id, sentiment_id, MAX(calculated_at) AS latest_at
                    FROM sentiment_score
                    WHERE user_id IN (
                        {user_subquery}
                    ){score_date_clause}
                    GROUP BY user_id, sentiment_id
                ) latest ON ss.user_id = latest.user_id
                         AND ss.sentiment_id = latest.sentiment_id
                         AND ss.calculated_at = latest.latest_at
                """,
                user_subquery_params + score_date_params,
            )
            latest_rows = cur.fetchall()

            if not latest_rows:
                return None

            # Trend grouped by date per dimension (filtered by date range)
            trend_date_clause = ""
            trend_date_params: list = []
            if date_from:
                trend_date_clause += " AND DATE(ss.calculated_at) >= %s"
                trend_date_params.append(date_from)
            if date_to:
                trend_date_clause += " AND DATE(ss.calculated_at) <= %s"
                trend_date_params.append(date_to)

            _execute(cur,
                f"""
                SELECT DATE(ss.calculated_at) AS run_date,
                       s.name AS dim_name,
                       ROUND(AVG(ss.score)) AS avg_score
                FROM sentiment_score ss
                JOIN sentiment s ON ss.sentiment_id = s.id
                WHERE ss.user_id IN (
                    {user_subquery}
                ){trend_date_clause}
                GROUP BY DATE(ss.calculated_at), s.name
                ORDER BY run_date ASC
                """,
                user_subquery_params + trend_date_params,
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

        # Get LLM-generated insights from existing org_sentiment table (per-org only)
        insight_data = (get_org_sentiment(org_slug) or {}) if org_slug else {}

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


def get_all_users_admin(org_slug=None) -> list:
    """Returns users with stats and their last-known Nexa access, for weace_super_admin.
    Pass org_slug to scope to a single organisation; None returns all orgs."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            where = ""
            params: list = []
            if org_slug:
                where = "WHERE s.org_slug = %s"
                params.append(org_slug)
            _execute(cur, f"""
                SELECT
                    s.user_id,
                    MAX(s.user_name)  AS user_name,
                    MAX(s.email)      AS email,
                    MAX(s.org_name)   AS org_name,
                    MAX(s.org_slug)   AS org_slug,
                    MAX(s.cohort_id)  AS cohort_id,
                    MAX(s.started_at) AS last_login,
                    COUNT(DISTINCT s.session_id) AS session_count,
                    SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS chat_count,
                    COALESCE(us.nexa_access_cached, 0) AS nexa_access,
                    us.first_login AS first_login,
                    us.access_last_date AS access_last_date
                FROM ai_coach_sessions s
                LEFT JOIN ai_coach_messages m ON s.session_id = m.session_id
                LEFT JOIN user_settings us ON s.user_id = us.user_id
                {where}
                GROUP BY s.user_id
                ORDER BY last_login DESC
            """, params)
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
