"""
PostgreSQL idle cache cleaner + zombie session purger for Railway.

Monitors pg_stat_activity and runs CHECKPOINT + DISCARD ALL
when the database has been idle for more than IDLE_THRESHOLD_MINUTES.

Also purges user_sessions rows older than the refresh-token TTL + safety
margin, so the table doesn't grow indefinitely with zombies.

Runs as a separate Railway worker service.
"""
import logging
import time
from datetime import datetime, timezone

import psycopg2

IDLE_THRESHOLD_MINUTES = 10
CHECK_INTERVAL_SECONDS = 600  # 10 min
# Refresh token TTL is 30 days; +5 days safety margin to avoid racing with
# users actively using their refresh token at the boundary.
SESSION_ZOMBIE_DAYS = 35

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def get_last_activity(conn) -> datetime | None:
    """Return the most recent query_start from pg_stat_activity (excluding idle)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(query_start)
            FROM pg_stat_activity
            WHERE state != 'idle'
              AND datname = current_database()
              AND pid != pg_backend_pid()
        """)
        return cur.fetchone()[0]


def free_cache(conn):
    """Run CHECKPOINT then DISCARD ALL to release OS page cache."""
    with conn.cursor() as cur:
        logger.info("Running CHECKPOINT...")
        cur.execute("CHECKPOINT")
        logger.info("Running DISCARD ALL...")
        cur.execute("DISCARD ALL")
    logger.info("Cache released.")


def purge_zombie_sessions(conn):
    """Delete user_sessions rows whose refresh tokens are surely expired.

    A row is a zombie once last_used_at is older than the refresh-token TTL
    (30 days) plus a safety margin. Such rows cannot be ressuscitées and
    only clutter the table + the user's "Connected devices" view.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM user_sessions "
            f"WHERE last_used_at < NOW() - INTERVAL '{SESSION_ZOMBIE_DAYS} days'"
        )
        deleted = cur.rowcount
    if deleted:
        logger.info(f"Purged {deleted} zombie user_sessions (>= {SESSION_ZOMBIE_DAYS} days idle).")


def main():
    from src.utils.config import get_settings

    settings = get_settings()
    url = settings.effective_database_url
    logger.info(
        f"db_idle_cleaner starting. Threshold: {IDLE_THRESHOLD_MINUTES} min, "
        f"check every {CHECK_INTERVAL_SECONDS}s"
    )

    while True:
        try:
            conn = psycopg2.connect(url)
            conn.autocommit = True

            last_activity = get_last_activity(conn)
            now = datetime.now(timezone.utc)

            if last_activity is None:
                # No active queries at all
                idle_since = "unknown"
                should_free = True
            else:
                last_activity_utc = (
                    last_activity.replace(tzinfo=timezone.utc)
                    if last_activity.tzinfo is None
                    else last_activity
                )
                idle_minutes = (now - last_activity_utc).total_seconds() / 60
                idle_since = f"{idle_minutes:.1f} min"
                should_free = idle_minutes >= IDLE_THRESHOLD_MINUTES

            logger.info(f"Last activity: {idle_since} ago. Will free cache: {should_free}")

            if should_free:
                free_cache(conn)

            try:
                purge_zombie_sessions(conn)
            except Exception as e:
                logger.warning(f"Zombie session purge failed: {e}")

            conn.close()

        except Exception as e:
            logger.error(f"Error: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
