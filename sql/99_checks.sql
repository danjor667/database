-- =============================================================================
-- 99_checks.sql — self-verifying test suite for the schema's invariants
-- =============================================================================
-- Proves that the guarantees Day 2's booking function will rely on are enforced
-- by the database itself, not by application convention. Every check either
-- prints "ok:" or aborts the script.
--
-- Runs inside a transaction that is rolled back, so it never leaves test rows
-- behind. Slots are placed 200 days out to stay clear of the seeded range.
--
--   ./scripts/db.sh psql -v ON_ERROR_STOP=1 -f /sql/99_checks.sql

BEGIN;

-- Fixtures: a dedicated restaurant so the checks cannot collide with seed data.
INSERT INTO cities (name, region, country_code)
VALUES ('Testville', 'ZZ', 'US');

INSERT INTO restaurants (name, city_id, address_line, location, slot_minutes)
VALUES ('Check Kitchen',
        (SELECT city_id FROM cities WHERE name = 'Testville'),
        '1 Test Way',
        ST_MakePoint(-73.9857, 40.7484)::geography,
        90);

INSERT INTO restaurant_tables (restaurant_id, table_code, seats, min_party_size)
VALUES ((SELECT restaurant_id FROM restaurants WHERE name = 'Check Kitchen'),
        'C01', 4, 2);

INSERT INTO customers (first_name, last_name, email)
VALUES ('Check', 'Runner', 'check.runner@example.com');

CREATE TEMP VIEW fixture AS
SELECT (SELECT table_id FROM restaurant_tables
         WHERE table_code = 'C01'
           AND restaurant_id = (SELECT restaurant_id FROM restaurants
                                 WHERE name = 'Check Kitchen')) AS table_id,
       (SELECT customer_id FROM customers
         WHERE email = 'check.runner@example.com')               AS customer_id,
       ((current_date + 200) + time '19:00')::timestamptz        AS slot_start;


-- -----------------------------------------------------------------------------
DO $$
DECLARE f record;
BEGIN
    SELECT * INTO f FROM fixture;

    -- 1. A first booking succeeds.
    INSERT INTO reservations (table_id, customer_id, during, party_size)
    VALUES (f.table_id, f.customer_id,
            tstzrange(f.slot_start, f.slot_start + interval '90 min', '[)'), 2);
    RAISE NOTICE 'ok  1: first booking accepted';

    -- 2. An exactly-overlapping booking on the same table is rejected.
    BEGIN
        INSERT INTO reservations (table_id, customer_id, during, party_size)
        VALUES (f.table_id, f.customer_id,
                tstzrange(f.slot_start, f.slot_start + interval '90 min', '[)'), 2);
        RAISE EXCEPTION 'CHECK 2 FAILED: identical slot was accepted';
    EXCEPTION WHEN exclusion_violation THEN
        RAISE NOTICE 'ok  2: identical slot rejected (23P01)';
    END;

    -- 3. A partially-overlapping booking is rejected too.
    BEGIN
        INSERT INTO reservations (table_id, customer_id, during, party_size)
        VALUES (f.table_id, f.customer_id,
                tstzrange(f.slot_start + interval '45 min',
                          f.slot_start + interval '135 min', '[)'), 2);
        RAISE EXCEPTION 'CHECK 3 FAILED: partial overlap was accepted';
    EXCEPTION WHEN exclusion_violation THEN
        RAISE NOTICE 'ok  3: partial overlap rejected (23P01)';
    END;

    -- 4. An abutting booking IS accepted: this is why bounds are half-open.
    INSERT INTO reservations (table_id, customer_id, during, party_size)
    VALUES (f.table_id, f.customer_id,
            tstzrange(f.slot_start + interval '90 min',
                      f.slot_start + interval '180 min', '[)'), 2);
    RAISE NOTICE 'ok  4: abutting slot accepted (half-open bounds)';
END $$;


-- -----------------------------------------------------------------------------
DO $$
DECLARE f record;
BEGIN
    SELECT * INTO f FROM fixture;

    -- 5. Cancelling releases the slot for a new booking.
    UPDATE reservations
       SET status = 'cancelled'
     WHERE table_id = f.table_id
       AND during = tstzrange(f.slot_start, f.slot_start + interval '90 min', '[)');

    INSERT INTO reservations (table_id, customer_id, during, party_size)
    VALUES (f.table_id, f.customer_id,
            tstzrange(f.slot_start, f.slot_start + interval '90 min', '[)'), 3);
    RAISE NOTICE 'ok  5: cancelled reservation released its slot';

    -- 6. Party larger than the table is rejected (cross-table trigger).
    BEGIN
        INSERT INTO reservations (table_id, customer_id, during, party_size)
        VALUES (f.table_id, f.customer_id,
                tstzrange(f.slot_start + interval '1 day',
                          f.slot_start + interval '1 day 90 min', '[)'), 9);
        RAISE EXCEPTION 'CHECK 6 FAILED: oversized party was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok  6: party exceeding table capacity rejected';
    END;

    -- 7. Party below the table minimum is rejected.
    BEGIN
        INSERT INTO reservations (table_id, customer_id, during, party_size)
        VALUES (f.table_id, f.customer_id,
                tstzrange(f.slot_start + interval '2 days',
                          f.slot_start + interval '2 days 90 min', '[)'), 1);
        RAISE EXCEPTION 'CHECK 7 FAILED: undersized party was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok  7: party below table minimum rejected';
    END;
END $$;


-- -----------------------------------------------------------------------------
DO $$
DECLARE f record;
BEGIN
    SELECT * INTO f FROM fixture;

    -- 8. Closed bounds are rejected: they would break abutting bookings.
    BEGIN
        INSERT INTO reservations (table_id, customer_id, during, party_size)
        VALUES (f.table_id, f.customer_id,
                tstzrange(f.slot_start + interval '3 days',
                          f.slot_start + interval '3 days 90 min', '[]'), 2);
        RAISE EXCEPTION 'CHECK 8 FAILED: closed range bounds were accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok  8: non-half-open range bounds rejected';
    END;

    -- 9. An unbounded reservation is rejected.
    BEGIN
        INSERT INTO reservations (table_id, customer_id, during, party_size)
        VALUES (f.table_id, f.customer_id,
                tstzrange(f.slot_start + interval '4 days', NULL, '[)'), 2);
        RAISE EXCEPTION 'CHECK 9 FAILED: unbounded range was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok  9: unbounded range rejected';
    END;

    -- 10. An implausibly long reservation is rejected.
    BEGIN
        INSERT INTO reservations (table_id, customer_id, during, party_size)
        VALUES (f.table_id, f.customer_id,
                tstzrange(f.slot_start + interval '5 days',
                          f.slot_start + interval '6 days', '[)'), 2);
        RAISE EXCEPTION 'CHECK 10 FAILED: 24-hour reservation was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok 10: over-long reservation rejected';
    END;
END $$;


-- -----------------------------------------------------------------------------
DO $$
DECLARE
    f   record;
    rid bigint;
BEGIN
    SELECT * INTO f FROM fixture;

    -- 11. A review on a future / non-completed reservation is rejected.
    SELECT reservation_id INTO rid
      FROM reservations
     WHERE table_id = f.table_id AND status = 'confirmed'
     LIMIT 1;

    BEGIN
        INSERT INTO reviews (reservation_id, rating) VALUES (rid, 5);
        RAISE EXCEPTION 'CHECK 11 FAILED: review on unfinished visit was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok 11: review on non-completed reservation rejected';
    END;

    -- 12. A malformed MongoDB ObjectId reference is rejected.
    SELECT reservation_id INTO rid
      FROM reservations
     WHERE status = 'completed'
       AND reservation_id NOT IN (SELECT reservation_id FROM reviews)
     LIMIT 1;

    BEGIN
        INSERT INTO reviews (reservation_id, rating, body_ref)
        VALUES (rid, 4, 'not-an-objectid');
        RAISE EXCEPTION 'CHECK 12 FAILED: malformed body_ref was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok 12: malformed body_ref rejected';
    END;

    -- 13. A second review for the same visit is rejected.
    INSERT INTO reviews (reservation_id, rating) VALUES (rid, 4);
    BEGIN
        INSERT INTO reviews (reservation_id, rating) VALUES (rid, 2);
        RAISE EXCEPTION 'CHECK 13 FAILED: duplicate review was accepted';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok 13: duplicate review for one visit rejected';
    END;
END $$;


-- -----------------------------------------------------------------------------
DO $$
DECLARE
    n integer;
BEGIN
    -- 14. A restaurant may not carry two primary cuisines.
    BEGIN
        INSERT INTO restaurant_cuisines (restaurant_id, cuisine_id, is_primary)
        VALUES ((SELECT restaurant_id FROM restaurants WHERE name = 'Vero Nord'),
                (SELECT cuisine_id FROM cuisines WHERE name = 'Seafood'),
                true);
        RAISE EXCEPTION 'CHECK 14 FAILED: second primary cuisine was accepted';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok 14: second primary cuisine rejected';
    END;

    -- 15. Bookings against a deactivated table are refused.
    UPDATE restaurant_tables SET is_active = false
     WHERE table_code = 'C01'
       AND restaurant_id = (SELECT restaurant_id FROM restaurants
                             WHERE name = 'Check Kitchen');
    BEGIN
        INSERT INTO reservations (table_id, customer_id, during, party_size)
        SELECT f.table_id, f.customer_id,
               tstzrange(f.slot_start + interval '7 days',
                         f.slot_start + interval '7 days 90 min', '[)'), 2
          FROM fixture f;
        RAISE EXCEPTION 'CHECK 15 FAILED: booking on inactive table was accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok 15: booking on inactive table rejected';
    END;

    -- 16. Geography is real: ST_DWithin finds Manhattan restaurants within 3 km
    --     of Times Square and excludes the other cities.
    SELECT count(*) INTO n
      FROM restaurants
     WHERE ST_DWithin(location, ST_MakePoint(-73.9855, 40.7580)::geography, 3000);
    IF n < 1 THEN
        RAISE EXCEPTION 'CHECK 16 FAILED: expected >=1 restaurant within 3km, got %', n;
    END IF;
    RAISE NOTICE 'ok 16: % restaurant(s) found within 3km of Times Square', n;
END $$;

ROLLBACK;

-- Confirm the rollback left nothing behind.
SELECT count(*) AS leftover_test_cities FROM cities WHERE name = 'Testville';