-- Runs once on first Postgres container start.
-- The pytest suite needs an isolated database so a test run can never
-- truncate development data.
CREATE DATABASE revrec_test OWNER revrec;
