-- verify.sql  — non-vacuous sync checks. Run against Supabase after seeding.
-- All should return ZERO rows (a returned row = a drift/dead-end bug).

-- A) CPU socket with no in-stock motherboard (dead-end)
SELECT DISTINCT c.specs->>'socket' AS socket FROM catalog c WHERE c.category='cpu'
AND NOT EXISTS (SELECT 1 FROM catalog m WHERE m.category='motherboard' AND m.in_stock
  AND m.specs->>'socket' = c.specs->>'socket');

-- B) CPU socket no cooler supports (cooler socket_compat is a JSON array)
SELECT DISTINCT c.specs->>'socket' AS socket FROM catalog c WHERE c.category='cpu'
AND NOT EXISTS (SELECT 1 FROM catalog k WHERE k.category='cooler' AND k.in_stock
  AND k.specs->'socket_compat' ? (c.specs->>'socket'));

-- C) motherboard DDR gen with no in-stock RAM
SELECT DISTINCT m.specs->>'ddr_type' AS ddr FROM catalog m WHERE m.category='motherboard' AND m.in_stock
AND NOT EXISTS (SELECT 1 FROM catalog r WHERE r.category='ram' AND r.in_stock
  AND (r.specs->>'ddr_gen') = (m.specs->>'ddr_type'));

-- D) in-stock motherboard form_factor no in-stock case supports
SELECT DISTINCT m.specs->>'form_factor' AS ff FROM catalog m WHERE m.category='motherboard' AND m.in_stock
AND NOT EXISTS (SELECT 1 FROM catalog cs WHERE cs.category='case' AND cs.in_stock
  AND cs.specs->'form_factor_support' ? (m.specs->>'form_factor'));

-- E) GPU/CPU rows (fitness-bearing categories) — count per category (sanity)
SELECT category, count(*) FROM catalog WHERE category IN ('gpu','cpu') GROUP BY category;

-- F) total + in-stock counts per category
SELECT category, count(*) total, count(*) FILTER (WHERE in_stock) in_stock FROM catalog GROUP BY category ORDER BY category;
