-- =============================================================================
-- 01_extensions.sql — required extensions
-- =============================================================================
-- Runs as the database owner (a superuser inside the postgis/postgis image).

-- Geospatial types + operators. Gives us geography(Point,4326) for restaurant
-- locations, metre-accurate distance, and a GiST-indexable search path.
CREATE EXTENSION IF NOT EXISTS postgis;

-- Needed by the reservations EXCLUDE constraint: GiST has no native equality
-- operator class for scalar types, so `table_id WITH =` requires this.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Case-insensitive text: email addresses compare and de-duplicate correctly
-- without sprinkling lower() through every query and index.
CREATE EXTENSION IF NOT EXISTS citext;

-- Trigram index support for fuzzy restaurant-name search.
CREATE EXTENSION IF NOT EXISTS pg_trgm;