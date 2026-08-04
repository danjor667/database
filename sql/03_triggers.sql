-- =============================================================================
-- 03_triggers.sql — invariants that a CHECK constraint cannot express
-- =============================================================================
-- A CHECK may only reference columns of its own row, so any rule spanning two
-- tables (party size vs. table capacity, review vs. reservation state) has to
-- be a trigger.

-- -----------------------------------------------------------------------------
-- updated_at maintenance
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER restaurants_touch
    BEFORE UPDATE ON restaurants
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE OR REPLACE TRIGGER customers_touch
    BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE OR REPLACE TRIGGER reservations_touch
    BEFORE UPDATE ON reservations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE OR REPLACE TRIGGER reviews_touch
    BEFORE UPDATE ON reviews
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- -----------------------------------------------------------------------------
-- reservations: capacity and bookability
-- -----------------------------------------------------------------------------
-- Guarantees a party physically fits the table it was assigned, and that new
-- bookings only land on tables that are actually open for business. The
-- bookability half is INSERT-only so that deactivating a table or restaurant
-- does not freeze the existing reservations (they still need to be cancellable
-- and completable).
CREATE OR REPLACE FUNCTION reservations_validate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    t record;
BEGIN
    SELECT rt.seats,
           rt.min_party_size,
           rt.is_active AS table_active,
           r.is_active  AS restaurant_active
      INTO t
      FROM restaurant_tables rt
      JOIN restaurants r USING (restaurant_id)
     WHERE rt.table_id = NEW.table_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'table_id % does not exist', NEW.table_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF NEW.party_size > t.seats THEN
        RAISE EXCEPTION 'party of % exceeds the % seat(s) at table_id %',
                        NEW.party_size, t.seats, NEW.table_id
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.party_size < t.min_party_size THEN
        RAISE EXCEPTION 'party of % is below the minimum of % for table_id %',
                        NEW.party_size, t.min_party_size, NEW.table_id
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'INSERT' AND NOT (t.table_active AND t.restaurant_active) THEN
        RAISE EXCEPTION 'table_id % is not bookable (table active: %, restaurant active: %)',
                        NEW.table_id, t.table_active, t.restaurant_active
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER reservations_validate
    BEFORE INSERT OR UPDATE OF table_id, party_size ON reservations
    FOR EACH ROW EXECUTE FUNCTION reservations_validate();


-- -----------------------------------------------------------------------------
-- reviews: only verified, finished visits
-- -----------------------------------------------------------------------------
-- Keeps the ratings feeding the per-city ranking trustworthy: you cannot review
-- a cancelled booking, a no-show, or a dinner that has not happened yet.
CREATE OR REPLACE FUNCTION reviews_validate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r record;
BEGIN
    SELECT status, during
      INTO r
      FROM reservations
     WHERE reservation_id = NEW.reservation_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'reservation_id % does not exist', NEW.reservation_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF r.status <> 'completed' THEN
        RAISE EXCEPTION 'cannot review reservation % with status %',
                        NEW.reservation_id, r.status
            USING ERRCODE = 'check_violation';
    END IF;

    IF upper(r.during) > now() THEN
        RAISE EXCEPTION 'cannot review reservation % before it ends (ends at %)',
                        NEW.reservation_id, upper(r.during)
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER reviews_validate
    BEFORE INSERT OR UPDATE OF reservation_id ON reviews
    FOR EACH ROW EXECUTE FUNCTION reviews_validate();