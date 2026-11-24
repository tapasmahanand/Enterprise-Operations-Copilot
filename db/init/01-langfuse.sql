-- Runs once, on first start of an empty Postgres volume: Langfuse gets its own database
-- on the same server, so the stack needs only one Postgres.
CREATE DATABASE langfuse;
