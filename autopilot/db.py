"""SQLite persistence for the autopilot service (single-writer, WAL)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  brief TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  daily_quota INTEGER NOT NULL DEFAULT 10,
  publisher_id TEXT NOT NULL DEFAULT '',
  extra_domains TEXT NOT NULL DEFAULT '',
  tone TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_knowledge (
  project_id INTEGER PRIMARY KEY,
  knowledge_json TEXT NOT NULL DEFAULT '{}',
  site_pages_json TEXT NOT NULL DEFAULT '[]',
  mined_titles_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  format TEXT NOT NULL DEFAULT '',
  pain TEXT NOT NULL DEFAULT '',
  promise TEXT NOT NULL DEFAULT '',
  keywords_json TEXT NOT NULL DEFAULT '[]',
  outline_json TEXT NOT NULL DEFAULT '[]',
  cta_angle TEXT NOT NULL DEFAULT '',
  source_title TEXT NOT NULL DEFAULT '',
  source_link TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',
  score REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_topics_project_status ON topics(project_id, status, score DESC);

CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  topic_id INTEGER,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  tags_json TEXT NOT NULL DEFAULT '[]',
  markdown TEXT NOT NULL,
  chars INTEGER NOT NULL DEFAULT 0,
  quality_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'ready',
  cover_path TEXT NOT NULL DEFAULT '',
  images_json TEXT NOT NULL DEFAULT '[]',
  dzen_publication_id TEXT NOT NULL DEFAULT '',
  dzen_url TEXT NOT NULL DEFAULT '',
  publish_mode TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  published_at TEXT,
  claimed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status, created_at);
CREATE INDEX IF NOT EXISTS idx_articles_project ON articles(project_id, status);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  project_id INTEGER,
  status TEXT NOT NULL,
  article_id INTEGER,
  note TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_kind_time ON runs(kind, finished_at);

CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);

CREATE TABLE IF NOT EXISTS metrics (
  article_id INTEGER PRIMARY KEY,
  views INTEGER NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  shares INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_stats (
  day TEXT NOT NULL,
  publisher_id TEXT NOT NULL,
  subscribers INTEGER,
  views INTEGER NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(day, publisher_id)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
  day TEXT PRIMARY KEY,
  sent_at TEXT NOT NULL,
  telegram_message_id INTEGER,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  kind TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin sqlite3 wrapper. All public methods are thread-safe (one lock)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=60, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS doesn't add columns to an existing table —
        add any columns introduced after the initial schema by hand."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(topics)").fetchall()}
        for name, ddl in (("source_title", "TEXT NOT NULL DEFAULT ''"), ("source_link", "TEXT NOT NULL DEFAULT ''")):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE topics ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------------ core
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self.tx() as c:
            cur = c.execute(sql, tuple(params))
            return int(cur.lastrowid or cur.rowcount)

    # -------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), utcnow()),
        )

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_setting(key, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.set_setting(key, json.dumps(value, ensure_ascii=False))

    # ---------------------------------------------------------------- events
    def log_event(self, message: str, *, kind: str = "", level: str = "info") -> None:
        self.execute(
            "INSERT INTO events(ts,level,kind,message) VALUES(?,?,?,?)",
            (utcnow(), level, kind, message[:2000]),
        )

    def recent_events(self, limit: int = 15) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # -------------------------------------------------------------- projects
    def upsert_project(self, slug: str, name: str, url: str, brief: str, *,
                       daily_quota: int = 10, status: str = "active",
                       extra_domains: str = "", tone: str = "") -> dict[str, Any]:
        now = utcnow()
        self.execute(
            """
            INSERT INTO projects(slug,name,url,brief,status,daily_quota,extra_domains,tone,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET
              name=excluded.name, url=excluded.url, brief=excluded.brief,
              daily_quota=excluded.daily_quota, extra_domains=excluded.extra_domains,
              tone=excluded.tone, updated_at=excluded.updated_at
            """,
            (slug, name, url, brief, status, daily_quota, extra_domains, tone, now, now),
        )
        return self.project(slug)  # type: ignore[return-value]

    def project(self, slug_or_id: str | int) -> dict[str, Any] | None:
        if isinstance(slug_or_id, int) or str(slug_or_id).isdigit():
            return self.one("SELECT * FROM projects WHERE id=?", (int(slug_or_id),))
        return self.one("SELECT * FROM projects WHERE slug=?", (str(slug_or_id),))

    def projects(self, only_active: bool = False) -> list[dict[str, Any]]:
        if only_active:
            return self.query("SELECT * FROM projects WHERE status='active' ORDER BY id")
        return self.query("SELECT * FROM projects ORDER BY id")

    def update_project(self, project_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"name", "url", "brief", "status", "daily_quota", "publisher_id", "extra_domains", "tone"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols) + ", updated_at=?"
        self.execute(f"UPDATE projects SET {sets} WHERE id=?", [fields[k] for k in cols] + [utcnow(), project_id])

    # ------------------------------------------------------------- knowledge
    def knowledge(self, project_id: int) -> dict[str, Any]:
        row = self.one("SELECT * FROM project_knowledge WHERE project_id=?", (project_id,))
        if not row:
            return {}
        try:
            data = json.loads(row["knowledge_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        data["_updated_at"] = row["updated_at"]
        return data

    def save_knowledge(self, project_id: int, knowledge: dict[str, Any], site_pages: list[dict[str, Any]],
                       mined_titles: list[str]) -> None:
        self.execute(
            """
            INSERT INTO project_knowledge(project_id,knowledge_json,site_pages_json,mined_titles_json,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
              knowledge_json=excluded.knowledge_json, site_pages_json=excluded.site_pages_json,
              mined_titles_json=excluded.mined_titles_json, updated_at=excluded.updated_at
            """,
            (project_id, json.dumps(knowledge, ensure_ascii=False), json.dumps(site_pages, ensure_ascii=False),
             json.dumps(mined_titles, ensure_ascii=False), utcnow()),
        )

    def site_pages(self, project_id: int) -> list[dict[str, Any]]:
        row = self.one("SELECT site_pages_json FROM project_knowledge WHERE project_id=?", (project_id,))
        if not row:
            return []
        try:
            return list(json.loads(row["site_pages_json"] or "[]"))
        except json.JSONDecodeError:
            return []

    def mined_titles(self, project_id: int) -> list[str]:
        row = self.one("SELECT mined_titles_json FROM project_knowledge WHERE project_id=?", (project_id,))
        if not row:
            return []
        try:
            return list(json.loads(row["mined_titles_json"] or "[]"))
        except json.JSONDecodeError:
            return []

    # ---------------------------------------------------------------- topics
    def add_topics(self, project_id: int, topics: list[dict[str, Any]]) -> int:
        now = utcnow()
        added = 0
        with self.tx() as c:
            for t in topics:
                c.execute(
                    """
                    INSERT INTO topics(project_id,title,format,pain,promise,keywords_json,outline_json,cta_angle,
                                       source_title,source_link,status,score,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'new',?,?)
                    """,
                    (project_id, t["title"], t.get("format", ""), t.get("pain", ""), t.get("promise", ""),
                     json.dumps(t.get("keywords", []), ensure_ascii=False),
                     json.dumps(t.get("outline", []), ensure_ascii=False),
                     t.get("cta_angle", ""), t.get("source_title", ""), t.get("source_link", ""),
                     float(t.get("score", 0) or 0), now),
                )
                added += 1
        return added

    def topic_titles(self, project_id: int, limit: int = 500) -> list[str]:
        rows = self.query(
            "SELECT title FROM topics WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit)
        )
        return [r["title"] for r in rows]

    def count_topics(self, project_id: int, status: str = "new") -> int:
        row = self.one("SELECT COUNT(*) n FROM topics WHERE project_id=? AND status=?", (project_id, status))
        return int(row["n"]) if row else 0

    def claim_topic(self, project_id: int) -> dict[str, Any] | None:
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM topics WHERE project_id=? AND status='new' ORDER BY score DESC, id ASC LIMIT 1",
                (project_id,),
            ).fetchone()
            if not row:
                return None
            c.execute("UPDATE topics SET status='in_progress' WHERE id=?", (row["id"],))
            return dict(row)

    def finish_topic(self, topic_id: int, status: str) -> None:
        self.execute("UPDATE topics SET status=?, used_at=? WHERE id=?", (status, utcnow(), topic_id))

    # -------------------------------------------------------------- articles
    def add_article(self, project_id: int, topic_id: int | None, *, title: str, description: str,
                    tags: list[str], markdown: str, quality: dict[str, Any], cover_path: str,
                    images: list[dict[str, Any]], status: str = "ready") -> int:
        now = utcnow()
        return self.execute(
            """
            INSERT INTO articles(project_id,topic_id,title,description,tags_json,markdown,chars,quality_json,status,
                                 cover_path,images_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (project_id, topic_id, title, description, json.dumps(tags, ensure_ascii=False), markdown,
             len(markdown), json.dumps(quality, ensure_ascii=False), status, cover_path,
             json.dumps(images, ensure_ascii=False), now, now),
        )

    def article(self, article_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM articles WHERE id=?", (article_id,))

    def count_articles(self, project_id: int, status: str) -> int:
        row = self.one("SELECT COUNT(*) n FROM articles WHERE project_id=? AND status=?", (project_id, status))
        return int(row["n"]) if row else 0

    def ready_articles(self, project_id: int | None = None) -> list[dict[str, Any]]:
        if project_id is None:
            return self.query("SELECT * FROM articles WHERE status='ready' ORDER BY id")
        return self.query("SELECT * FROM articles WHERE status='ready' AND project_id=? ORDER BY id", (project_id,))

    def claim_article(self, project_id: int) -> dict[str, Any] | None:
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM articles WHERE project_id=? AND status='ready' ORDER BY id LIMIT 1", (project_id,)
            ).fetchone()
            if not row:
                return None
            now = utcnow()
            c.execute(
                "UPDATE articles SET status='publishing', claimed_at=?, attempts=attempts+1, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            return dict(row)

    def update_article(self, article_id: int, **fields: Any) -> None:
        allowed = {"status", "dzen_publication_id", "dzen_url", "publish_mode", "last_error", "published_at",
                   "claimed_at", "cover_path", "images_json", "markdown", "title", "description", "tags_json"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols) + ", updated_at=?"
        self.execute(f"UPDATE articles SET {sets} WHERE id=?", [fields[k] for k in cols] + [utcnow(), article_id])

    def release_stale_claims(self, older_than_minutes: int = 45) -> int:
        rows = self.query(
            "SELECT id, claimed_at FROM articles WHERE status='publishing' AND claimed_at IS NOT NULL"
        )
        released = 0
        now = datetime.now(timezone.utc)
        for r in rows:
            try:
                claimed = datetime.fromisoformat(r["claimed_at"])
            except (TypeError, ValueError):
                claimed = now
            if (now - claimed).total_seconds() > older_than_minutes * 60:
                self.update_article(int(r["id"]), status="ready", claimed_at=None,
                                    last_error="stale publishing claim released")
                released += 1
        return released

    def published_links(self, project_id: int, limit: int = 12) -> list[dict[str, Any]]:
        return self.query(
            "SELECT id,title,dzen_url FROM articles WHERE project_id=? AND status='published' AND dzen_url<>'' "
            "ORDER BY published_at DESC LIMIT ?",
            (project_id, limit),
        )

    def published_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT a.*, p.slug AS project_slug, p.name AS project_name FROM articles a "
            "JOIN projects p ON p.id=a.project_id "
            "WHERE a.status IN ('published','draft_saved') AND a.published_at>=? AND a.published_at<? ORDER BY a.published_at",
            (start_iso, end_iso),
        )

    def articles_created_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT a.*, p.slug AS project_slug FROM articles a JOIN projects p ON p.id=a.project_id "
            "WHERE a.created_at>=? AND a.created_at<? ORDER BY a.id",
            (start_iso, end_iso),
        )

    def all_published(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT a.id, a.project_id, a.dzen_publication_id, a.title FROM articles a "
            "WHERE a.status='published' AND a.dzen_publication_id<>''"
        )

    # ------------------------------------------------------------------ runs
    def add_run(self, kind: str, status: str, *, project_id: int | None = None, article_id: int | None = None,
                note: str = "", started_at: str | None = None) -> int:
        now = utcnow()
        return self.execute(
            "INSERT INTO runs(kind,project_id,status,article_id,note,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
            (kind, project_id, status, article_id, note[:1000], started_at or now, now),
        )

    def runs_between(self, kind: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM runs WHERE kind=? AND finished_at>=? AND finished_at<? ORDER BY id",
            (kind, start_iso, end_iso),
        )

    def recent_failures(self, kind: str, minutes: int = 60) -> int:
        row = self.one(
            "SELECT COUNT(*) n FROM runs WHERE kind=? AND status='failed' AND finished_at>=?",
            (kind, datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - minutes * 60, tz=timezone.utc).isoformat(timespec='seconds')),
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------- llm usage
    def add_usage(self, provider: str, model: str, purpose: str, input_tokens: int, output_tokens: int,
                  cost_usd: float) -> None:
        self.execute(
            "INSERT INTO llm_usage(ts,provider,model,purpose,input_tokens,output_tokens,cost_usd) VALUES(?,?,?,?,?,?,?)",
            (utcnow(), provider, model, purpose, int(input_tokens), int(output_tokens), float(cost_usd)),
        )

    def usage_between(self, start_iso: str, end_iso: str) -> dict[str, Any]:
        row = self.one(
            "SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) out, "
            "COALESCE(SUM(cost_usd),0) cost FROM llm_usage WHERE ts>=? AND ts<?",
            (start_iso, end_iso),
        ) or {}
        return {"calls": int(row.get("calls", 0)), "input_tokens": int(row.get("inp", 0)),
                "output_tokens": int(row.get("out", 0)), "cost_usd": float(row.get("cost", 0.0))}

    # --------------------------------------------------------------- metrics
    def upsert_metric(self, article_id: int, views: int, likes: int, comments: int, shares: int) -> None:
        self.execute(
            "INSERT INTO metrics(article_id,views,likes,comments,shares,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(article_id) DO UPDATE SET views=excluded.views, likes=excluded.likes, "
            "comments=excluded.comments, shares=excluded.shares, updated_at=excluded.updated_at",
            (article_id, views, likes, comments, shares, utcnow()),
        )

    def metrics_total(self) -> dict[str, int]:
        row = self.one(
            "SELECT COALESCE(SUM(views),0) v, COALESCE(SUM(likes),0) l, COALESCE(SUM(comments),0) c, "
            "COALESCE(SUM(shares),0) s FROM metrics"
        ) or {}
        return {"views": int(row.get("v", 0)), "likes": int(row.get("l", 0)),
                "comments": int(row.get("c", 0)), "shares": int(row.get("s", 0))}

    def top_articles(self, limit: int = 3) -> list[dict[str, Any]]:
        return self.query(
            "SELECT a.id,a.title,a.dzen_url,p.slug project_slug,m.views,m.likes,m.comments FROM metrics m "
            "JOIN articles a ON a.id=m.article_id JOIN projects p ON p.id=a.project_id "
            "ORDER BY m.views DESC LIMIT ?",
            (limit,),
        )

    def save_channel_stats(self, day: str, publisher_id: str, subscribers: int | None, views: int, likes: int,
                           comments: int, payload: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO channel_stats(day,publisher_id,subscribers,views,likes,comments,payload_json,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(day,publisher_id) DO UPDATE SET subscribers=excluded.subscribers, "
            "views=excluded.views, likes=excluded.likes, comments=excluded.comments, payload_json=excluded.payload_json, "
            "updated_at=excluded.updated_at",
            (day, publisher_id, subscribers, views, likes, comments, json.dumps(payload, ensure_ascii=False), utcnow()),
        )

    def channel_stats_for(self, day: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM channel_stats WHERE day=?", (day,))

    # --------------------------------------------------------------- reports
    def report_sent(self, day: str) -> bool:
        return self.one("SELECT 1 FROM reports WHERE day=?", (day,)) is not None

    def save_report(self, day: str, message_id: int | None, payload: dict[str, Any]) -> None:
        self.execute(
            "INSERT OR REPLACE INTO reports(day,sent_at,telegram_message_id,payload_json) VALUES(?,?,?,?)",
            (day, utcnow(), message_id, json.dumps(payload, ensure_ascii=False)),
        )
