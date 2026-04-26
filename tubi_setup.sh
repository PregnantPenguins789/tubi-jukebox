#!/bin/bash
# tubi_setup.sh - One-shot SQLite setup for Tubi movie catalog
# Run from the directory containing tubi_movies_final.csv
# Usage: bash tubi_setup.sh

CSV="tubi_movies_final.csv"
DB="tubi.db"

if [ ! -f "$CSV" ]; then
  echo "❌ $CSV not found. Run from the same directory as the CSV."
  exit 1
fi

echo "🎬 Setting up Tubi SQLite database..."

sqlite3 "$DB" <<'ENDSQL'

-- Drop and recreate for clean import
DROP TABLE IF EXISTS movies_raw;
DROP TABLE IF EXISTS movies;
DROP TABLE IF EXISTS movie_genres;
DROP TABLE IF EXISTS movie_actors;
DROP TABLE IF EXISTS movie_directors;

-- Raw import table (CSV columns land here as-is)
CREATE TABLE movies_raw (
  id TEXT,
  title TEXT,
  year TEXT,
  rating TEXT,
  genres TEXT,
  directors TEXT,
  actors TEXT,
  duration TEXT,
  description TEXT,
  source TEXT
);

ENDSQL

# Import CSV (sqlite3 .import handles headers automatically in newer versions)
sqlite3 "$DB" <<ENDSQL
.mode csv
.headers on
.import $CSV movies_raw
ENDSQL

sqlite3 "$DB" <<'ENDSQL'

-- Clean normalized table
CREATE TABLE movies AS
SELECT
  CAST(id AS INTEGER)       AS id,
  TRIM(title)               AS title,
  CAST(year AS INTEGER)     AS year,
  TRIM(rating)              AS rating,
  TRIM(genres)              AS genres_raw,
  TRIM(directors)           AS directors_raw,
  TRIM(actors)              AS actors_raw,
  CAST(duration AS INTEGER) AS duration_minutes,
  TRIM(description)         AS description,
  TRIM(source)              AS source,
  'https://tubitv.com/movies/' || CAST(id AS INTEGER) AS url
FROM movies_raw
WHERE id != 'ID'  -- skip header row if it snuck in
  AND title IS NOT NULL
  AND title != '';

-- Index the things you'll search most
CREATE INDEX idx_title    ON movies(title);
CREATE INDEX idx_year     ON movies(year);
CREATE INDEX idx_rating   ON movies(rating);
CREATE INDEX idx_genres   ON movies(genres_raw);

-- Convenience views for common queries

CREATE VIEW horror AS
  SELECT id, title, year, rating, duration_minutes, genres_raw, url
  FROM movies WHERE genres_raw LIKE '%Horror%'
  ORDER BY year DESC;

CREATE VIEW action AS
  SELECT id, title, year, rating, duration_minutes, genres_raw, url
  FROM movies WHERE genres_raw LIKE '%Action%'
  ORDER BY year DESC;

CREATE VIEW documentary AS
  SELECT id, title, year, rating, duration_minutes, genres_raw, url
  FROM movies WHERE genres_raw LIKE '%Documentary%'
  ORDER BY year DESC;

CREATE VIEW classics AS
  SELECT id, title, year, rating, duration_minutes, genres_raw, url
  FROM movies WHERE year < 1970
  ORDER BY year DESC;

CREATE VIEW short_films AS
  SELECT id, title, year, rating, duration_minutes, genres_raw, url
  FROM movies WHERE duration_minutes < 60 AND duration_minutes > 0
  ORDER BY duration_minutes;

-- Drop the raw import table
DROP TABLE movies_raw;

-- Quick stats on import
SELECT '✅ Import complete.' AS status;
SELECT COUNT(*) || ' movies loaded' AS result FROM movies;
SELECT 'Year range: ' || MIN(year) || ' - ' || MAX(year) AS result FROM movies WHERE year > 0;

ENDSQL

echo ""
echo "✅ Done. Database: $DB"
echo ""
echo "Quick start queries:"
echo "  sqlite3 $DB"
echo "  sqlite> SELECT COUNT(*) FROM movies;"
echo "  sqlite> SELECT * FROM horror LIMIT 10;"
echo "  sqlite> SELECT title, year, genres_raw FROM movies WHERE genres_raw LIKE '%Noir%' ORDER BY year;"
echo "  sqlite> SELECT title, url FROM movies WHERE directors_raw LIKE '%Kubrick%';"
echo "  sqlite> SELECT title, year FROM movies WHERE actors_raw LIKE '%Buster Keaton%';"
echo "  sqlite> .quit"
