# --- Imports ---
from __future__ import annotations

import os
import sys
import re
import time
import json
import html
import io
import hashlib
import queue
import signal
import random
import logging
import threading
import sqlite3
from enum import IntEnum
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from collections import OrderedDict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
import tweepy
from groq import Groq
from flask import Flask, jsonify, request
from logging.handlers import RotatingFileHandler

# --- Initial Setup ---
load_dotenv()

# --- Constants ---
MAX_SEARCH_RESULTS = 10  # Increased from 5
FEEDBACK_LOG_FILE = "feedback.log"
CONTEXT_CACHE_SIZE = 200  # Increased from 100
MAX_PROCESSED_ARTICLES = 2000  # Increased from 1000
MAX_SENT_HASHES = 10000  # Increased from 5000
DB_FILE = "bot_data.db"
MAX_RETRY_ATTEMPTS = 3
REQUEST_TIMEOUT_DEFAULT = 30

DEFAULT_NEWS_KEYWORDS = "Bitcoin, Crypto, Blockchain"
ADMIN_TOKEN_ENV = "ADMIN_TOKEN"


# --- Priority Enum ---
class Priority(IntEnum):
    CRITICAL = 0  # System critical operations
    HIGH = 1      # User questions
    NORMAL = 2    # Article processing
    LOW = 3       # Background tasks


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_pub_date_to_ts(pub: Optional[str]) -> float:
    """
    Best-effort parse of pubDate strings from various APIs.
    Returns unix timestamp (float). Unknown formats => 0.
    """
    if not pub or not isinstance(pub, str):
        return 0.0

    s = pub.strip()
    if not s:
        return 0.0

    # Common ISO variants
    iso_candidates = [
        s,
        s.replace("Z", "+00:00"),
    ]
    for cand in iso_candidates:
        try:
            return datetime.fromisoformat(cand).timestamp()
        except Exception:
            pass

    # RFC 2822 / HTTP-date-ish patterns (very common in RSS)
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            continue

    return 0.0


# --- Database Manager ---
class DatabaseManager:
    """Thread-safe SQLite database manager for persistent storage"""

    def __init__(self, db_file: str = DB_FILE):
        self.db_file = db_file
        self.lock = threading.RLock()
        self._initialize_db()

    def _initialize_db(self) -> None:
        """Create database tables if they don't exist"""
        with self.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_articles (
                    hash TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    original_title TEXT,
                    summary TEXT,
                    hashtags TEXT,
                    link TEXT UNIQUE NOT NULL,
                    image_url TEXT,
                    source TEXT,
                    pub_date TEXT,
                    timestamp TEXT NOT NULL,
                    posted_twitter INTEGER DEFAULT 0
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER PRIMARY KEY,
                    total_questions INTEGER DEFAULT 0,
                    last_interaction TEXT,
                    article_index INTEGER DEFAULT 0
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    user_id INTEGER,
                    chat_id INTEGER,
                    article_hash TEXT,
                    rating TEXT,
                    title_hint TEXT
                )
                """
            )

            # Indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_timestamp ON sent_articles(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_timestamp ON processed_articles(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id)")

            conn.commit()

    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(self.db_file, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with self.lock:
                yield conn
        finally:
            conn.close()

    def add_sent_article(self, article_hash: str) -> None:
        """Add article hash to sent articles"""
        if not article_hash:
            return
        with self.get_connection() as conn:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO sent_articles (hash, timestamp) VALUES (?, ?)",
                    (article_hash, now_utc_iso()),
                )
                conn.commit()
            except sqlite3.Error as e:
                logging.error(f"Database error adding sent article: {e}")

    def is_article_sent(self, article_hash: str) -> bool:
        """Check if article was already sent"""
        if not article_hash:
            return False
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT 1 FROM sent_articles WHERE hash = ?", (article_hash,))
            return cursor.fetchone() is not None

    def get_sent_articles_count(self) -> int:
        """Get total count of sent articles"""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM sent_articles")
            return int(cursor.fetchone()[0])

    def cleanup_old_sent_articles(self, days_to_keep: int = 30) -> int:
        """Remove sent articles older than specified days"""
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.execute("DELETE FROM sent_articles WHERE timestamp < ?", (cutoff_date,))
            deleted = cursor.rowcount or 0
            conn.commit()
            return int(deleted)

    def cleanup_old_processed_articles(self, max_rows: int = MAX_PROCESSED_ARTICLES) -> int:
        """
        Keep processed_articles bounded (optional hygiene).
        Deletes oldest rows beyond max_rows.
        """
        with self.get_connection() as conn:
            try:
                cursor = conn.execute("SELECT COUNT(*) FROM processed_articles")
                total = int(cursor.fetchone()[0])
                if total <= max_rows:
                    return 0

                to_delete = total - max_rows
                # delete oldest
                del_cursor = conn.execute(
                    """
                    DELETE FROM processed_articles
                    WHERE id IN (
                        SELECT id FROM processed_articles
                        ORDER BY timestamp ASC
                        LIMIT ?
                    )
                    """,
                    (to_delete,),
                )
                deleted = del_cursor.rowcount or 0
                conn.commit()
                return int(deleted)
            except sqlite3.Error as e:
                logging.error(f"Database error cleaning processed articles: {e}")
                return 0

    def add_processed_article(self, article_data: Dict[str, Any]) -> None:
        """Add processed article to database"""
        with self.get_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO processed_articles
                    (title, original_title, summary, hashtags, link, image_url, source, pub_date, timestamp, posted_twitter)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article_data.get("title"),
                        article_data.get("original_title"),
                        article_data.get("summary"),
                        article_data.get("hashtags"),
                        article_data.get("link"),
                        article_data.get("image_url"),
                        article_data.get("source"),
                        article_data.get("pubDate"),
                        article_data.get("timestamp"),
                        1 if article_data.get("posted_twitter") else 0,
                    ),
                )
                conn.commit()
            except sqlite3.Error as e:
                logging.error(f"Database error adding processed article: {e}")

    def get_processed_articles(self, limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        """Retrieve processed articles from database"""
        with self.get_connection() as conn:
            query = "SELECT * FROM processed_articles ORDER BY timestamp DESC"
            params: Tuple[Any, ...] = ()
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                params = (int(limit), int(offset))
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def search_articles(self, search_term: str, limit: int = MAX_SEARCH_RESULTS) -> List[Dict[str, Any]]:
        """Search articles by term"""
        with self.get_connection() as conn:
            query = """
                SELECT * FROM processed_articles
                WHERE title LIKE ? OR summary LIKE ? OR original_title LIKE ? OR link LIKE ?
                ORDER BY timestamp DESC LIMIT ?
            """
            search_pattern = f"%{search_term}%"
            cursor = conn.execute(query, (search_pattern, search_pattern, search_pattern, search_pattern, int(limit)))
            return [dict(row) for row in cursor.fetchall()]

    def log_feedback(self, user_id: int, chat_id: int, article_hash: str, rating: str, title_hint: str) -> None:
        """Log user feedback to database"""
        with self.get_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO feedback (timestamp, user_id, chat_id, article_hash, rating, title_hint)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_utc_iso(),
                        int(user_id),
                        int(chat_id),
                        article_hash,
                        rating,
                        (title_hint or "")[:200],
                    ),
                )
                conn.commit()
            except sqlite3.Error as e:
                logging.error(f"Database error logging feedback: {e}")

    def update_user_stats(self, user_id: int, increment_questions: bool = False, article_index: Optional[int] = None) -> None:
        """Update user statistics"""
        with self.get_connection() as conn:
            try:
                cursor = conn.execute("SELECT * FROM user_stats WHERE user_id = ?", (int(user_id),))
                existing = cursor.fetchone()

                if existing:
                    updates = []
                    params: List[Any] = []

                    if increment_questions:
                        updates.append("total_questions = total_questions + 1")

                    updates.append("last_interaction = ?")
                    params.append(now_utc_iso())

                    if article_index is not None:
                        updates.append("article_index = ?")
                        params.append(int(article_index))

                    params.append(int(user_id))

                    conn.execute(f"UPDATE user_stats SET {', '.join(updates)} WHERE user_id = ?", params)
                else:
                    conn.execute(
                        """
                        INSERT INTO user_stats (user_id, total_questions, last_interaction, article_index)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            int(user_id),
                            1 if increment_questions else 0,
                            now_utc_iso(),
                            int(article_index or 0),
                        ),
                    )

                conn.commit()
            except sqlite3.Error as e:
                logging.error(f"Database error updating user stats: {e}")

    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        with self.get_connection() as conn:
            try:
                cursor = conn.execute("SELECT * FROM user_stats WHERE user_id = ?", (int(user_id),))
                row = cursor.fetchone()
                return dict(row) if row else {}
            except sqlite3.Error as e:
                logging.error(f"Database error getting user stats: {e}")
                return {}


# --- Configuration Class ---
@dataclass
class BotConfig:
    """Bot configuration with validation"""
    telegram_bot_token: str
    telegram_channel_id: str
    groq_api_key: str
    newsdata_api_key: Optional[str] = None
    marketaux_api_key: Optional[str] = None
    etherscan_api_key: Optional[str] = None

    # Twitter/X credentials
    twitter_api_key: Optional[str] = None
    twitter_api_secret_key: Optional[str] = None
    twitter_access_token: Optional[str] = None
    twitter_access_token_secret: Optional[str] = None
    twitter_bearer_token: Optional[str] = None

    # Timing configs
    news_check_interval: int = 1800
    tweet_post_delay: int = 120
    user_reset_timeout: int = 300
    groq_api_timeout: int = 60
    question_api_timeout: int = 45
    external_api_timeout: int = 15
    image_validation_timeout: int = 7
    sort_refresh_interval: int = 300

    # LLM configs
    groq_primary_model: str = "llama-3.3-70b-versatile"
    groq_fallback_model: str = "llama-3.1-8b-instant"
    llm_temperature: float = 0.2
    llm_top_p: float = 1.0
    llm_max_tokens: int = 8192

    # Service tier
    groq_service_tier: str = "auto"  # 'on_demand', 'flex', or 'auto'

    @classmethod
    def from_env(cls) -> "BotConfig":
        def get_env_int(var_name: str, default: int, min_val: int = 1) -> int:
            try:
                value = int(os.getenv(var_name, default))
                return max(min_val, value)
            except (ValueError, TypeError):
                return default

        def get_env_float(var_name: str, default: float) -> float:
            try:
                return float(os.getenv(var_name, default))
            except (ValueError, TypeError):
                return default

        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_channel_id=os.getenv("TELEGRAM_CHANNEL_ID", ""),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            newsdata_api_key=os.getenv("NEWSDATA_API_KEY"),
            marketaux_api_key=os.getenv("MARKETAUX_API_KEY"),
            etherscan_api_key=os.getenv("ETHERSCAN_API_KEY"),
            twitter_api_key=os.getenv("API_KEY"),
            twitter_api_secret_key=os.getenv("API_SECRET_KEY"),
            twitter_access_token=os.getenv("ACCESS_TOKEN"),
            twitter_access_token_secret=os.getenv("ACCESS_TOKEN_SECRET"),
            twitter_bearer_token=os.getenv("BEARER_TOKEN"),
            news_check_interval=get_env_int("NEWS_CHECK_INTERVAL_SECONDS", 1800),
            tweet_post_delay=get_env_int("TWEET_POST_DELAY_SECONDS", 120),
            user_reset_timeout=get_env_int("USER_RESET_TIMEOUT", 300),
            groq_api_timeout=get_env_int("GROQ_API_TIMEOUT", 60),
            question_api_timeout=get_env_int("QUESTION_API_TIMEOUT", 45),
            external_api_timeout=get_env_int("EXTERNAL_API_TIMEOUT", 15),
            image_validation_timeout=get_env_int("IMAGE_VALIDATION_TIMEOUT", 7),
            sort_refresh_interval=get_env_int("SORT_REFRESH_INTERVAL_SECONDS", 300),
            groq_primary_model=os.getenv("GROQ_PRIMARY_MODEL", "llama-3.3-70b-versatile"),
            groq_fallback_model=os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant"),
            llm_temperature=get_env_float("LLM_TEMPERATURE", 0.2),
            llm_top_p=get_env_float("LLM_TOP_P", 1.0),
            llm_max_tokens=get_env_int("LLM_MAX_TOKENS", 8192),
            groq_service_tier=os.getenv("GROQ_SERVICE_TIER", "auto"),
        )

    def validate(self) -> None:
        if not all([self.telegram_bot_token, self.telegram_channel_id, self.groq_api_key]):
            raise ValueError("Missing required env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GROQ_API_KEY")

        if not any([self.newsdata_api_key, self.marketaux_api_key]):
            raise ValueError("At least one News API key required: NEWSDATA_API_KEY or MARKETAUX_API_KEY")

        if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", self.telegram_bot_token):
            logging.warning("Telegram bot token format looks invalid")

        if not (self.telegram_channel_id.startswith("@") or self.telegram_channel_id.startswith("-")):
            logging.warning("Telegram channel ID format may be invalid")


# --- LRU Cache Implementation ---
class LRUCache:
    """Thread-safe LRU cache using OrderedDict"""
    def __init__(self, max_size: int = 100):
        self.cache = OrderedDict()
        self.max_size = int(max_size)
        self.lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        with self.lock:
            if key not in self.cache:
                self.misses += 1
                return None
            self.hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]

    def put(self, key, value) -> None:
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()
            self.hits = 0
            self.misses = 0

    def get_stats(self) -> Dict[str, Any]:
        with self.lock:
            total = self.hits + self.misses
            hit_rate = (self.hits / total * 100.0) if total > 0 else 0.0
            return {
                "size": len(self.cache),
                "max_size": self.max_size,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(hit_rate, 2),
            }

    def __len__(self) -> int:
        with self.lock:
            return len(self.cache)


# --- Improved Cache Decorator ---
def cache_result(expiration: int = 3600):
    """Decorator to cache function results with automatic cleanup"""
    cache: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    lock = threading.RLock()

    def cleanup_cache() -> None:
        current_time = time.time()
        expired_keys = [k for k, v in cache.items() if current_time - v["timestamp"] >= expiration]
        for key in expired_keys:
            cache.pop(key, None)

    def decorator(func):
        def wrapper(*args, **kwargs):
            with lock:
                # probabilistic cleanup
                if cache and random.randint(1, 100) == 1:
                    cleanup_cache()

                key_parts = [str(func.__name__)]
                key_parts.extend(str(arg) for arg in args)
                key_parts.extend(f"{k}={str(v)}" for k, v in sorted(kwargs.items()))
                key = tuple(key_parts)

                current_time = time.time()
                if key in cache:
                    if current_time - cache[key]["timestamp"] < expiration:
                        logging.getLogger("BitcoinNewsBot.Cache").debug(f"Cache hit for: {func.__name__}")
                        return cache[key]["result"]
                    cache.pop(key, None)

                result = func(*args, **kwargs)
                cache[key] = {"result": result, "timestamp": current_time}
                return result

        return wrapper

    return decorator


# --- Metrics Collection ---
class BotMetrics:
    """Enhanced metrics collection with thread safety"""
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.news_fetches = 0
            self.articles_processed = 0
            self.telegram_posts = 0
            self.twitter_posts = 0
            self.api_errors = 0
            self.user_questions = 0
            self.duplicate_articles_skipped = 0
            self.ai_processing_failures = 0
            self.start_time = time.time()

    def increment(self, metric_name: str, amount: int = 1) -> None:
        with self.lock:
            if hasattr(self, metric_name):
                setattr(self, metric_name, getattr(self, metric_name) + amount)

    def to_dict(self) -> Dict[str, Any]:
        with self.lock:
            uptime = time.time() - self.start_time
            processed = self.articles_processed
            failures = self.ai_processing_failures
            denom = processed + failures
            return {
                "news_fetches": self.news_fetches,
                "articles_processed": processed,
                "telegram_posts": self.telegram_posts,
                "twitter_posts": self.twitter_posts,
                "api_errors": self.api_errors,
                "user_questions": self.user_questions,
                "duplicate_articles_skipped": self.duplicate_articles_skipped,
                "ai_processing_failures": failures,
                "uptime_seconds": round(uptime, 2),
                "uptime_human": self._format_uptime(uptime),
                "processing_rate_per_hour": round(processed / (uptime / 3600), 2) if uptime > 0 else 0,
                "success_rate": round((processed / denom * 100), 2) if denom > 0 else 0,
            }

    def _format_uptime(self, seconds: float) -> str:
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{days}d {hours}h {minutes}m"


# --- Rate Limiter ---
class RateLimiter:
    """Simple rate limiter for API calls"""
    def __init__(self, calls_per_minute: int = 60):
        self.calls_per_minute = int(calls_per_minute)
        self.calls: List[float] = []
        self.lock = threading.Lock()

    def wait_if_needed(self) -> None:
        with self.lock:
            now = time.time()
            self.calls = [t for t in self.calls if now - t < 60]
            if len(self.calls) >= self.calls_per_minute:
                sleep_time = 60 - (now - self.calls[0])
                if sleep_time > 0:
                    logging.info(f"Rate limit reached, waiting {sleep_time:.2f}s")
                    time.sleep(sleep_time)
                    self.calls = self.calls[1:]
            self.calls.append(time.time())


# --- Main Bot Class ---
class BitcoinNewsBot:
    """A Telegram bot that fetches, processes, and distributes cryptocurrency news."""

    _QUEUE_SENTINEL = object()

    def __init__(self):
        self._setup_init_logger()
        self._init_logger.info("Starting bot initialization...")

        # Load and validate configuration
        self.config = BotConfig.from_env()
        self.config.validate()

        # Setup paths
        self.CONFIG_DIR = os.path.dirname(__file__) if "__file__" in globals() else os.getcwd()
        self.LOG_FILE = os.path.join(self.CONFIG_DIR, "bot.log")

        # Setup logging early (rotating)
        self._setup_logging()
        self.logger = logging.getLogger("BitcoinNewsBot")
        self.logger.info("Logger initialized.")

        # Initialize database
        self.db = DatabaseManager()

        # Initialize services
        self.bot = telebot.TeleBot(self.config.telegram_bot_token, threaded=False)
        self.groq_client = Groq(api_key=self.config.groq_api_key)
        self.session = self._setup_http_session()

        # State management
        self.context_cache = LRUCache(max_size=CONTEXT_CACHE_SIZE)
        self.user_last_request_time: Dict[int, float] = {}

        # Threading
        self.lock = threading.RLock()
        self.shutdown_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="BotWorker")

        # Metrics
        self.metrics = BotMetrics()
        self.start_time = time.time()

        # Rate limiters
        self.groq_rate_limiter = RateLimiter(calls_per_minute=30)
        self.news_rate_limiter = RateLimiter(calls_per_minute=10)

        # API worker setup
        self.api_request_queue: "queue.PriorityQueue[Tuple[int, float, int, Any]]" = queue.PriorityQueue()
        self._queue_seq = 0
        self._queue_seq_lock = threading.Lock()

        self.api_worker_thread = threading.Thread(
            target=self.api_request_worker,
            name="GroqApiWorker",
            daemon=True,
        )

        # Twitter configuration
        self.post_to_twitter = all([
            self.config.twitter_api_key,
            self.config.twitter_api_secret_key,
            self.config.twitter_access_token,
            self.config.twitter_access_token_secret,
            self.config.twitter_bearer_token,
        ])
        if not self.post_to_twitter:
            self.logger.warning("Twitter API credentials incomplete. Tweeting disabled.")

        # Setup handlers and signals
        self.setup_bot_handlers()
        self._setup_signal_handlers()

        self.logger.info("Bot initialization complete.")

    def _setup_init_logger(self) -> None:
        self._init_logger = logging.getLogger("BitcoinNewsBot.Init")
        if not self._init_logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
            handler.setFormatter(formatter)
            self._init_logger.addHandler(handler)
            self._init_logger.setLevel(logging.INFO)

    def _setup_http_session(self) -> requests.Session:
        """Setup HTTP session with connection pooling and retry logic"""
        session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST", "HEAD"),
            raise_on_status=False,
        )

        adapter = requests.adapters.HTTPAdapter(
            pool_connections=30,
            pool_maxsize=30,
            max_retries=retry_strategy,
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _setup_logging(self) -> None:
        log_format = "%(asctime)s - %(levelname)s - %(name)s - %(threadName)s - %(message)s"
        formatter = logging.Formatter(log_format)

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)

        file_handler = RotatingFileHandler(
            filename=self.LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.handlers.clear()
        root_logger.addHandler(stream_handler)
        root_logger.addHandler(file_handler)

        # Suppress noisy libraries
        for lib_name in ["requests", "urllib3", "tweepy", "telebot", "asyncio", "hpack", "werkzeug"]:
            logging.getLogger(lib_name).setLevel(logging.WARNING)

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown_handler)
        signal.signal(signal.SIGTERM, self.shutdown_handler)

    def shutdown_handler(self, sig, frame) -> None:
        if not self.shutdown_event.is_set():
            self.logger.warning(f"Received signal {sig}. Initiating graceful shutdown...")
            self.shutdown_event.set()

    def shutdown(self) -> None:
        self.logger.info("Executing shutdown sequence...")

        if hasattr(self, "bot") and self.bot:
            try:
                self.bot.stop_polling()
                self.logger.info("Telegram polling stopped.")
            except Exception as e:
                self.logger.error(f"Error stopping bot polling: {e}")

        if hasattr(self, "api_worker_thread") and self.api_worker_thread.is_alive():
            self._enqueue_worker_sentinel()
            self.api_worker_thread.join(timeout=5)
            self.logger.info("API worker thread joined.")

        if hasattr(self, "executor"):
            try:
                self.executor.shutdown(wait=True, cancel_futures=False)
            except TypeError:
                self.executor.shutdown(wait=True)
            self.logger.info("Thread pool executor shutdown.")

        if hasattr(self, "session") and self.session:
            self.session.close()
            self.logger.info("HTTP session closed.")

        self.logger.info("Shutdown complete. Exiting.")

    # --- Utility Methods ---

    def get_link_hash(self, link: Optional[str]) -> Optional[str]:
        if not link:
            return None
        return hashlib.sha1(link.encode("utf-8")).hexdigest()[:10]

    def add_context(self, message_id: int, context_data: Dict[str, Any]) -> None:
        self.context_cache.put(message_id, context_data)

    def get_context(self, message_id: int) -> Optional[Dict[str, Any]]:
        return self.context_cache.get(message_id)

    # --- Input Sanitization ---

    def sanitize_user_input(self, text: Any, max_length: int = 500) -> str:
        """Sanitize user input to prevent injection attacks"""
        if not text or not isinstance(text, str):
            return ""
        text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)
        text = text[:max_length]
        text = " ".join(text.split())
        return text

    # --- News Fetching and Processing ---

    @cache_result(expiration=600)
    def fetch_live_news(self, keyword: str = "Bitcoin, Crypto") -> List[Dict[str, Any]]:
        self.logger.info(f"Fetching live news for keyword(s): {keyword}")
        self.metrics.increment("news_fetches")
        self.news_rate_limiter.wait_if_needed()

        all_articles: List[Dict[str, Any]] = []

        if self.config.newsdata_api_key:
            try:
                url = f"https://newsdata.io/api/1/news?apikey={self.config.newsdata_api_key}&q={keyword}&language=en"
                r = self.session.get(url, timeout=self.config.external_api_timeout)
                r.raise_for_status()
                for item in r.json().get("results", []):
                    if item.get("title") and item.get("link"):
                        all_articles.append({
                            "title": item["title"],
                            "link": item["link"],
                            "description": item.get("description") or item.get("content"),
                            "image_url": item.get("image_url"),
                            "pubDate": item.get("pubDate"),
                            "source": "NewsData.io",
                        })
            except Exception as e:
                self.logger.error(f"Failed to fetch from NewsData.io: {e}")
                self.metrics.increment("api_errors")

        if self.config.marketaux_api_key:
            try:
                keywords_lower = [k.strip().lower() for k in keyword.split(",")]
                param = (
                    f"search={keyword}"
                    if any(k not in ["bitcoin", "crypto", "microstrategy", "tesla"] for k in keywords_lower)
                    else "symbols=BTC,ETH"
                )
                url = f"https://api.marketaux.com/v1/news/all?api_token={self.config.marketaux_api_key}&{param}&language=en"
                r = self.session.get(url, timeout=self.config.external_api_timeout)
                r.raise_for_status()
                for item in r.json().get("data", []):
                    if item.get("title") and item.get("url"):
                        all_articles.append({
                            "title": item["title"],
                            "link": item["url"],
                            "description": item.get("description") or item.get("snippet"),
                            "image_url": item.get("image_url"),
                            "pubDate": item.get("published_at"),
                            "source": "MarketAux",
                        })
            except Exception as e:
                self.logger.error(f"Failed to fetch from MarketAux: {e}")
                self.metrics.increment("api_errors")

        # Deduplicate by link
        unique = {}
        for a in all_articles:
            lk = a.get("link")
            if lk:
                unique[lk] = a
        unique_articles = list(unique.values())

        # Sort robustly by parsed timestamp
        sorted_articles = sorted(unique_articles, key=lambda x: parse_pub_date_to_ts(x.get("pubDate")), reverse=True)

        self.logger.info(f"Fetched {len(sorted_articles)} unique articles.")
        return sorted_articles

    def are_titles_similar(self, title1: str, title2: str, threshold: float = 0.7) -> bool:
        if not title1 or not title2:
            return False
        normalize = lambda t: set(re.sub(r"[^\w\s]", "", t.lower()).split())
        words1, words2 = normalize(title1), normalize(title2)
        if not words1 or not words2:
            return False
        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))
        return (intersection / union) >= threshold

    # --- AI Processing ---

    def _next_queue_seq(self) -> int:
        with self._queue_seq_lock:
            self._queue_seq += 1
            return self._queue_seq

    def api_request_worker(self) -> None:
        """Worker thread with priority handling"""
        self.logger.info("Groq API worker thread started.")
        while not self.shutdown_event.is_set():
            try:
                priority, ts, seq, item = self.api_request_queue.get(timeout=1)

                if item is self._QUEUE_SENTINEL:
                    self.api_request_queue.task_done()
                    break

                request_data, callback, request_id = item

                try:
                    self.logger.debug(f"Worker processing {request_id} (priority: {priority})")
                    response = self._call_groq_api_with_retry(*request_data, request_id=request_id)
                    if callback:
                        callback(response)
                except Exception as e:
                    self.logger.error(f"Error processing request {request_id}: {e}")
                    self.metrics.increment("api_errors")
                finally:
                    self.api_request_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                self.logger.critical(f"API worker critical error: {e}", exc_info=True)

        self.logger.info("Groq API worker thread shutting down.")

    def _enqueue_worker_sentinel(self) -> None:
        self.api_request_queue.put((Priority.CRITICAL, time.time(), self._next_queue_seq(), self._QUEUE_SENTINEL))

    def _enqueue_groq_request(
        self,
        messages: List[Dict[str, str]],
        model: str,
        callback,
        temperature: Optional[float] = None,
        priority: Priority = Priority.NORMAL,
    ) -> None:
        req_id = f"groq_{time.time_ns()}"
        seq = self._next_queue_seq()
        self.api_request_queue.put((
            int(priority),
            time.time(),
            seq,
            ((messages, model, temperature), callback, req_id),
        ))

    def _call_groq_api_with_retry(
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: Optional[float] = None,
        request_id: str = "N/A",
        max_retries: int = MAX_RETRY_ATTEMPTS,
    ):
        """Enhanced Groq API call with retry logic and rate limiting"""
        self.groq_rate_limiter.wait_if_needed()

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                completion = self.groq_client.chat.completions.create(
                    messages=messages,
                    model=model,
                    temperature=self.config.llm_temperature if temperature is None else temperature,
                    top_p=self.config.llm_top_p,
                    max_tokens=self.config.llm_max_tokens,
                    extra_body={"service_tier": self.config.groq_service_tier},
                )
                return completion
            except Exception as e:
                last_exc = e
                self.logger.warning(f"Groq req {request_id} failed attempt {attempt + 1}: {e}")

                # fallback model on final attempt if primary failed
                if attempt + 1 == max_retries and model == self.config.groq_primary_model:
                    self.logger.info(f"Trying fallback model: {self.config.groq_fallback_model}")
                    try:
                        completion = self.groq_client.chat.completions.create(
                            messages=messages,
                            model=self.config.groq_fallback_model,
                            temperature=self.config.llm_temperature if temperature is None else temperature,
                            top_p=self.config.llm_top_p,
                            max_tokens=min(self.config.llm_max_tokens, 4096),
                        )
                        return completion
                    except Exception as fallback_e:
                        self.logger.error(f"Fallback model also failed: {fallback_e}")
                        return fallback_e

                if attempt + 1 < max_retries:
                    sleep_s = (2 ** attempt) + random.uniform(0, 0.5)
                    time.sleep(sleep_s)

        return last_exc

    def _process_article_with_ai(self, title: str, description: Optional[str], callback) -> None:
        """Enhanced AI processing with better prompt engineering"""
        prompt = f"""You are an expert news editor for a Hebrew-speaking crypto audience.
Analyze the following English news article and respond in STRICT JSON ONLY.

Article Title: "{html.escape(title or "")}"
Article Description: "{html.escape((description or "")[:800])}"

Return JSON with:
- hebrew_title: Hebrew headline (<=100 chars)
- hebrew_summary: Hebrew summary (2-3 sentences, <=300 chars)
- hashtags: 3-5 Hebrew hashtags strings starting with '#'

Example:
{{
  "hebrew_title": "כותרת לדוגמה",
  "hebrew_summary": "סיכום קצר של הידיעה.",
  "hashtags": ["#ביטקוין", "#קריפטו"]
}}
"""
        messages = [
            {"role": "system", "content": "You translate/summarize crypto news into Hebrew. Output ONLY valid JSON. No extra text."},
            {"role": "user", "content": prompt},
        ]
        self._enqueue_groq_request(messages, self.config.groq_primary_model, callback, temperature=0.2)

    def _extract_json_object(self, text: str) -> Optional[str]:
        """
        Robustly extract first JSON object from a string.
        Handles code fences and extra prose.
        """
        if not text:
            return None
        s = text.strip()

        # Strip code fences if present
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)

        # Find first '{' and last '}' (best effort)
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = s[start:end + 1].strip()
        return candidate

    def _parse_ai_response(self, api_response) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Enhanced JSON parsing with robust error handling"""
        if isinstance(api_response, Exception):
            self.logger.error(f"AI processing failed before parsing: {api_response}")
            return None, None, None

        try:
            content = api_response.choices[0].message.content or ""
            content = content.strip()

            if not content:
                self.logger.warning("AI returned empty response")
                return None, None, None

            if len(content) > 50000:
                self.logger.warning(f"AI response too large: {len(content)} chars")
                return None, None, None

            json_str = self._extract_json_object(content)
            if not json_str:
                self.logger.warning(f"No JSON found in AI response: {content[:200]}")
                return None, None, None

            data = json.loads(json_str)

            title = data.get("hebrew_title")
            summary = data.get("hebrew_summary")
            hashtags_list = data.get("hashtags", [])

            if not isinstance(title, str) or not isinstance(summary, str):
                self.logger.warning("Invalid data types in AI response")
                return None, None, None

            title = title.strip()
            summary = summary.strip()
            if not title or not summary:
                self.logger.warning("Empty title or summary in AI response")
                return None, None, None

            if not isinstance(hashtags_list, list):
                hashtags_list = []

            hashtags = " ".join(
                tag for tag in hashtags_list
                if isinstance(tag, str) and tag.startswith("#") and 1 < len(tag) < 50
            )

            return title, summary, hashtags

        except json.JSONDecodeError as e:
            self.logger.error(f"JSON decode error: {e}. Content: {content[:300]}")
            return None, None, None
        except Exception as e:
            self.logger.error(f"Response parsing error: {e}", exc_info=True)
            return None, None, None

    # --- Distribution and Main Loop ---

    def continuous_news_monitor(self) -> None:
        self.logger.info("News monitor thread started.")
        time.sleep(5)
        cycle_count = 0

        while not self.shutdown_event.is_set():
            try:
                keywords = os.getenv("NEWS_KEYWORDS", DEFAULT_NEWS_KEYWORDS)
                self.run_news_cycle(keyword=keywords)

                # Periodic cleanup every 10 cycles
                cycle_count += 1
                if cycle_count % 10 == 0:
                    deleted = self.db.cleanup_old_sent_articles(days_to_keep=30)
                    if deleted > 0:
                        self.logger.info(f"Cleaned up {deleted} old article hashes")

                    pruned = self.db.cleanup_old_processed_articles(max_rows=MAX_PROCESSED_ARTICLES)
                    if pruned > 0:
                        self.logger.info(f"Pruned {pruned} old processed articles")

            except Exception as e:
                self.logger.critical(f"Unhandled exception in news monitor loop: {e}", exc_info=True)

            self.shutdown_event.wait(self.config.news_check_interval)

        self.logger.info("News monitor thread shutting down.")

    def run_news_cycle(self, keyword: str = "Bitcoin") -> None:
        self.logger.info(f"--- Starting news cycle for: {keyword} ---")
        live_articles = self.fetch_live_news(keyword=keyword)
        if not live_articles:
            return

        new_articles = []
        for a in live_articles:
            lk = a.get("link")
            lh = self.get_link_hash(lk)
            if lk and lh and not self.db.is_article_sent(lh):
                new_articles.append(a)

        if not new_articles:
            self.logger.info("No new articles to process.")
            return

        self.logger.info(f"Found {len(new_articles)} new articles.")
        stats = {"processed": 0, "tweeted": 0, "telegram": 0, "failed_ai": 0, "skipped_dup": 0}

        recent_articles = self.db.get_processed_articles(limit=50)
        recent_titles = {a["title"].lower() for a in recent_articles if a.get("title")}

        for i, article in enumerate(new_articles):
            if self.shutdown_event.is_set():
                break

            self.logger.info(f"Processing {i+1}/{len(new_articles)}: {article.get('title', 'N/A')[:80]}")
            success, status = self._process_and_distribute_article(article, recent_titles)

            if success:
                stats["processed"] += 1
                if status.get("tweeted"):
                    stats["tweeted"] += 1
                if status.get("telegram"):
                    stats["telegram"] += 1
            elif status.get("reason") == "ai_failure":
                stats["failed_ai"] += 1
                self.metrics.increment("ai_processing_failures")
            elif status.get("reason") == "duplicate":
                stats["skipped_dup"] += 1
                self.metrics.increment("duplicate_articles_skipped")

            if i < len(new_articles) - 1:
                time.sleep(self.config.tweet_post_delay)

        self.logger.info(f"--- News cycle finished. Stats: {stats} ---")

    def _process_and_distribute_article(self, article: Dict[str, Any], recent_titles_set: set) -> Tuple[bool, Dict[str, Any]]:
        link = article.get("link")
        link_hash = self.get_link_hash(link)
        title_orig = article.get("title", "No Title") or "No Title"
        if not link_hash:
            return False, {"reason": "invalid_link"}

        ai_event, ai_result = threading.Event(), {}

        def ai_callback(response):
            ai_result["response"] = response
            ai_event.set()

        self._process_article_with_ai(title_orig, article.get("description"), ai_callback)

        if not ai_event.wait(timeout=self.config.groq_api_timeout):
            self.logger.error(f"AI processing timed out for: {title_orig}")
            self.db.add_sent_article(link_hash)
            return False, {"reason": "ai_failure"}

        title_tr, summary, hashtags = self._parse_ai_response(ai_result.get("response"))

        if not all([title_tr, summary]):
            self.logger.error(f"Failed to get valid content from AI for: {title_orig}")
            self.db.add_sent_article(link_hash)
            return False, {"reason": "ai_failure"}

        title_low = title_tr.lower()

        # Duplicates
        if title_low in recent_titles_set:
            self.logger.warning(f"Exact duplicate found: '{title_tr[:50]}...'")
            self.db.add_sent_article(link_hash)
            return False, {"reason": "duplicate"}

        for recent_title in list(recent_titles_set)[:30]:
            if self.are_titles_similar(title_low, recent_title):
                self.logger.warning(f"Similar title found: '{title_tr[:50]}...'")
                self.db.add_sent_article(link_hash)
                return False, {"reason": "duplicate"}

        status = {"tweeted": False, "telegram": False}
        img_url = article.get("image_url")

        if self.post_to_twitter:
            tweet_msg = self._build_tweet_message(title_tr, link, hashtags)
            if self._post_tweet(tweet_msg, img_url):
                status["tweeted"] = True

        if self._post_telegram_message(title_tr, summary, link, hashtags, img_url):
            status["telegram"] = True

        if status["telegram"]:
            article_data = {
                "title": title_tr,
                "original_title": title_orig,
                "summary": summary,
                "hashtags": hashtags,
                "link": link,
                "image_url": img_url,
                "source": article.get("source"),
                "pubDate": article.get("pubDate"),
                "timestamp": now_utc_iso(),
                "posted_twitter": status["tweeted"],
            }

            self.db.add_sent_article(link_hash)
            self.db.add_processed_article(article_data)
            recent_titles_set.add(title_low)

            self.metrics.increment("articles_processed")
            return True, status

        self.logger.error(f"Failed to post to Telegram: {title_orig}")
        return False, {"reason": "telegram_failure"}

    def _validate_image_url(self, url: Optional[str]) -> bool:
        if not url or not isinstance(url, str):
            return False
        try:
            parsed = urlparse(url)
            if not all([parsed.scheme in ["http", "https"], parsed.netloc]):
                return False

            # First try HEAD
            try:
                r = self.session.head(url, timeout=self.config.image_validation_timeout, allow_redirects=True)
                if r.status_code >= 400:
                    raise requests.RequestException(f"HEAD status {r.status_code}")
                content_type = (r.headers.get("Content-Type", "") or "").lower()
                if "image" in content_type:
                    return True
            except Exception:
                # Fallback: GET small range
                headers = {"Range": "bytes=0-1024"}
                r = self.session.get(url, headers=headers, timeout=self.config.image_validation_timeout, stream=True, allow_redirects=True)
                if r.status_code >= 400:
                    return False
                content_type = (r.headers.get("Content-Type", "") or "").lower()
                if "image" in content_type:
                    return True

            self.logger.warning(f"URL is not an image: {url}")
            return False
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"Image URL validation failed: {e}")
            return False

    def _send_telegram_message_safe(self, chat_id, text=None, photo=None, max_attempts: int = 3, **kwargs):
        """Safe Telegram message sending with bounded retry logic"""
        for attempt in range(max_attempts):
            try:
                if photo:
                    return self.bot.send_photo(chat_id, photo, **kwargs)
                return self.bot.send_message(chat_id, text, **kwargs)
            except telebot.apihelper.ApiTelegramException as e:
                if e.error_code == 429:
                    retry_after = safe_int(e.result_json.get("parameters", {}).get("retry_after", 30), 30)
                    self.logger.warning(f"Telegram rate limit hit. Waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue
                if e.error_code == 400:
                    self.logger.error(f"Bad request to Telegram: {getattr(e, 'description', str(e))}")
                    return None
                if e.error_code in [401, 403]:
                    self.logger.critical(f"Telegram auth error: {getattr(e, 'description', str(e))}")
                    return None
                self.logger.error(f"Telegram API error: {e}")
                time.sleep(2 * (attempt + 1))
            except Exception as e:
                self.logger.error(f"Unexpected Telegram error: {e}", exc_info=True)
                self.metrics.increment("api_errors")
                time.sleep(2 * (attempt + 1))
        return None

    def _post_telegram_message(self, title: str, summary: str, link: str, hashtags: Optional[str], image_url: Optional[str]) -> bool:
        feedback_markup = self._create_feedback_markup(link)
        message_html = self._format_telegram_message(title, summary, link, hashtags)

        try:
            valid_image = self._validate_image_url(image_url)
            if valid_image:
                sent_msg = self._send_telegram_message_safe(
                    self.config.telegram_channel_id,
                    photo=image_url,
                    caption=message_html,
                    parse_mode="HTML",
                    reply_markup=feedback_markup,
                )
            else:
                sent_msg = self._send_telegram_message_safe(
                    self.config.telegram_channel_id,
                    text=message_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=feedback_markup,
                )

            if sent_msg:
                self.logger.info(f"Posted to Telegram: {title[:50]}...")
                self.add_context(sent_msg.message_id, {"link": link, "title": title, "summary": summary})
                self.metrics.increment("telegram_posts")
                return True
            return False

        except Exception as e:
            self.logger.error(f"Telegram send failed: {e}", exc_info=True)
            return False

    def _build_tweet_message(self, title: str, link: str, hashtags: Optional[str]) -> str:
        base_text = f"{title}\n\nקראו עוד: {link}"
        # Twitter counts URLs as 23 characters
        available_chars = 280 - (len(base_text) - len(link) + 23)
        if hashtags and available_chars > len(hashtags) + 2:
            return f"{base_text}\n\n{hashtags}"
        return base_text

    def _post_tweet(self, message: str, image_url: Optional[str] = None) -> bool:
        if not self.post_to_twitter:
            return False
        try:
            client = tweepy.Client(
                bearer_token=self.config.twitter_bearer_token,
                consumer_key=self.config.twitter_api_key,
                consumer_secret=self.config.twitter_api_secret_key,
                access_token=self.config.twitter_access_token,
                access_token_secret=self.config.twitter_access_token_secret,
                wait_on_rate_limit=False,
            )

            media_id = None
            if image_url and self._validate_image_url(image_url):
                try:
                    response = self.session.get(image_url, stream=True, timeout=self.config.external_api_timeout)
                    response.raise_for_status()
                    auth_v1 = tweepy.OAuth1UserHandler(
                        self.config.twitter_api_key,
                        self.config.twitter_api_secret_key,
                        self.config.twitter_access_token,
                        self.config.twitter_access_token_secret,
                    )
                    api_v1 = tweepy.API(auth_v1)
                    media = api_v1.media_upload(filename="image.jpg", file=io.BytesIO(response.content))
                    media_id = media.media_id_string
                except Exception as e:
                    self.logger.error(f"Twitter media upload failed: {e}")

            resp = client.create_tweet(text=message, media_ids=[media_id] if media_id else None)
            self.logger.info(f"Posted Tweet: https://twitter.com/user/status/{resp.data['id']}")
            self.metrics.increment("twitter_posts")
            return True

        except tweepy.errors.TooManyRequests:
            self.logger.warning("Twitter rate limit hit. Skipping tweet.")
            return False
        except Exception as e:
            self.logger.error(f"Error posting tweet: {e}")
            self.metrics.increment("api_errors")
            return False

    # --- Telegram Handlers & Interactive Features ---

    def setup_bot_handlers(self) -> None:
        self._set_bot_commands()
        self.bot.message_handler(commands=["start", "help"])(self.start_help_command)
        self.bot.message_handler(commands=["new"])(self.new_command)
        self.bot.message_handler(commands=["search"])(self.search_command)
        self.bot.message_handler(commands=["price"])(self.price_command)
        self.bot.message_handler(commands=["market"])(self.market_command)
        self.bot.message_handler(commands=["gas"])(self.gas_command)
        self.bot.message_handler(commands=["history"])(self.history_command)
        self.bot.message_handler(commands=["poll"])(self.poll_command)
        self.bot.message_handler(commands=["stats"])(self.stats_command)
        self.bot.callback_query_handler(func=lambda call: call.data.startswith("feedback:"))(self.feedback_callback)
        self.bot.message_handler(func=lambda msg: True, content_types=["text"])(self.default_message_handler)

    def _set_bot_commands(self) -> None:
        commands = {
            "start": "התחלה ועזרה",
            "new": "הצג את הידיעה האחרונה",
            "price": "מחיר עדכני של ביטקוין",
            "market": "נתוני שוק קריפטו כלליים",
            "gas": "עמלות גז באית'ריום",
            "search": "חיפוש בחדשות שפורסמו",
            "history": "מחיר היסטורי של ביטקוין",
            "poll": "צור סקר שוק (קבוצות)",
            "stats": "סטטיסטיקות הבוט",
            "help": "הצג עזרה",
        }
        try:
            self.bot.set_my_commands([BotCommand(cmd, desc) for cmd, desc in commands.items()])
            self.logger.info("Telegram bot commands set.")
        except Exception as e:
            self.logger.error(f"Failed to set bot commands: {e}")

    def _format_telegram_message(self, title: str, summary: str, link: str, hashtags: Optional[str]) -> str:
        t_html = html.escape(title)
        s_html = html.escape(summary)
        h_html = html.escape(hashtags or "")
        link_escaped = html.escape(link, quote=True)
        l_html = f"<a href=\"{link_escaped}\">קראו את הכתבה המלאה</a>"

        channel_username = self.config.telegram_channel_id.lstrip("@")
        p_html = f"✈️ <a href=\"https://t.me/{html.escape(channel_username)}\">הצטרפו לערוץ לעוד עדכונים</a>"

        return f"<b>{t_html}</b>\n\n{s_html}\n\n{h_html}\n\n{l_html}\n{p_html}"

    def _create_feedback_markup(self, article_link: str) -> Optional[InlineKeyboardMarkup]:
        link_hash = self.get_link_hash(article_link)
        if not link_hash:
            return None
        markup = InlineKeyboardMarkup()
        useful = InlineKeyboardButton(text="👍 שימושי", callback_data=f"feedback:{link_hash}:useful")
        irrelevant = InlineKeyboardButton(text="👎 לא רלוונטי", callback_data=f"feedback:{link_hash}:irrelevant")
        markup.row(useful, irrelevant)
        return markup

    def start_help_command(self, message) -> None:
        self.logger.info(f"User {message.from_user.id} used /start or /help.")
        commands_help = self.bot.get_my_commands()
        help_text = "👋 **ברוכים הבאים לבוט חדשות הקריפטו!**\n\n**פקודות זמינות:**\n"
        for command in commands_help:
            help_text += f"/{command.command} - {command.description}\n"

        channel_username = self.config.telegram_channel_id.lstrip("@")
        help_text += (
            "\nתוכלו גם לשאול אותי כל שאלה, או להשיב להודעת חדשות כדי לשאול עליה."
            f"\n\n✈️ **[הצטרפו לערוץ](https://t.me/{channel_username})**"
        )

        self.bot.reply_to(message, help_text, parse_mode="Markdown", disable_web_page_preview=True)

    def new_command(self, message) -> None:
        self.logger.info(f"User {message.from_user.id} requested /new.")
        self.bot.send_chat_action(message.chat.id, "typing")

        articles = self.db.get_processed_articles(limit=10)
        if not articles:
            self.bot.reply_to(message, "אין כרגע חדשות זמינות.")
            return

        user_id = message.from_user.id
        stats = self.db.get_user_stats(user_id)
        idx = int(stats.get("article_index", 0)) if stats else 0

        article = articles[idx % len(articles)]
        self.db.update_user_stats(user_id, article_index=(idx + 1) % 10)

        title = article.get("title")
        summary = article.get("summary")
        link = article.get("link")
        img_url = article.get("image_url")
        hashtags = article.get("hashtags")

        markup = self._create_feedback_markup(link)
        msg_html = self._format_telegram_message(title, summary, link, hashtags)

        try:
            if self._validate_image_url(img_url):
                sent_msg = self.bot.send_photo(
                    message.chat.id,
                    img_url,
                    caption=msg_html,
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                    reply_markup=markup,
                )
            else:
                sent_msg = self.bot.reply_to(
                    message,
                    msg_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )

            if sent_msg:
                self.add_context(sent_msg.message_id, {"link": link, "title": title, "summary": summary})
        except Exception as e:
            self.logger.error(f"Error sending /new article: {e}")
            self.bot.reply_to(message, "שגיאה בשליחת הידיעה.")

    def search_command(self, message) -> None:
        self.logger.info(f"User {message.from_user.id} used /search.")
        query = message.text.split(maxsplit=1)

        if len(query) < 2 or not query[1].strip():
            self.bot.reply_to(message, "שימוש: `/search <מילת חיפוש>`", parse_mode="Markdown")
            return

        term = self.sanitize_user_input(query[1].strip().lower())
        if not term:
            self.bot.reply_to(message, "קלט לא תקין.")
            return

        self.bot.send_chat_action(message.chat.id, "typing")
        results = self.db.search_articles(term, limit=MAX_SEARCH_RESULTS)

        if not results:
            self.bot.reply_to(message, f"לא נמצאו תוצאות עבור '{html.escape(term)}'.", parse_mode="HTML")
            return

        resp = f"🔎 <b>נמצאו {len(results)} תוצאות עבור '{html.escape(term)}':</b>\n\n"
        for i, a in enumerate(results):
            resp += f"{i + 1}. <a href=\"{html.escape(a.get('link', '#'), quote=True)}\">{html.escape(a.get('title', 'N/A'))}</a>\n"

        self.bot.reply_to(message, resp, parse_mode="HTML", disable_web_page_preview=True)

    def price_command(self, message) -> None:
        self.logger.info(f"User {message.from_user.id} requested /price.")
        self.bot.send_chat_action(message.chat.id, "typing")
        result = self.get_bitcoin_price()
        self.bot.reply_to(message, result["html"], parse_mode="HTML")

    def market_command(self, message) -> None:
        self.logger.info(f"User {message.from_user.id} requested /market.")
        self.bot.send_chat_action(message.chat.id, "typing")
        msg = self.get_market_data()
        self.bot.reply_to(message, msg, parse_mode="HTML")

    def gas_command(self, message) -> None:
        if not self.config.etherscan_api_key:
            self.bot.reply_to(message, "מצטערים, פונקציה זו אינה זמינה כעת.")
            return
        self.logger.info(f"User {message.from_user.id} requested /gas.")
        self.bot.send_chat_action(message.chat.id, "typing")
        msg = self.get_eth_gas_fees()
        self.bot.reply_to(message, msg, parse_mode="HTML")

    def history_command(self, message) -> None:
        self.logger.info(f"User {message.from_user.id} requested /history.")
        parts = message.text.split(maxsplit=1)
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d") if len(parts) < 2 else parts[1].strip()
        self.bot.send_chat_action(message.chat.id, "typing")
        msg = self.get_historical_btc_price(date_str)
        self.bot.reply_to(message, msg, parse_mode="HTML")

    def poll_command(self, message) -> None:
        if message.chat.type == "private":
            self.bot.reply_to(message, "ניתן ליצור סקרים בקבוצות בלבד.")
            return

        self.logger.info(f"User {message.from_user.id} requested /poll in chat {message.chat.id}.")
        q = "מה כיוון השוק לדעתכם?"
        opts = ["🚀 שורי מאוד", "🐂 שורי", "↔️ ניטרלי", "🐻 דובי", "📉 דובי מאוד"]
        self.bot.send_poll(
            message.chat.id,
            question=q,
            options=opts,
            is_anonymous=False,
            reply_to_message_id=message.message_id,
        )

    def stats_command(self, message) -> None:
        self.logger.info(f"User {message.from_user.id} requested /stats.")
        self.bot.send_chat_action(message.chat.id, "typing")

        metrics = self.metrics.to_dict()
        cache_stats = self.context_cache.get_stats()
        db_count = self.db.get_sent_articles_count()

        stats_msg = f"""📊 <b>סטטיסטיקות הבוט</b>

⏱ <b>זמן פעילות:</b> {metrics['uptime_human']}

📰 <b>ידיעות:</b>
• עובדו: {metrics['articles_processed']}
• נשלחו לטלגרם: {metrics['telegram_posts']}
• נשלחו לטוויטר: {metrics['twitter_posts']}
• דוכפלו: {metrics['duplicate_articles_skipped']}

🤖 <b>AI:</b>
• כשלונות: {metrics['ai_processing_failures']}
• שאלות משתמשים: {metrics['user_questions']}
• שיעור הצלחה: {metrics['success_rate']}%

💾 <b>מטמון:</b>
• גודל: {cache_stats['size']}/{cache_stats['max_size']}
• שיעור פגיעה: {cache_stats['hit_rate']}%

⚠️ <b>שגיאות API:</b> {metrics['api_errors']}

🧾 <b>DB:</b>
• Hashes שנשלחו: {db_count}

📈 <b>קצב עיבוד:</b> {metrics['processing_rate_per_hour']} ידיעות/שעה
"""
        self.bot.reply_to(message, stats_msg, parse_mode="HTML")

    def feedback_callback(self, call) -> None:
        self.logger.info(f"Feedback from user {call.from_user.id}: {call.data}")
        try:
            _, link_hash, rating = call.data.split(":")
            title_hint = (call.message.caption or call.message.text or "").split("\n", 1)[0].strip()[:100]
            self.db.log_feedback(call.from_user.id, call.message.chat.id, link_hash, rating, title_hint)
            self.bot.answer_callback_query(call.id, text="תודה על המשוב!")
            try:
                self.bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
        except Exception as e:
            self.logger.error(f"Error processing feedback callback: {e}")
            self.bot.answer_callback_query(call.id, "שגיאה בעיבוד המשוב.")

    def default_message_handler(self, message) -> None:
        if message.text.startswith("/"):
            return

        self.logger.info(f"Received text message from user {message.from_user.id}.")
        self.bot.send_chat_action(message.chat.id, "typing")

        self.db.update_user_stats(message.from_user.id, increment_questions=True)

        ctx = self._get_context_from_reply(message)
        text_l = (message.text or "").lower().strip()

        if not ctx:
            if any(kw in text_l for kw in ["מחיר", "price", "btc"]):
                return self.price_command(message)
            if any(kw in text_l for kw in ["שוק", "market"]):
                return self.market_command(message)

        answer = self.answer_question(message.text, context=ctx)
        self.bot.reply_to(message, answer)

    def _get_context_from_reply(self, message) -> Optional[Dict[str, Any]]:
        try:
            if not (message.reply_to_message and message.reply_to_message.from_user.id == self.bot.get_me().id):
                return None
        except Exception:
            return None

        replied_id = message.reply_to_message.message_id
        ctx = self.get_context(replied_id)
        if ctx:
            return ctx

        target_msg = message.reply_to_message
        link = None
        entities = target_msg.caption_entities or target_msg.entities

        if entities:
            for entity in entities:
                if entity.type == "text_link" and entity.url:
                    link = entity.url
                    break

        if link:
            articles = self.db.search_articles(link, limit=1)
            if articles:
                article = articles[0]
                ctx = {"link": link, "title": article.get("title"), "summary": article.get("summary")}
                self.add_context(replied_id, ctx)
                return ctx

        return None

    # --- Data Fetching Helpers for Commands ---

    def answer_question(self, question: str, context: Optional[Dict[str, Any]] = None) -> str:
        q = self.sanitize_user_input(question, max_length=800)
        if not q:
            return "קלט לא תקין."

        log_prefix = "[Context Q&A]" if context else "[General Q&A]"
        self.logger.info(f"{log_prefix} Q: {q[:80]}...")
        self.metrics.increment("user_questions")

        event, res_cont = threading.Event(), {}

        def callback(api_response):
            res_cont["result"] = api_response
            event.set()

        if context:
            ctx_text = (
                f"Context:\nTitle: {html.escape(context.get('title', 'N/A'))}\n"
                f"Summary: {html.escape(context.get('summary', 'N/A'))}\n"
                f"Link: {html.escape(context.get('link', ''))}"
            )
            prompt = f"{ctx_text}\n\nUser Question: \"{html.escape(q)}\"\n\nAnswer in Hebrew based on the context."
            system_prompt = "You answer questions in Hebrew about a specific news article provided in context. Be concise and accurate."
        else:
            prompt = f'User Question: "{html.escape(q)}"\nAnswer in Hebrew briefly. Focus on crypto/finance.'
            system_prompt = "You are a helpful Hebrew AI assistant for crypto/finance questions. Be concise and accurate."

        self._enqueue_groq_request(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            self.config.groq_primary_model,
            callback,
            temperature=0.2,
            priority=Priority.HIGH,
        )

        if not event.wait(timeout=self.config.question_api_timeout):
            return "חריגה בזמן עיבוד, נסו שוב מאוחר יותר."

        api_res = res_cont.get("result")
        if isinstance(api_res, Exception):
            return "שגיאה בעיבוד הבקשה מול שירות ה-AI."

        try:
            content = (api_res.choices[0].message.content or "").strip()
            return content or "שירות ה-AI לא החזיר תשובה."
        except Exception as e:
            self.logger.error(f"{log_prefix} Error parsing AI response: {e}")
            return "שגיאה בעיבוד תשובת ה-AI."

    @cache_result(expiration=300)
    def get_bitcoin_price(self) -> Dict[str, Any]:
        URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd,ils"
        try:
            r = self.session.get(URL, timeout=self.config.external_api_timeout)
            r.raise_for_status()
            d = r.json()["bitcoin"]
            return {
                "html": f"💰 <b>מחיר BTC:</b>\n🇺🇸 ${d['usd']:,.2f}\n🇮🇱 ₪{d['ils']:,.2f}",
                "data": d,
            }
        except Exception as e:
            self.logger.error(f"BTC price fetch error: {e}")
            self.metrics.increment("api_errors")
            return {"html": "שגיאה בקבלת מחיר BTC.", "data": None}

    @cache_result(expiration=300)
    def get_market_data(self) -> str:
        URL = "https://api.coingecko.com/api/v3/global"
        try:
            r = self.session.get(URL, timeout=self.config.external_api_timeout)
            r.raise_for_status()
            d = r.json()["data"]
            mc = d["total_market_cap"]["usd"]
            vol = d["total_volume"]["usd"]
            mc_chg = d["market_cap_change_percentage_24h_usd"]
            btc_dom = d["market_cap_percentage"]["btc"]
            sign = "📈" if mc_chg >= 0 else "📉"

            return (
                "<b>נתוני שוק קריפטו:</b>\n\n"
                f"<b>שווי שוק:</b> ${mc:,.0f}\n"
                f"📊 <b>מחזור (24ש):</b> ${vol:,.0f}\n"
                f"{sign} <b>שינוי (24ש):</b> {mc_chg:.2f}%\n"
                f"<b>דומיננטיות BTC:</b> {btc_dom:.2f}%"
            )
        except Exception as e:
            self.logger.error(f"Market data fetch error: {e}")
            self.metrics.increment("api_errors")
            return "שגיאה בקבלת נתוני שוק."

    @cache_result(expiration=120)
    def get_eth_gas_fees(self) -> str:
        URL = f"https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={self.config.etherscan_api_key}"
        try:
            r = self.session.get(URL, timeout=self.config.external_api_timeout)
            r.raise_for_status()
            res = r.json()["result"]
            safe = int(float(res["SafeGasPrice"]))
            prop = int(float(res["ProposeGasPrice"]))
            fast = int(float(res["FastGasPrice"]))
            base = float(res.get("suggestBaseFee", 0.0))

            return (
                "⛽️ <b>עמלות גז ETH (Gwei):</b>\n\n"
                f"🌐 <b>איטי:</b> {safe}\n"
                f"🚶 <b>רגיל:</b> {prop}\n"
                f"🚀 <b>מהיר:</b> {fast}\n\n"
                f"🔥 <b>עמלת בסיס:</b> {base:.2f}"
            )
        except Exception as e:
            self.logger.error(f"Gas fetch error: {e}")
            self.metrics.increment("api_errors")
            return "שגיאה בקבלת נתוני גז."

    @cache_result(expiration=3600)
    def get_historical_btc_price(self, date_str: str) -> str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
            if target_date.date() > datetime.now().date():
                return "תאריך עתידי אינו נתמך."
        except ValueError:
            return "פורמט תאריך שגוי. יש להשתמש בפורמט: YYYY-MM-DD."

        URL = f"https://api.coingecko.com/api/v3/coins/bitcoin/history?date={target_date.strftime('%d-%m-%Y')}"
        try:
            r = self.session.get(URL, timeout=self.config.external_api_timeout)
            r.raise_for_status()
            price = r.json()["market_data"]["current_price"]["usd"]
            return f"📈 מחיר BTC בתאריך {html.escape(date_str)}: <b>${price:,.2f}</b>"
        except Exception as e:
            self.logger.error(f"Historical price error for {date_str}: {e}")
            self.metrics.increment("api_errors")
            return f"שגיאה בקבלת מחיר היסטורי עבור {html.escape(date_str)}."

    # --- Main Entry Point ---

    def send_startup_message(self) -> None:
        msg = (
            "✅ Bot is online and operational.\n"
            f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"Model: {self.config.groq_primary_model}"
        )
        try:
            self.bot.send_message(self.config.telegram_channel_id, msg)
        except Exception as e:
            self.logger.error(f"Failed to send startup message: {e}")

    def start(self) -> None:
        self.logger.info("Starting bot services...")
        self.api_worker_thread.start()
        self.send_startup_message()

        news_thread = threading.Thread(
            target=self.continuous_news_monitor,
            name="NewsMonitor",
            daemon=True,
        )
        news_thread.start()

        self.logger.info("Starting Telegram polling...")
        try:
            self.bot.infinity_polling(
                logger_level=logging.WARNING,
                timeout=30,
                long_polling_timeout=60,
                skip_pending=True,
            )
        except Exception as e:
            if not self.shutdown_event.is_set():
                self.logger.critical(f"Infinity polling exited unexpectedly: {e}", exc_info=True)

        self.logger.info("Main thread waiting for shutdown...")


# --- Flask App for Health Checks and API ---
app = Flask(__name__)
bot_instance: Optional[BitcoinNewsBot] = None


def _require_admin_token() -> bool:
    expected = os.getenv(ADMIN_TOKEN_ENV, "")
    if not expected:
        # If no token configured, allow local ops (but log warning)
        return True

    provided = request.headers.get("X-Admin-Token") or request.args.get("token") or ""
    return provided == expected


@app.route("/health")
def health_check():
    """Enhanced health check with detailed metrics"""
    if not bot_instance:
        return jsonify({"status": "error", "message": "Bot not initialized"}), 503

    status = "ok"
    details = {
        "timestamp": now_utc_iso(),
        "uptime_seconds": round(time.time() - bot_instance.start_time, 2),
    }

    # Check Telegram connectivity
    try:
        me = bot_instance.bot.get_me()
        details["telegram"] = {"status": "ok", "username": me.username}
    except Exception as e:
        status = "degraded"
        details["telegram"] = {"status": "error", "message": str(e)}

    # Check Groq queue
    details["groq_queue_size"] = bot_instance.api_request_queue.qsize()
    if details["groq_queue_size"] > 50:
        status = "degraded"
        details["groq_warning"] = "Queue size is high"

    # Memory usage
    try:
        import psutil
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
        details["memory_mb"] = round(memory_mb, 2)
        if memory_mb > 500:
            details["memory_warning"] = "High memory usage"
    except ImportError:
        details["memory_mb"] = "psutil not installed"
    except Exception as e:
        details["memory_error"] = str(e)

    # Thread health
    details["threads"] = {
        t.name: {"alive": t.is_alive(), "daemon": t.daemon}
        for t in threading.enumerate()
    }

    details["metrics"] = bot_instance.metrics.to_dict()
    details["cache_stats"] = bot_instance.context_cache.get_stats()
    details["db_sent_count"] = bot_instance.db.get_sent_articles_count()
    details["config"] = {
        "model": bot_instance.config.groq_primary_model,
        "service_tier": bot_instance.config.groq_service_tier,
        "temperature": bot_instance.config.llm_temperature,
        "twitter_enabled": bot_instance.post_to_twitter,
    }

    return jsonify({"status": status, "details": details})


@app.route("/metrics")
def metrics_endpoint():
    if bot_instance:
        return jsonify(bot_instance.metrics.to_dict())
    return jsonify({"error": "Bot not initialized"}), 503


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    if not _require_admin_token():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    if bot_instance:
        bot_instance.context_cache.clear()
        return jsonify({"status": "success", "message": "Cache cleared"})
    return jsonify({"error": "Bot not initialized"}), 503


def main():
    global bot_instance

    if not app.debug:
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

    main_thread = threading.main_thread()

    try:
        app.logger.info("--- Initializing BitcoinNewsBot ---")
        bot_instance = BitcoinNewsBot()

        flask_port = int(os.getenv("PORT", 5001))
        flask_thread = threading.Thread(
            target=lambda: app.run(host="0.0.0.0", port=flask_port, threaded=True),
            name="FlaskAPIServer",
            daemon=True,
        )
        flask_thread.start()
        app.logger.info(f"Flask API server started on http://0.0.0.0:{flask_port}")

        bot_instance.start()

        while main_thread.is_alive():
            if bot_instance.shutdown_event.is_set():
                break
            time.sleep(1)

    except (ValueError, AttributeError) as e:
        app.logger.critical(f"Configuration or Code Error: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        app.logger.critical(f"Fatal startup error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if bot_instance:
            bot_instance.shutdown()


if __name__ == "__main__":
    main()
