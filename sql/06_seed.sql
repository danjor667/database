-- =============================================================================
-- 06_seed.sql — demo dataset
-- =============================================================================
-- Sized so that Day 4's EXPLAIN ANALYZE work is meaningful: ~9-10k reservations
-- across 96 tables and 60 days, with ratings deliberately skewed per restaurant
-- so the per-city RANK() produces a real ordering rather than a tie.
--
-- setseed makes the generated data reproducible across reloads.
SELECT setseed(0.42);

TRUNCATE reviews, reservations, restaurant_cuisines, restaurant_tables,
         restaurants, cuisines, customers, cities RESTART IDENTITY CASCADE;


-- -----------------------------------------------------------------------------
-- cities
-- -----------------------------------------------------------------------------
INSERT INTO cities (name, region, country_code) VALUES
    ('New York',      'NY', 'US'),
    ('San Francisco', 'CA', 'US'),
    ('Chicago',       'IL', 'US'),
    ('Austin',        'TX', 'US');


-- -----------------------------------------------------------------------------
-- cuisines
-- -----------------------------------------------------------------------------
INSERT INTO cuisines (name) VALUES
    ('Italian'), ('Japanese'), ('Seafood'), ('New American'),
    ('Mexican'), ('Barbecue'), ('Vegetarian'), ('Cocktail Bar');


-- -----------------------------------------------------------------------------
-- restaurants
-- -----------------------------------------------------------------------------
-- ST_MakePoint takes (longitude, latitude) — the reverse of the usual spoken
-- order, and the single most common mistake in geospatial seed data.
INSERT INTO restaurants
    (name, city_id, address_line, postal_code, location, phone, email,
     price_tier, slot_minutes, menu, opening_hours)
VALUES
(
    'Vero Nord',
    (SELECT city_id FROM cities WHERE name = 'New York'),
    '18 W 21st St', '10010',
    ST_MakePoint(-73.9890, 40.7419)::geography,
    '+1-212-555-0118', 'book@veronord.example.com',
    3, 105,
    '{"currency": "USD",
      "sections": [
        {"name": "Antipasti", "items": [
          {"name": "Burrata & Peach",   "price": 18.0, "tags": ["vegetarian"]},
          {"name": "Vitello Tonnato",   "price": 21.0, "tags": []},
          {"name": "Carciofi Fritti",   "price": 16.0, "tags": ["vegan"]}]},
        {"name": "Primi", "items": [
          {"name": "Cacio e Pepe",      "price": 26.0, "tags": ["vegetarian"]},
          {"name": "Agnolotti del Plin","price": 32.0, "tags": []},
          {"name": "Tagliatelle Ragu",  "price": 34.0, "tags": []}]}]}',
    '{"mon": [], "tue": [{"open": "17:30", "close": "22:30"}],
      "wed": [{"open": "17:30", "close": "22:30"}],
      "thu": [{"open": "17:30", "close": "23:00"}],
      "fri": [{"open": "17:00", "close": "23:30"}],
      "sat": [{"open": "12:00", "close": "15:00"}, {"open": "17:00", "close": "23:30"}],
      "sun": [{"open": "12:00", "close": "21:00"}]}'
),
(
    'Ko Izakaya',
    (SELECT city_id FROM cities WHERE name = 'New York'),
    '211 E 7th St', '10009',
    ST_MakePoint(-73.9818, 40.7264)::geography,
    '+1-212-555-0244', 'hello@koizakaya.example.com',
    2, 90,
    '{"currency": "USD",
      "sections": [
        {"name": "Skewers", "items": [
          {"name": "Negima",            "price": 6.0,  "tags": []},
          {"name": "Shiitake",          "price": 5.0,  "tags": ["vegan"]},
          {"name": "Tsukune",           "price": 7.0,  "tags": []}]},
        {"name": "Small Plates", "items": [
          {"name": "Agedashi Tofu",     "price": 12.0, "tags": ["vegetarian"]},
          {"name": "Hamachi Crudo",     "price": 22.0, "tags": ["raw"]},
          {"name": "Karaage",           "price": 15.0, "tags": []}]}]}',
    '{"mon": [{"open": "18:00", "close": "23:00"}],
      "tue": [{"open": "18:00", "close": "23:00"}],
      "wed": [{"open": "18:00", "close": "23:00"}],
      "thu": [{"open": "18:00", "close": "00:00"}],
      "fri": [{"open": "18:00", "close": "00:00"}],
      "sat": [{"open": "17:00", "close": "00:00"}], "sun": []}'
),
(
    'Harbor & Vine',
    (SELECT city_id FROM cities WHERE name = 'New York'),
    '4 Water St', '10004',
    ST_MakePoint(-74.0086, 40.7061)::geography,
    '+1-212-555-0390', 'reserve@harborandvine.example.com',
    4, 120,
    '{"currency": "USD",
      "sections": [
        {"name": "Raw Bar", "items": [
          {"name": "Oysters (6)",       "price": 24.0, "tags": ["raw"]},
          {"name": "Tuna Tartare",      "price": 26.0, "tags": ["raw"]},
          {"name": "Shrimp Cocktail",   "price": 23.0, "tags": []}]},
        {"name": "Mains", "items": [
          {"name": "Whole Branzino",    "price": 58.0, "tags": []},
          {"name": "Lobster Risotto",   "price": 62.0, "tags": []},
          {"name": "Roasted Cauliflower","price": 34.0,"tags": ["vegan"]}]}]}',
    '{"mon": [], "tue": [], "wed": [{"open": "17:30", "close": "22:00"}],
      "thu": [{"open": "17:30", "close": "22:00"}],
      "fri": [{"open": "17:00", "close": "23:00"}],
      "sat": [{"open": "17:00", "close": "23:00"}],
      "sun": [{"open": "16:00", "close": "21:00"}]}'
),
(
    'Fog Line Kitchen',
    (SELECT city_id FROM cities WHERE name = 'San Francisco'),
    '340 Post St', '94108',
    ST_MakePoint(-122.4071, 37.7857)::geography,
    '+1-415-555-0155', 'table@foglinekitchen.example.com',
    3, 105,
    '{"currency": "USD",
      "sections": [
        {"name": "To Start", "items": [
          {"name": "Dungeness Toast",   "price": 22.0, "tags": []},
          {"name": "Little Gems",       "price": 17.0, "tags": ["vegetarian"]},
          {"name": "Sourdough & Butter","price": 9.0,  "tags": ["vegetarian"]}]},
        {"name": "Plates", "items": [
          {"name": "Black Cod",         "price": 42.0, "tags": []},
          {"name": "Duck Breast",       "price": 46.0, "tags": []},
          {"name": "Chanterelle Pasta", "price": 33.0, "tags": ["vegetarian"]}]}]}',
    '{"mon": [], "tue": [{"open": "17:00", "close": "22:00"}],
      "wed": [{"open": "17:00", "close": "22:00"}],
      "thu": [{"open": "17:00", "close": "22:00"}],
      "fri": [{"open": "17:00", "close": "23:00"}],
      "sat": [{"open": "11:30", "close": "14:30"}, {"open": "17:00", "close": "23:00"}],
      "sun": [{"open": "11:30", "close": "20:00"}]}'
),
(
    'La Missionera',
    (SELECT city_id FROM cities WHERE name = 'San Francisco'),
    '2895 24th St', '94110',
    ST_MakePoint(-122.4148, 37.7599)::geography,
    '+1-415-555-0281', 'hola@lamissionera.example.com',
    2, 90,
    '{"currency": "USD",
      "sections": [
        {"name": "Tacos", "items": [
          {"name": "Al Pastor",         "price": 5.5,  "tags": []},
          {"name": "Nopales",           "price": 5.0,  "tags": ["vegan"]},
          {"name": "Pescado",           "price": 6.5,  "tags": []}]},
        {"name": "Platos", "items": [
          {"name": "Mole Poblano",      "price": 27.0, "tags": []},
          {"name": "Chiles Rellenos",   "price": 23.0, "tags": ["vegetarian"]},
          {"name": "Carne Asada",       "price": 31.0, "tags": []}]}]}',
    '{"mon": [{"open": "17:00", "close": "22:00"}],
      "tue": [{"open": "17:00", "close": "22:00"}],
      "wed": [{"open": "17:00", "close": "22:00"}],
      "thu": [{"open": "17:00", "close": "22:30"}],
      "fri": [{"open": "17:00", "close": "23:30"}],
      "sat": [{"open": "12:00", "close": "23:30"}],
      "sun": [{"open": "12:00", "close": "21:00"}]}'
),
(
    'Loop & Ladle',
    (SELECT city_id FROM cities WHERE name = 'Chicago'),
    '120 N State St', '60602',
    ST_MakePoint(-87.6233, 41.8827)::geography,
    '+1-312-555-0167', 'reserve@loopandladle.example.com',
    2, 90,
    '{"currency": "USD",
      "sections": [
        {"name": "Bowls", "items": [
          {"name": "Wisconsin Cheddar Soup","price": 12.0, "tags": ["vegetarian"]},
          {"name": "Short Rib Ramen",   "price": 21.0, "tags": []},
          {"name": "Red Lentil Dal",    "price": 16.0, "tags": ["vegan"]}]},
        {"name": "Sides", "items": [
          {"name": "Cornbread",         "price": 8.0,  "tags": ["vegetarian"]},
          {"name": "Charred Broccolini","price": 11.0, "tags": ["vegan"]},
          {"name": "Fries",             "price": 9.0,  "tags": ["vegetarian"]}]}]}',
    '{"mon": [{"open": "11:00", "close": "21:00"}],
      "tue": [{"open": "11:00", "close": "21:00"}],
      "wed": [{"open": "11:00", "close": "21:00"}],
      "thu": [{"open": "11:00", "close": "22:00"}],
      "fri": [{"open": "11:00", "close": "22:30"}],
      "sat": [{"open": "10:00", "close": "22:30"}],
      "sun": [{"open": "10:00", "close": "20:00"}]}'
),
(
    'Pilsen Fire',
    (SELECT city_id FROM cities WHERE name = 'Chicago'),
    '1741 W 18th St', '60608',
    ST_MakePoint(-87.6565, 41.8565)::geography,
    '+1-312-555-0492', 'info@pilsenfire.example.com',
    3, 120,
    '{"currency": "USD",
      "sections": [
        {"name": "From the Hearth", "items": [
          {"name": "Wood-Fired Octopus","price": 28.0, "tags": []},
          {"name": "Charred Cabbage",  "price": 19.0, "tags": ["vegan"]},
          {"name": "Bone Marrow",      "price": 24.0, "tags": []}]},
        {"name": "Mains", "items": [
          {"name": "Ribeye for Two",   "price": 96.0, "tags": []},
          {"name": "Ember Trout",      "price": 38.0, "tags": []},
          {"name": "Squash Barbacoa",  "price": 29.0, "tags": ["vegan"]}]}]}',
    '{"mon": [], "tue": [], "wed": [{"open": "17:00", "close": "22:00"}],
      "thu": [{"open": "17:00", "close": "22:00"}],
      "fri": [{"open": "17:00", "close": "23:00"}],
      "sat": [{"open": "17:00", "close": "23:00"}],
      "sun": [{"open": "16:00", "close": "21:00"}]}'
),
(
    'Rainey Smokehouse',
    (SELECT city_id FROM cities WHERE name = 'Austin'),
    '82 Rainey St', '78701',
    ST_MakePoint(-97.7386, 30.2578)::geography,
    '+1-512-555-0133', 'howdy@raineysmokehouse.example.com',
    2, 90,
    '{"currency": "USD",
      "sections": [
        {"name": "By the Pound", "items": [
          {"name": "Brisket",          "price": 32.0, "tags": []},
          {"name": "Pork Ribs",        "price": 28.0, "tags": []},
          {"name": "Smoked Turkey",    "price": 24.0, "tags": []}]},
        {"name": "Sides", "items": [
          {"name": "Mac & Cheese",     "price": 8.0,  "tags": ["vegetarian"]},
          {"name": "Collards",         "price": 7.0,  "tags": []},
          {"name": "Smoked Carrots",   "price": 9.0,  "tags": ["vegan"]}]}]}',
    '{"mon": [], "tue": [{"open": "11:00", "close": "20:00"}],
      "wed": [{"open": "11:00", "close": "20:00"}],
      "thu": [{"open": "11:00", "close": "21:00"}],
      "fri": [{"open": "11:00", "close": "22:00"}],
      "sat": [{"open": "11:00", "close": "22:00"}],
      "sun": [{"open": "11:00", "close": "18:00"}]}'
);


-- -----------------------------------------------------------------------------
-- restaurant_cuisines
-- -----------------------------------------------------------------------------
INSERT INTO restaurant_cuisines (restaurant_id, cuisine_id, is_primary)
SELECT r.restaurant_id, c.cuisine_id, m.is_primary
  FROM (VALUES
        ('Vero Nord',         'Italian',      true),
        ('Vero Nord',         'Vegetarian',   false),
        ('Ko Izakaya',        'Japanese',     true),
        ('Ko Izakaya',        'Cocktail Bar', false),
        ('Harbor & Vine',     'Seafood',      true),
        ('Harbor & Vine',     'New American', false),
        ('Fog Line Kitchen',  'New American', true),
        ('Fog Line Kitchen',  'Seafood',      false),
        ('La Missionera',     'Mexican',      true),
        ('La Missionera',     'Vegetarian',   false),
        ('Loop & Ladle',      'New American', true),
        ('Loop & Ladle',      'Vegetarian',   false),
        ('Pilsen Fire',       'New American', true),
        ('Pilsen Fire',       'Barbecue',     false),
        ('Rainey Smokehouse', 'Barbecue',     true),
        ('Rainey Smokehouse', 'New American', false)
       ) AS m(restaurant_name, cuisine_name, is_primary)
  JOIN restaurants r ON r.name = m.restaurant_name
  JOIN cuisines    c ON c.name = m.cuisine_name;


-- -----------------------------------------------------------------------------
-- restaurant_tables — 12 tables per restaurant, identical floor plan
-- -----------------------------------------------------------------------------
-- min_party_size scales with the table so the availability query has to make a
-- real choice instead of always landing on the largest table.
INSERT INTO restaurant_tables (restaurant_id, table_code, seats, min_party_size, zone)
SELECT r.restaurant_id,
       'T' || lpad(p.idx::text, 2, '0'),
       p.seats,
       greatest(1, p.seats - 3)::smallint,
       p.zone::table_zone
  FROM restaurants r
 CROSS JOIN (VALUES
        (1,  2::smallint, 'bar'),
        (2,  2::smallint, 'bar'),
        (3,  2::smallint, 'indoor'),
        (4,  4::smallint, 'indoor'),
        (5,  4::smallint, 'indoor'),
        (6,  4::smallint, 'patio'),
        (7,  4::smallint, 'patio'),
        (8,  6::smallint, 'indoor'),
        (9,  6::smallint, 'indoor'),
        (10, 6::smallint, 'patio'),
        (11, 8::smallint, 'private'),
        (12, 10::smallint,'private')
       ) AS p(idx, seats, zone);


-- -----------------------------------------------------------------------------
-- customers — 60 synthetic diners
-- -----------------------------------------------------------------------------
INSERT INTO customers (first_name, last_name, email, phone)
SELECT n.first_name,
       n.last_name,
       lower(n.first_name || '.' || n.last_name || g || '@example.com'),
       '+1-555-' || lpad(g::text, 4, '0')
  FROM generate_series(1, 60) AS g
 CROSS JOIN LATERAL (
        SELECT (ARRAY['Ava','Noah','Mia','Liam','Zoe','Ethan','Iris','Omar',
                      'Lena','Kai','Nina','Jonas','Priya','Diego','Yuki','Sam',
                      'Rosa','Theo','Amara','Felix'])[1 + (g * 7) % 20]  AS first_name,
               (ARRAY['Nguyen','Patel','Okafor','Silva','Kim','Rossi','Haddad','Novak',
                      'Mensah','Tanaka','Alvarez','Fischer','Dubois','Ivanov','Costa','Reyes',
                      'Bauer','Moreau','Larsen','Osei'])[1 + (g * 11) % 20] AS last_name
       ) AS n;


-- -----------------------------------------------------------------------------
-- reservations — 60 days x 3 slots per table, ~55% booked
-- -----------------------------------------------------------------------------
-- Slots start at 17:00 / 19:00 / 21:00 and run for the restaurant's own
-- slot_minutes (90-120), so consecutive bookings abut but never overlap — which
-- is exactly the case the half-open range bounds exist to allow.
WITH cust AS (
    SELECT array_agg(customer_id ORDER BY customer_id) AS ids FROM customers
),
slots AS (
    SELECT rt.table_id,
           rt.min_party_size,
           rt.seats,
           tstzrange(x.ts, x.ts + make_interval(mins => r.slot_minutes), '[)') AS during
      FROM restaurant_tables rt
      JOIN restaurants r USING (restaurant_id)
     CROSS JOIN generate_series(-45, 14) AS d(offset_days)
     CROSS JOIN (VALUES (time '17:00'), (time '19:00'), (time '21:00')) AS s(slot)
     CROSS JOIN LATERAL (
            SELECT ((current_date + d.offset_days) + s.slot)::timestamptz AS ts
           ) AS x
     WHERE random() < 0.55
)
INSERT INTO reservations (table_id, customer_id, during, party_size, status)
SELECT s.table_id,
       c.ids[1 + floor(random() * array_length(c.ids, 1))::int],
       s.during,
       (s.min_party_size + floor(random() * (s.seats - s.min_party_size + 1)))::smallint,
       CASE
           WHEN upper(s.during) <= now() THEN
               CASE WHEN random() < 0.85 THEN 'completed'
                    WHEN random() < 0.60 THEN 'cancelled'
                    ELSE 'no_show'
               END
           ELSE
               CASE WHEN random() < 0.80 THEN 'confirmed' ELSE 'pending' END
       END::reservation_status
  FROM slots s
 CROSS JOIN cust c;


-- -----------------------------------------------------------------------------
-- reviews — ~45% of completed visits
-- -----------------------------------------------------------------------------
-- Each restaurant gets a hidden quality baseline derived from its id, so the
-- per-city RANK() in Day 4 has a stable, non-tied ordering to discover.
-- body_ref is a fabricated ObjectId: valid in format, pointing at nothing. Run
-- scripts/backfill_feedback.py to write the documents and repoint these, or
-- app.feedback.reconcile() will report every one of them as dangling.
INSERT INTO reviews (reservation_id, rating, food_rating, service_rating,
                     ambience_rating, title, body_ref, created_at)
SELECT res.reservation_id,
       sc.rating,
       greatest(1, least(5, sc.rating + (floor(random() * 3) - 1)))::smallint,
       greatest(1, least(5, sc.rating + (floor(random() * 3) - 1)))::smallint,
       greatest(1, least(5, sc.rating + (floor(random() * 3) - 1)))::smallint,
       (ARRAY['Not for me',
              'Some rough edges',
              'Solid, would return',
              'Really lovely evening',
              'Best meal of the year'])[sc.rating],
       substr(md5(random()::text || res.reservation_id::text), 1, 24),
       least(now(), upper(res.during) + (random() * 5) * interval '1 day')
  FROM reservations res
  JOIN restaurant_tables rt USING (table_id)
 CROSS JOIN LATERAL (
        SELECT greatest(1, least(5,
                   round((3.1 + (rt.restaurant_id % 4) * 0.45
                              + (random() - 0.5) * 1.6)::numeric)
               ))::smallint AS rating
       ) AS sc
 WHERE res.status = 'completed'
   AND random() < 0.45;


-- -----------------------------------------------------------------------------
-- Load summary
-- -----------------------------------------------------------------------------
ANALYZE;

SELECT 'cities'              AS relation, count(*) FROM cities
UNION ALL SELECT 'cuisines',            count(*) FROM cuisines
UNION ALL SELECT 'restaurants',         count(*) FROM restaurants
UNION ALL SELECT 'restaurant_cuisines', count(*) FROM restaurant_cuisines
UNION ALL SELECT 'restaurant_tables',   count(*) FROM restaurant_tables
UNION ALL SELECT 'customers',           count(*) FROM customers
UNION ALL SELECT 'reservations',        count(*) FROM reservations
UNION ALL SELECT 'reviews',             count(*) FROM reviews;