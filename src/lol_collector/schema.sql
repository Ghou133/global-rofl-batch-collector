PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS collection_run (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  status TEXT NOT NULL,
  region TEXT NOT NULL,
  rso_platform_id TEXT,
  sgp_server_id TEXT,
  target_patch TEXT NOT NULL,
  target_full_version TEXT,
  window_start_at TEXT NOT NULL,
  window_end_at TEXT NOT NULL,
  selected_tiers TEXT NOT NULL,
  target_valid_matches INTEGER NOT NULL,
  master_plus_threshold INTEGER NOT NULL,
  require_known_rank_count INTEGER NOT NULL,
  scheduler_rng_seed INTEGER NOT NULL,
  capability_profile_id INTEGER,
  queue_snapshot_id INTEGER
);
CREATE TABLE IF NOT EXISTS raw_artifact (
  id INTEGER PRIMARY KEY,
  artifact_type TEXT NOT NULL,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  endpoint_template TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  byte_size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  filesystem_path TEXT NOT NULL UNIQUE,
  schema_hash TEXT NOT NULL,
  artifact_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS capability_probe (
  id INTEGER PRIMARY KEY,
  probe_time TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  capability TEXT NOT NULL,
  status TEXT NOT NULL,
  success INTEGER NOT NULL,
  schema_hash TEXT,
  sanitized_error TEXT,
  raw_artifact_id INTEGER REFERENCES raw_artifact(id)
);
CREATE TABLE IF NOT EXISTS queue_mapping_snapshot (
  id INTEGER PRIMARY KEY,
  fetched_at TEXT NOT NULL,
  raw_artifact_id INTEGER NOT NULL REFERENCES raw_artifact(id),
  solo_ranked_queue_id INTEGER,
  schema_hash TEXT NOT NULL,
  classifier_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leaderboard_snapshot (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES collection_run(id),
  tier TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  entry_count INTEGER NOT NULL,
  pagination_complete INTEGER,
  schema_hash TEXT NOT NULL,
  raw_artifact_id INTEGER NOT NULL REFERENCES raw_artifact(id)
);
CREATE TABLE IF NOT EXISTS player (
  id INTEGER PRIMARY KEY,
  region TEXT NOT NULL,
  puuid TEXT,
  summoner_id TEXT,
  riot_game_name TEXT,
  riot_tagline TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(region, puuid)
);
CREATE TABLE IF NOT EXISTS leaderboard_entry (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL REFERENCES leaderboard_snapshot(id),
  player_id INTEGER NOT NULL REFERENCES player(id),
  returned_puuid TEXT,
  returned_summoner_id TEXT,
  returned_riot_id TEXT,
  tier TEXT NOT NULL,
  lp INTEGER,
  leaderboard_position INTEGER,
  raw_entry_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS player_rank_snapshot (
  id INTEGER PRIMARY KEY,
  player_id INTEGER NOT NULL REFERENCES player(id),
  observed_at TEXT NOT NULL,
  source TEXT NOT NULL,
  region TEXT NOT NULL,
  queue_type TEXT NOT NULL,
  tier TEXT NOT NULL,
  division TEXT,
  lp INTEGER,
  wins INTEGER,
  losses INTEGER,
  raw_artifact_id INTEGER REFERENCES raw_artifact(id),
  schema_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS player_queue (
  run_id INTEGER NOT NULL REFERENCES collection_run(id),
  player_id INTEGER NOT NULL REFERENCES player(id),
  state TEXT NOT NULL,
  rank_status TEXT NOT NULL,
  history_start_index INTEGER NOT NULL DEFAULT 0,
  history_page_size INTEGER NOT NULL DEFAULT 20,
  pages_crawled INTEGER NOT NULL DEFAULT 0,
  oldest_seen_game_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  resume_state TEXT NOT NULL DEFAULT 'RANK_CHECK',
  failed_step TEXT,
  last_error TEXT,
  lease_owner TEXT,
  lease_expires_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(run_id, player_id)
);
CREATE TABLE IF NOT EXISTS match (
  id INTEGER PRIMARY KEY,
  sgp_server_id TEXT NOT NULL,
  platform_id TEXT NOT NULL,
  game_id TEXT NOT NULL,
  game_start_at TEXT,
  game_end_at TEXT,
  queue_id INTEGER,
  game_mode TEXT,
  map_id INTEGER,
  game_version_full TEXT,
  patch_normalized TEXT,
  summary_artifact_id INTEGER REFERENCES raw_artifact(id),
  details_artifact_id INTEGER REFERENCES raw_artifact(id),
  parsed_at TEXT,
  UNIQUE(sgp_server_id, game_id)
);
CREATE TABLE IF NOT EXISTS run_match (
  run_id INTEGER NOT NULL REFERENCES collection_run(id),
  match_id INTEGER NOT NULL REFERENCES match(id),
  discovered_at TEXT NOT NULL,
  queue_valid INTEGER,
  patch_valid INTEGER,
  time_valid INTEGER,
  known_rank_count INTEGER NOT NULL DEFAULT 0,
  unknown_rank_count INTEGER NOT NULL DEFAULT 0,
  master_count INTEGER NOT NULL DEFAULT 0,
  grandmaster_count INTEGER NOT NULL DEFAULT 0,
  challenger_count INTEGER NOT NULL DEFAULT 0,
  below_master_count INTEGER NOT NULL DEFAULT 0,
  master_plus_count INTEGER NOT NULL DEFAULT 0,
  target_valid INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id, match_id)
);
CREATE TABLE IF NOT EXISTS match_queue (
  match_id INTEGER PRIMARY KEY REFERENCES match(id),
  state TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  resume_state TEXT NOT NULL DEFAULT 'SUMMARY',
  failed_step TEXT,
  last_error TEXT,
  lease_owner TEXT,
  lease_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS match_participant (
  match_id INTEGER NOT NULL REFERENCES match(id),
  participant_id INTEGER NOT NULL,
  player_id INTEGER REFERENCES player(id),
  puuid TEXT NOT NULL,
  team_id INTEGER,
  rank_snapshot_id_used INTEGER REFERENCES player_rank_snapshot(id),
  rank_snapshot_delay_seconds REAL,
  PRIMARY KEY(match_id, participant_id),
  UNIQUE(match_id, puuid)
);
CREATE TABLE IF NOT EXISTS match_discovery (
  match_id INTEGER NOT NULL REFERENCES match(id),
  discovered_by_player_id INTEGER NOT NULL REFERENCES player(id),
  run_id INTEGER NOT NULL REFERENCES collection_run(id),
  history_page INTEGER NOT NULL,
  discovered_at TEXT NOT NULL,
  PRIMARY KEY(match_id, discovered_by_player_id, run_id)
);
CREATE TABLE IF NOT EXISTS error_event (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES collection_run(id),
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  step TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  error_class TEXT NOT NULL,
  retryable INTEGER NOT NULL,
  status_code INTEGER,
  sanitized_message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_artifact (
  game_id TEXT PRIMARY KEY,
  match_id INTEGER UNIQUE REFERENCES match(id),
  details_artifact_id INTEGER REFERENCES raw_artifact(id),
  game_version TEXT,
  patch TEXT,
  rofl_path TEXT,
  source_path TEXT,
  file_size INTEGER CHECK(file_size IS NULL OR file_size >= 0),
  sha256 TEXT,
  download_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED' CHECK(download_status IN ('NOT_REQUESTED', 'QUEUED', 'DOWNLOADING', 'DOWNLOADED', 'DOWNLOADED_BUT_INVALID', 'VALIDATED', 'FAILED', 'UNAVAILABLE')),
  validation_status TEXT NOT NULL DEFAULT 'NOT_VALIDATED' CHECK(validation_status IN ('NOT_VALIDATED', 'VALIDATED', 'INVALID')),
  download_started_at TEXT,
  download_completed_at TEXT,
  error_code TEXT,
  error_message TEXT,
  acquisition_method TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_checked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(download_status <> 'VALIDATED' OR (match_id IS NOT NULL AND details_artifact_id IS NOT NULL AND rofl_path IS NOT NULL AND file_size > 0 AND sha256 IS NOT NULL AND validation_status = 'VALIDATED'))
);
CREATE TABLE IF NOT EXISTS replay_source (
  source_path TEXT PRIMARY KEY,
  game_id TEXT NOT NULL,
  platform_id TEXT,
  observed_size INTEGER NOT NULL,
  observed_mtime_ns INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition IN ('BASELINE', 'QUEUED', 'CAPTURED', 'FAILED', 'UNAVAILABLE')),
  error_code TEXT,
  error_message TEXT,
  next_retry_at TEXT,
  processed_at TEXT
);
CREATE TABLE IF NOT EXISTS replay_capture_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_player_queue_ready ON player_queue(state, next_retry_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_match_queue_ready ON match_queue(state, next_retry_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_replay_artifact_status ON replay_artifact(download_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_replay_source_disposition ON replay_source(disposition, first_seen_at);
