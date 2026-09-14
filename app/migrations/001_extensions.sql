-- Extensions and the immutable helpers that index expressions depend on.
--
-- Both extensions ship with the official postgres image (contrib is bundled) and the `crm`
-- role is POSTGRES_USER, therefore superuser, so CREATE EXTENSION succeeds. Verified against
-- postgres:17.6-alpine3.22.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent() is declared STABLE, not IMMUTABLE, because a superuser could in principle swap
-- the dictionary underneath it. PostgreSQL therefore refuses to let the bare function appear
-- in an index expression. Pinning the dictionary with the two-argument regdictionary form
-- removes that freedom, which is what makes an IMMUTABLE wrapper honest rather than a lie to
-- the planner.
--
-- Order matters less here than it would under a C locale: the database runs on the image
-- default (UTF8 / en_US.utf8), where lower() folds accented capitals correctly on its own.
-- unaccent is still wanted so that a search for "societa" finds "Societa" spelled with an
-- accent -- which is about half the company names in the archive.
CREATE OR REPLACE FUNCTION search_key(text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, $1)) $$;

COMMENT ON FUNCTION search_key(text) IS
    'Fold case and accents for search. IMMUTABLE so it can be indexed.';
