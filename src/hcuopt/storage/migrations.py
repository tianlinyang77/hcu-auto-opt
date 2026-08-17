from importlib.resources import files

MIGRATIONS = {
    1: "0001_walking_skeleton.sql",
    2: "0002_evaluation_evidence.sql",
}


def migration_sql(version: int = 1) -> str:
    filename = MIGRATIONS.get(version)
    if filename is None:
        raise ValueError(f"unknown schema version: {version}")
    resource = files("hcuopt.storage.sql").joinpath(filename)
    return resource.read_text(encoding="utf-8")


def migration_plan() -> tuple[tuple[int, str], ...]:
    return tuple((version, migration_sql(version)) for version in sorted(MIGRATIONS))
