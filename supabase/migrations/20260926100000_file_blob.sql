-- Statement files kept in the database (FILE_STORE=database), for hosting on
-- Vercel where the app has no disk. Matches rikz.store.db.FileBlob.
CREATE TABLE IF NOT EXISTS rikz.file_blob (
	sha256 VARCHAR(64) NOT NULL,
	data BYTEA NOT NULL,
	stored_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (sha256)
);
alter table rikz.file_blob enable row level security;
