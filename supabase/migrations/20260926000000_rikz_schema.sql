-- Whitehomes/Rikz portfolio reporting: database objects on Supabase.
-- Generated from src/rikz/store/db.py (schema version 1); the app creates the
-- same objects itself on start-up, so this file and the app never disagree.

create schema if not exists rikz;
-- Not exposed through Supabase's Data API (only 'public' is by default).
revoke all on schema rikz from anon, authenticated;

CREATE TABLE IF NOT EXISTS rikz.drive_file (
	drive_id VARCHAR(128) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	md5 VARCHAR(64), 
	sha256 VARCHAR(64), 
	seen_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	note TEXT, 
	PRIMARY KEY (drive_id)
);
alter table rikz.drive_file enable row level security;

CREATE TABLE IF NOT EXISTS rikz.job (
	id SERIAL NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	payload JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	attempts INTEGER NOT NULL, 
	run_after TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
alter table rikz.job enable row level security;

CREATE TABLE IF NOT EXISTS rikz.login_attempt (
	id SERIAL NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	client VARCHAR(64) NOT NULL, 
	ok BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);
alter table rikz.login_attempt enable row level security;

CREATE TABLE IF NOT EXISTS rikz.meta (
	key VARCHAR(64) NOT NULL, 
	value TEXT NOT NULL, 
	PRIMARY KEY (key)
);
alter table rikz.meta enable row level security;

CREATE TABLE IF NOT EXISTS rikz.notification (
	id SERIAL NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	audience VARCHAR(16) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	snapshot_version INTEGER, 
	recipient VARCHAR(255) NOT NULL, 
	subject VARCHAR(255) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	sent_at TIMESTAMP WITH TIME ZONE, 
	job_id INTEGER, 
	PRIMARY KEY (id)
);
alter table rikz.notification enable row level security;

CREATE TABLE IF NOT EXISTS rikz.snapshot (
	id SERIAL NOT NULL, 
	version INTEGER NOT NULL, 
	as_of DATE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	inputs JSON NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	coverage JSON NOT NULL, 
	headline JSON NOT NULL, 
	changes JSON NOT NULL, 
	report TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (version)
);
alter table rikz.snapshot enable row level security;

CREATE TABLE IF NOT EXISTS rikz.view_log (
	id SERIAL NOT NULL, 
	at TIMESTAMP WITH TIME ZONE NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	path VARCHAR(255) NOT NULL, 
	snapshot_version INTEGER, 
	client VARCHAR(64) NOT NULL, 
	user_agent VARCHAR(255) NOT NULL, 
	PRIMARY KEY (id)
);
alter table rikz.view_log enable row level security;

CREATE TABLE IF NOT EXISTS rikz.batch (
	id SERIAL NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	source VARCHAR(16) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	reasons JSON NOT NULL, 
	notes JSON NOT NULL, 
	file_names JSON NOT NULL, 
	file_hashes JSON NOT NULL, 
	snapshot_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(snapshot_id) REFERENCES rikz.snapshot (id)
);
alter table rikz.batch enable row level security;

CREATE TABLE IF NOT EXISTS rikz.stored_file (
	sha256 VARCHAR(64) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	channel VARCHAR(16) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	size INTEGER NOT NULL, 
	batch_id INTEGER NOT NULL, 
	stored_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	covers_from DATE, 
	covers_to DATE, 
	paired_with VARCHAR(64), 
	summary JSON NOT NULL, 
	PRIMARY KEY (sha256), 
	FOREIGN KEY(batch_id) REFERENCES rikz.batch (id)
);
alter table rikz.stored_file enable row level security;

insert into rikz.meta (key, value) values ('schema_version', '1') on conflict (key) do nothing;

-- Private bucket for the statement files (content-addressed, written once).
insert into storage.buckets (id, name, public) values ('statements', 'statements', false)
on conflict (id) do update set public = false;
