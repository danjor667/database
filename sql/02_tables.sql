-- =============================================================================
-- 02_tables.sql — 3NF relational schema
-- =============================================================================
-- Design notes and the normalization argument live in docs/er-diagram.md.
-- This file is idempotent: it drops and recreates the application schema.

DROP TABLE IF EXISTS reviews             CASCADE;
DROP TABLE IF EXISTS reservations        CASCADE;
DROP TABLE IF EXISTS restaurant_cuisines CASCADE;
DROP TABLE IF EXISTS restaurant_tables   CASCADE;
DROP TABLE IF EXISTS restaurants         CASCADE;
DROP TABLE IF EXISTS cuisines            CASCADE;
DROP TABLE IF EXISTS customers           CASCADE;
DROP TABLE IF EXISTS cities              CASCADE;

DROP TYPE IF EXISTS reservation_status CASCADE;
DROP TYPE IF EXISTS table_zone         CASCADE;


-- -----------------------------------------------------------------------------
-- Enumerated domains
-- -----------------------------------------------------------------------------
-- Small, stable, application-controlled vocabularies. Enums keep them out of
-- the relational graph without losing referential safety; a lookup table would
-- be the right call only if these values needed user-facing metadata.

CREATE TYPE table_zone AS ENUM ('indoor', 'patio', 'bar', 'private');

-- Lifecycle: pending -> confirmed -> seated -> completed, with cancelled /
-- no_show as terminal exits. Only the first four hold the table (see the
-- reservations_no_double_booking constraint).
CREATE TYPE reservation_status AS ENUM (
    'pending', 'confirmed', 'seated', 'completed', 'cancelled', 'no_show'
);


-- -----------------------------------------------------------------------------
-- cities
-- -----------------------------------------------------------------------------
-- Extracted from restaurants so that (region, country) depend on a city key
-- rather than on a city *name* repeated per restaurant. Also makes the
-- "rank restaurants by average review score per city" query group on a real
-- key instead of a free-text field where 'NYC' and 'New York' diverge.
CREATE TABLE cities (
    city_id      integer  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text     NOT NULL,
    region       text     NOT NULL,          -- state / province
    country_code char(2)  NOT NULL,          -- ISO 3166-1 alpha-2

    CONSTRAINT cities_natural_key      UNIQUE (name, region, country_code),
    CONSTRAINT cities_name_not_blank   CHECK (btrim(name) <> ''),
    CONSTRAINT cities_region_not_blank CHECK (btrim(region) <> ''),
    CONSTRAINT cities_country_iso      CHECK (country_code ~ '^[A-Z]{2}$')
);

COMMENT ON TABLE cities IS
    'Reference data for restaurant locations; grouping key for per-city analytics.';


-- -----------------------------------------------------------------------------
-- cuisines / restaurant_cuisines
-- -----------------------------------------------------------------------------
-- A restaurant has many cuisines and a cuisine has many restaurants, so the
-- relationship is resolved through a junction table rather than a repeating
-- group (e.g. a cuisine_1 / cuisine_2 column pair, or a comma-joined string),
-- either of which would break 1NF.
CREATE TABLE cuisines (
    cuisine_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       citext  NOT NULL UNIQUE,

    CONSTRAINT cuisines_name_not_blank CHECK (btrim(name) <> '')
);

CREATE TABLE restaurant_cuisines (
    restaurant_id integer NOT NULL,
    cuisine_id    integer NOT NULL REFERENCES cuisines (cuisine_id) ON DELETE RESTRICT,
    is_primary    boolean NOT NULL DEFAULT false,

    PRIMARY KEY (restaurant_id, cuisine_id)
    -- FK to restaurants is added after that table exists (see below).
);


-- -----------------------------------------------------------------------------
-- restaurants
-- -----------------------------------------------------------------------------
-- `location` is a PostGIS geography point (SRID 4326, lon/lat order). Latitude
-- and longitude are deliberately NOT stored as separate columns: they are
-- derivable from `location` via ST_Y/ST_X, and duplicating them would create a
-- second source of truth that no constraint could keep in sync.
--
-- `menu` and `opening_hours` are JSONB by assignment. They are semi-structured,
-- read as a whole document, and vary in shape per restaurant, so the relational
-- decomposition that normalization would demand buys nothing here. The
-- structural CHECKs below are the floor of validation that keeps them queryable.
CREATE TABLE restaurants (
    restaurant_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text    NOT NULL,
    city_id       integer NOT NULL REFERENCES cities (city_id) ON DELETE RESTRICT,
    address_line  text    NOT NULL,
    postal_code   text,
    location      geography(Point, 4326) NOT NULL,
    phone         text,
    email         citext  UNIQUE,

    -- 1 = $, 4 = $$$$
    price_tier    smallint NOT NULL DEFAULT 2,

    -- Default dining duration, i.e. how long one booking holds a table. Drives
    -- the availability CTE, so it belongs to the restaurant, not the query.
    slot_minutes  smallint NOT NULL DEFAULT 90,

    -- { "currency": "USD",
    --   "sections": [ { "name": "Starters",
    --                   "items": [ { "name": "...", "price": 12.5,
    --                                "tags": ["vegan"] } ] } ] }
    menu          jsonb    NOT NULL DEFAULT '{"currency": "USD", "sections": []}',

    -- { "mon": [], "tue": [ { "open": "17:00", "close": "22:30" } ], ... }
    -- An empty array means closed that day; two entries model a split shift.
    opening_hours jsonb    NOT NULL DEFAULT '{}',

    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT restaurants_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT restaurants_price_tier_range CHECK (price_tier BETWEEN 1 AND 4),
    -- 15-minute floor, 8-hour ceiling, and a whole number of quarter-hours.
    CONSTRAINT restaurants_slot_minutes_sane
        CHECK (slot_minutes BETWEEN 15 AND 480 AND slot_minutes % 15 = 0),
    CONSTRAINT restaurants_menu_shape
        CHECK (jsonb_typeof(menu) = 'object' AND jsonb_typeof(menu -> 'sections') = 'array'),
    CONSTRAINT restaurants_opening_hours_shape
        CHECK (jsonb_typeof(opening_hours) = 'object'),
    -- Reject a geography that is not a single point (e.g. a polygon cast in).
    CONSTRAINT restaurants_location_is_point
        CHECK (GeometryType(location::geometry) = 'POINT')
);

COMMENT ON COLUMN restaurants.location IS
    'WGS84 point (lon, lat). Query with ST_DWithin for index-assisted radius search.';
COMMENT ON COLUMN restaurants.slot_minutes IS
    'Length of one reservation slot; the duration a booking holds a table.';

ALTER TABLE restaurant_cuisines
    ADD CONSTRAINT restaurant_cuisines_restaurant_fk
        FOREIGN KEY (restaurant_id) REFERENCES restaurants (restaurant_id) ON DELETE CASCADE;

-- At most one primary cuisine per restaurant.
CREATE UNIQUE INDEX restaurant_cuisines_one_primary
    ON restaurant_cuisines (restaurant_id)
    WHERE is_primary;


-- -----------------------------------------------------------------------------
-- restaurant_tables
-- -----------------------------------------------------------------------------
-- The physical inventory being booked. `table_code` is unique per restaurant
-- (the natural key) but the surrogate PK keeps reservations narrow.
CREATE TABLE restaurant_tables (
    table_id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    restaurant_id  integer NOT NULL REFERENCES restaurants (restaurant_id) ON DELETE CASCADE,
    table_code     text    NOT NULL,           -- 'T01', 'PATIO-3'
    seats          smallint NOT NULL,
    -- Floor on party size: stops a 2-person booking consuming a 10-seat table.
    min_party_size smallint NOT NULL DEFAULT 1,
    zone           table_zone NOT NULL DEFAULT 'indoor',
    is_active      boolean  NOT NULL DEFAULT true,

    CONSTRAINT restaurant_tables_code_key  UNIQUE (restaurant_id, table_code),
    CONSTRAINT restaurant_tables_code_not_blank CHECK (btrim(table_code) <> ''),
    CONSTRAINT restaurant_tables_seats_range    CHECK (seats BETWEEN 1 AND 30),
    CONSTRAINT restaurant_tables_party_range
        CHECK (min_party_size >= 1 AND min_party_size <= seats)
);


-- -----------------------------------------------------------------------------
-- customers
-- -----------------------------------------------------------------------------
CREATE TABLE customers (
    customer_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_name  text   NOT NULL,
    last_name   text   NOT NULL,
    email       citext NOT NULL UNIQUE,
    phone       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT customers_first_name_not_blank CHECK (btrim(first_name) <> ''),
    CONSTRAINT customers_last_name_not_blank  CHECK (btrim(last_name) <> ''),
    -- Deliberately permissive: full RFC 5322 validation belongs in the app.
    CONSTRAINT customers_email_shape CHECK (email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$')
);


-- -----------------------------------------------------------------------------
-- reservations  (the core booking relation)
-- -----------------------------------------------------------------------------
-- restaurant_id is NOT duplicated here: it is functionally determined by
-- table_id (table -> restaurant), so storing it would be the textbook 3NF
-- transitive-dependency violation. Joins go through restaurant_tables, which
-- is a single indexed hop; v_reservation_details flattens it for convenience.
--
-- The booked interval is one tstzrange column rather than a start/end pair.
-- That is what makes overlap a *declarative* constraint instead of application
-- logic: the EXCLUDE constraint below makes double-booking unrepresentable, so
-- the Day 2 transaction is correct even under concurrent commits.
CREATE TABLE reservations (
    reservation_id   bigint  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_id         integer NOT NULL REFERENCES restaurant_tables (table_id) ON DELETE RESTRICT,
    customer_id      integer NOT NULL REFERENCES customers (customer_id) ON DELETE RESTRICT,
    during           tstzrange NOT NULL,
    party_size       smallint  NOT NULL,
    status           reservation_status NOT NULL DEFAULT 'confirmed',
    special_requests text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT reservations_party_size_positive CHECK (party_size > 0),

    -- Force canonical half-open bounds. Without this, [18:00,19:30] and
    -- [19:30,21:00] would overlap at the shared instant and the second booking
    -- would be rejected even though the table is genuinely free.
    CONSTRAINT reservations_during_half_open
        CHECK (NOT isempty(during)
               AND lower_inc(during) AND NOT upper_inc(during)
               AND lower(during) IS NOT NULL AND upper(during) IS NOT NULL),

    CONSTRAINT reservations_duration_sane
        CHECK (upper(during) - lower(during) BETWEEN interval '15 minutes' AND interval '8 hours'),

    -- No two live bookings may overlap on the same table. Cancelled and no_show
    -- reservations release the slot; completed ones keep blocking so history
    -- stays consistent.
    CONSTRAINT reservations_no_double_booking
        EXCLUDE USING gist (table_id WITH =, during WITH &&)
        WHERE (status IN ('pending', 'confirmed', 'seated', 'completed'))
);

COMMENT ON CONSTRAINT reservations_no_double_booking ON reservations IS
    'Database-level guarantee against double-booking; raises SQLSTATE 23P01 on conflict.';


-- -----------------------------------------------------------------------------
-- reviews
-- -----------------------------------------------------------------------------
-- A review hangs off the reservation that earned it (verified-diner model), so
-- customer_id and restaurant_id are NOT repeated here — both are reachable
-- through reservation_id, and duplicating them would allow a review whose
-- restaurant disagrees with the visit it claims to describe.
--
-- The numeric ratings stay in Postgres because the analytics aggregate over
-- them. The long-form prose lives in MongoDB; `body_ref` is the cross-store
-- pointer (a 24-hex ObjectId).
CREATE TABLE reviews (
    review_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reservation_id  bigint NOT NULL UNIQUE
                        REFERENCES reservations (reservation_id) ON DELETE CASCADE,
    rating          smallint NOT NULL,
    food_rating     smallint,
    service_rating  smallint,
    ambience_rating smallint,
    title           text,
    body_ref        text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT reviews_rating_range           CHECK (rating BETWEEN 1 AND 5),
    CONSTRAINT reviews_food_rating_range      CHECK (food_rating BETWEEN 1 AND 5),
    CONSTRAINT reviews_service_rating_range   CHECK (service_rating BETWEEN 1 AND 5),
    CONSTRAINT reviews_ambience_rating_range  CHECK (ambience_rating BETWEEN 1 AND 5),
    CONSTRAINT reviews_body_ref_objectid      CHECK (body_ref ~ '^[0-9a-f]{24}$')
);

COMMENT ON COLUMN reviews.body_ref IS
    'MongoDB ObjectId of the long-form feedback document in restaurant_feedback.reviews.';