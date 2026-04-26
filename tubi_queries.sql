# tubi.db SQLite Cheatsheet
# Run: sqlite3 tubi.db
# Enable readable output: .mode column / .headers on / .width 40 5 20 6

-- SETUP (paste at start of session for readable output)
.mode column
.headers on
.width 40 5 25 5 6

-- ─────────────────────────────────────
-- COUNTS & STATS
-- ─────────────────────────────────────
SELECT COUNT(*) FROM movies;
SELECT year, COUNT(*) AS count FROM movies GROUP BY year ORDER BY year DESC;
SELECT rating, COUNT(*) AS count FROM movies GROUP BY rating ORDER BY count DESC;

-- ─────────────────────────────────────
-- GENRE BROWSING (use the views)
-- ─────────────────────────────────────
SELECT * FROM horror LIMIT 20;
SELECT * FROM action LIMIT 20;
SELECT * FROM documentary LIMIT 20;
SELECT * FROM classics LIMIT 20;
SELECT * FROM short_films LIMIT 20;

-- Any genre search:
SELECT title, year, genres_raw, url FROM movies
WHERE genres_raw LIKE '%Thriller%'
ORDER BY year DESC LIMIT 20;

-- Multiple genres (AND):
SELECT title, year, genres_raw FROM movies
WHERE genres_raw LIKE '%Horror%' AND genres_raw LIKE '%Comedy%'
ORDER BY year;

-- ─────────────────────────────────────
-- SEARCH
-- ─────────────────────────────────────
-- Title search (case insensitive):
SELECT title, year, url FROM movies
WHERE title LIKE '%ninja%'
ORDER BY year;

-- Director search:
SELECT title, year, genres_raw, url FROM movies
WHERE directors_raw LIKE '%Cronenberg%';

-- Actor search:
SELECT title, year, genres_raw, url FROM movies
WHERE actors_raw LIKE '%Christopher Lee%'
ORDER BY year;

-- Full text search across all fields:
SELECT title, year, genres_raw FROM movies
WHERE title LIKE '%blood%'
   OR description LIKE '%vampire%'
   OR actors_raw LIKE '%Lugosi%';

-- ─────────────────────────────────────
-- SORTING & FILTERING
-- ─────────────────────────────────────
-- By decade:
SELECT title, year, genres_raw FROM movies
WHERE year BETWEEN 1960 AND 1969
ORDER BY year;

-- Short movies (under 75 min):
SELECT title, year, duration_minutes, genres_raw FROM movies
WHERE duration_minutes BETWEEN 1 AND 75
ORDER BY duration_minutes;

-- Feature length only:
SELECT title, year, duration_minutes FROM movies
WHERE duration_minutes >= 70
ORDER BY year DESC;

-- Unrated / unknown:
SELECT title, year, genres_raw FROM movies
WHERE rating IS NULL OR rating = ''
ORDER BY year DESC LIMIT 20;

-- ─────────────────────────────────────
-- GET URL FOR PLAYBACK
-- ─────────────────────────────────────
-- Get the URL for a specific title (open in browser):
SELECT title, year, url FROM movies WHERE title = 'Body Double';

-- xdg-open from shell (not sqlite - use in bash):
-- xdg-open "$(sqlite3 tubi.db "SELECT url FROM movies WHERE title='Body Double'")"

-- ─────────────────────────────────────
-- EXPORT SUBSETS BACK TO CSV
-- ─────────────────────────────────────
.mode csv
.headers on
.output horror_list.csv
SELECT * FROM horror;
.output stdout
.mode column

-- ─────────────────────────────────────
-- USEFUL .commands (not SQL)
-- ─────────────────────────────────────
.tables           -- list tables and views
.schema movies    -- show table structure
.mode column      -- readable columns
.mode csv         -- CSV mode
.headers on/off   -- toggle headers
.width 40 5 20    -- set column widths
.quit             -- exit
