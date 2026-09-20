from importlib.resources import files

MIGRATIONS = {
    1: "0001_walking_skeleton.sql",
    2: "0002_evaluation_evidence.sql",
    3: "0003_framework_smoke_control_plane.sql",
    4: "0004_framework_smoke_dual_execution.sql",
    5: "0005_stage0_control_plane.sql",
    6: "0006_m1_manual_candidate_control_plane.sql",
    7: "0007_m1_hotspot_overlay_pipeline.sql",
    8: "0008_m2_search_round.sql",
    9: "0009_m2_round_authority.sql",
    10: "0010_operator_plan_preview.sql",
    11: "0011_operator_start_intent.sql",
    12: "0012_m2_formal_authority.sql",
    13: "0013_m2_formal_finalizer.sql",
    14: "0014_m2_formal_signoff_outbox.sql",
    15: "0015_m2b_agent_generation_authority.sql",
    16: "0016_m2b_runner_execution_receipt.sql",
    17: "0017_m2b_agent_evidence_read_model.sql",
    18: "0018_m2a_formal_start_intent.sql",
    19: "0019_m2b_terminal_runner_receipt_binding.sql",
    20: "0020_m2a_formal_evidence_acceptance.sql",
    21: "0021_m2a_formal_evaluation_registration.sql",
    22: "0022_endpoint_validation_control_plane.sql",
    23: "0023_endpoint_validation_failure_evidence.sql",
    24: "0024_endpoint_campaign_control_plane.sql",
    25: "0025_endpoint_adjudication_jobs.sql",
}


def migration_sql(version: int = 1) -> str:
    filename = MIGRATIONS.get(version)
    if filename is None:
        raise ValueError(f"unknown schema version: {version}")
    resource = files("hcuopt.storage.sql").joinpath(filename)
    return resource.read_text(encoding="utf-8")


def migration_plan() -> tuple[tuple[int, str], ...]:
    return tuple((version, migration_sql(version)) for version in sorted(MIGRATIONS))
