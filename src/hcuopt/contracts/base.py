from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadModel(ContractModel):
    model_config = ConfigDict(extra="ignore")
