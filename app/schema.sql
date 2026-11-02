CREATE TABLE IF NOT EXISTS matters (
    id SERIAL PRIMARY KEY,
    number VARCHAR(30) NOT NULL UNIQUE,
    title VARCHAR(150) NOT NULL,
    client VARCHAR(150) NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id SERIAL PRIMARY KEY,
    matter_id INTEGER NOT NULL REFERENCES matters(id),
    title VARCHAR(150) NOT NULL,
    filename VARCHAR(200) NOT NULL,
    storage_key CHAR(32) NOT NULL UNIQUE,
    size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
    sha256 CHAR(64) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ok','error')),
    message VARCHAR(200) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
