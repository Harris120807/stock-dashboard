CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  alerts INTEGER NOT NULL DEFAULT 0,
  unsub_token TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS tokens (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlists (
  user_id INTEGER PRIMARY KEY,
  tickers TEXT NOT NULL DEFAULT '[]',
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS attempts (
  key TEXT PRIMARY KEY,
  n INTEGER NOT NULL,
  reset_at INTEGER NOT NULL
);
-- security monitoring (2026-07-26): intrusion/tamper event log. Written
-- best-effort by the Worker (secLog); read by /admin/security and the hourly
-- anomaly check. detail is route names / generic text only — never emails,
-- passwords or tokens.
CREATE TABLE IF NOT EXISTS security_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,          -- epoch seconds
  kind TEXT NOT NULL,           -- admin_auth_fail | login_fail | signup | password_reset | account_delete | canary_login | t212_connect | t212_delete
  detail TEXT,
  country TEXT
);
CREATE INDEX IF NOT EXISTS idx_seclog_at ON security_log(at);
CREATE INDEX IF NOT EXISTS idx_seclog_kind_at ON security_log(kind, at);
-- Trading 212 portfolio import (2026-07-26): one read-only broker key per
-- user. enc = base64(iv || AES-256-GCM ciphertext) under Worker secret
-- VAULT_KEY — never plaintext. env records which T212 environment the key
-- validated against (live | demo).
CREATE TABLE IF NOT EXISTS broker_keys (
  user_id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,       -- 't212'
  enc TEXT NOT NULL,
  env TEXT NOT NULL DEFAULT 'live',
  created_at INTEGER
);
-- daily portfolio value snapshots (2026-07-27): one row per connected T212
-- user per UTC day, written by the 20:30 Worker cron from /equity/account/cash;
-- wiped on broker disconnect and on account deletion
CREATE TABLE IF NOT EXISTS portfolio_history (
  user_id INTEGER NOT NULL,
  d INTEGER NOT NULL,            -- UTC daynum (unix ms / 86400000)
  total REAL, invested REAL, ppl REAL,
  PRIMARY KEY (user_id, d)
);
-- custom per-stock alert rules (2026-07-27): one-shot — the digest cron
-- stamps triggered_at when a rule fires; the UI can re-arm (NULL it)
CREATE TABLE IF NOT EXISTS alert_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  ticker TEXT NOT NULL,
  kind TEXT NOT NULL,            -- price_above/price_below/score_above/score_below/rsi_above/rsi_below
  threshold REAL,
  created_at INTEGER NOT NULL,
  triggered_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rules_user ON alert_rules(user_id);
-- Worker route-error log (2026-07-27): our own exception text per route,
-- best-effort, surfaced as "Errors (24h)" on the admin Status card
CREATE TABLE IF NOT EXISTS error_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,           -- epoch seconds
  route TEXT,
  detail TEXT
);
