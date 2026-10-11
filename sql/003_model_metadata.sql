-- Additive migration in the independent V6 instance only. Historical rows stay intact.
ALTER TABLE v6.match_centre_invocations
    ADD COLUMN candidate_id text,
    ADD COLUMN artifact_sha256 text,
    ADD COLUMN raw_model_score double precision,
    ADD COLUMN probability_status text,
    ADD COLUMN calibration_method text,
    ADD COLUMN calibration_version text,
    ADD COLUMN calibration_validation_status text,
    ADD COLUMN decision_basis text,
    ADD COLUMN decision_threshold double precision,
    ADD COLUMN decision_policy_version text;
