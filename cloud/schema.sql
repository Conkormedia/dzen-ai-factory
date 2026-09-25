CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY,
  headline TEXT NOT NULL,
  category TEXT,
  article_markdown TEXT NOT NULL,
  source_urls_json TEXT NOT NULL DEFAULT '[]',
  fact_check_json TEXT NOT NULL DEFAULT '[]',
  image_url TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  quality_ok INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);

CREATE TABLE IF NOT EXISTS metrics (
  article_id INTEGER PRIMARY KEY,
  views INTEGER NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  shares INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_usage (
  day TEXT PRIMARY KEY,
  requests INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- The former hard-coded 50 requests/day triggers are removed: the daily
-- OpenRouter cap is enforced atomically by quota.reserve() using
-- OPENROUTER_DAILY_LIMIT, which scales with ARTICLES_PER_DAY.
DROP TRIGGER IF EXISTS ai_usage_cap_insert;
DROP TRIGGER IF EXISTS ai_usage_cap_update;

CREATE TABLE IF NOT EXISTS resource_usage (
  day TEXT NOT NULL,
  resource TEXT NOT NULL,
  used REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(day, resource)
);

CREATE TABLE IF NOT EXISTS image_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'generating',
  source_run_id TEXT NOT NULL,
  artifact_name TEXT NOT NULL,
  candidate_json TEXT NOT NULL DEFAULT '[]',
  telegram_message_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(article_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_image_batches_article ON image_batches(article_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_image_batches_status ON image_batches(status);

CREATE TABLE IF NOT EXISTS article_packages (
  article_id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  package_day TEXT NOT NULL,
  source_run_id TEXT NOT NULL,
  artifact_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ready',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_packages_day ON article_packages(package_day, status);

CREATE TABLE IF NOT EXISTS article_package_deliveries (
  article_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  telegram_message_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(article_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_article_package_deliveries_status
ON article_package_deliveries(status);

CREATE TABLE IF NOT EXISTS daily_packages (
  day TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  telegram_message_id INTEGER,
  article_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_proposal_groups (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'pending',
  selected_proposal_id INTEGER,
  telegram_message_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topic_proposal_groups_status
ON topic_proposal_groups(status, created_at);

CREATE TABLE IF NOT EXISTS topic_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  title TEXT NOT NULL,
  link TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0,
  trend_title TEXT NOT NULL DEFAULT '',
  trend_views INTEGER NOT NULL DEFAULT 0,
  trend_channel TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(group_id, position)
);
CREATE INDEX IF NOT EXISTS idx_topic_proposals_group ON topic_proposals(group_id, position);
CREATE INDEX IF NOT EXISTS idx_topic_proposals_status ON topic_proposals(status, created_at);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Autopilot: publication queue, dispatch accounting, daily reports, metrics.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS publications (
  article_id INTEGER PRIMARY KEY,
  batch_id INTEGER,
  source_run_id TEXT NOT NULL DEFAULT '',
  artifact_name TEXT NOT NULL DEFAULT '',
  image_count INTEGER NOT NULL DEFAULT 0,
  cover_file TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ready',
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed_at TEXT,
  dzen_publication_id TEXT,
  dzen_url TEXT,
  publish_mode TEXT NOT NULL DEFAULT '',
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_publications_status ON publications(status, created_at);
CREATE INDEX IF NOT EXISTS idx_publications_published ON publications(published_at);

CREATE TABLE IF NOT EXISTS auto_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,
  dispatch_token TEXT NOT NULL UNIQUE,
  dispatched_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'dispatched',
  article_id INTEGER,
  run_id TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auto_runs_day ON auto_runs(day, status);

CREATE TABLE IF NOT EXISTS daily_reports (
  day TEXT PRIMARY KEY,
  sent_at TEXT NOT NULL,
  telegram_message_id INTEGER,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS channel_stats (
  day TEXT PRIMARY KEY,
  subscribers INTEGER,
  views INTEGER NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  shares INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_assets (
  id TEXT PRIMARY KEY,
  article_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  content_type TEXT NOT NULL DEFAULT 'image/jpeg',
  data BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publish_assets_article ON publish_assets(article_id);
