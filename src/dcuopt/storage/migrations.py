from importlib.resources import files


def migration_sql(version: int = 1) -> str:
    if version != 1:
        raise ValueError(f"unknown schema version: {version}")
    resource = files("dcuopt.storage.sql").joinpath("0001_walking_skeleton.sql")
    return resource.read_text(encoding="utf-8")
