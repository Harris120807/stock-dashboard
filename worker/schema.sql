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
  kind TEXT NOT NULL,           -- admin_auth_fail | login_fail | signup | password_reset | account_delete | canary_login
  detail TEXT,
  country TEXT
);
CREATE INDEX IF NOT EXISTS idx_seclog_at ON security_log(at);
CREATE INDEX IF NOT EXISTS idx_seclog_kind_at ON security_log(kind, at);
