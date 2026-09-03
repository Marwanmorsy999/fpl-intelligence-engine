-- === MIGRATION 0024_supabase_perf_evidence UPGRADE ===
BEGIN;

CREATE UNIQUE INDEX predictions_current_pk_idx ON public.predictions_current ("gameweek", "element_id");
ALTER TABLE public.predictions_current DROP CONSTRAINT uq_pred_current_gw_element;
ALTER TABLE public.predictions_current ADD CONSTRAINT predictions_current_pkey PRIMARY KEY USING INDEX predictions_current_pk_idx;
CREATE INDEX predictions_current_computed_at_idx ON public.predictions_current ("computed_at");
CREATE INDEX ix_availability_events_primary_source_id ON public.availability_events ("primary_source_id");

COMMIT;

-- === MIGRATION 0024_supabase_perf_evidence DOWNGRADE ===
BEGIN;
ALTER TABLE public.predictions_current DROP CONSTRAINT predictions_current_pkey;
ALTER TABLE public.predictions_current ADD CONSTRAINT uq_pred_current_gw_element UNIQUE ("gameweek", "element_id");
DROP INDEX IF EXISTS public.ix_availability_events_primary_source_id;
DROP INDEX IF EXISTS public.predictions_current_computed_at_idx;
COMMIT;
