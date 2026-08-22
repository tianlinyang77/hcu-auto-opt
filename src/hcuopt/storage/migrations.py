from importlib.resources import files

MIGRATIONS = {
    1: "0001_walking_skeleton.sql",
    2: "0002_evaluation_evidence.sql",
    3: "0003_framework_smoke_control_plane.sql",
    4: "0004_framework_smoke_dual_execution.sql",
    5: "0005_stage0_control_plane.sql",
    6: "0006_m1_manual_candidate_control_plane.sql",
}


def migration_sql(version: int = 1) -> str:
    filename = MIGRATIONS.get(version)
    if filename is None:
        raise ValueError(f"unknown schema version: {version}")
    resource = files("hcuopt.storage.sql").joinpath(filename)
    return resource.read_text(encoding="utf-8")


def migration_plan() -> tuple[tuple[int, str], ...]:
    return tuple((version, migration_sql(version)) for version in sorted(MIGRATIONS))
