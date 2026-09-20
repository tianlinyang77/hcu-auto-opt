# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from hcuopt.adapters.bw20_endpoint_execution import BASELINE_MODULE_HASH
from hcuopt.adapters.profiles import ENDPOINT_FORMAL_ADJUDICATION_PROFILE
from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
from hcuopt.contracts.endpoint_adjudication_v1 import (
    EndpointAdjudicationGroupRef,
    EndpointCampaignCreate,
    EndpointCampaignSignoffRequest,
    EndpointFormalAdjudicationRequest,
    EndpointFormalAdjudicationResult,
    endpoint_adjudication_result_hash,
)
from hcuopt.contracts.endpoint_control_v1 import (
    EndpointValidationJobResult,
    EndpointValidationRunCreate,
    endpoint_create_plan_hash,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobState,
    JobType,
    LeaseScope,
    TaskState,
    WorkerType,
    WorkflowType,
)
from hcuopt.domain.errors import Conflict, NotFound
from hcuopt.domain.transitions import transition_task


class EndpointValidationRepositoryMixin:
    """Durable endpoint Run authority layered on the generic Job/Lease queue."""

    @staticmethod
    def _verify_signed_m1(connection: Any, request: EndpointValidationRunCreate) -> None:
        signed = request.signed_m1
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = %s FOR SHARE", (signed.task_id,)
        ).fetchone()
        if task is None:
            raise NotFound(f"signed M1 task not found: {signed.task_id}")
        if (
            task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value
            or task["state"] != TaskState.COMPLETED.value
            or task["target_id"] != signed.target_id
            or task["target_snapshot_id"] != signed.target_snapshot_id
            or task["automatic_release_allowed"] is not False
        ):
            raise Conflict("endpoint Run requires the completed non-releasing M1 task")
        target = connection.execute(
            "SELECT * FROM target_snapshots WHERE target_snapshot_id = %s FOR SHARE",
            (signed.target_snapshot_id,),
        ).fetchone()
        candidate = connection.execute(
            "SELECT * FROM candidates WHERE candidate_id = %s FOR SHARE",
            (signed.candidate_id,),
        ).fetchone()
        baseline = connection.execute(
            "SELECT * FROM baseline_epochs WHERE baseline_epoch_id = %s FOR SHARE",
            (signed.baseline_epoch_id,),
        ).fetchone()
        source = connection.execute(
            """
            SELECT * FROM source_snapshots
            WHERE task_id = %s AND candidate_id = %s AND kind = 'candidate'
            FOR SHARE
            """,
            (signed.task_id, signed.candidate_id),
        ).fetchone()
        artifact = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = %s FOR SHARE",
            (signed.artifact_id,),
        ).fetchone()
        evidence = connection.execute(
            "SELECT * FROM evidence_bundles WHERE evidence_id = %s FOR SHARE",
            (signed.evidence_bundle_id,),
        ).fetchone()
        signoff = connection.execute(
            "SELECT * FROM manual_candidate_signoffs WHERE signoff_id = %s FOR SHARE",
            (signed.signoff_id,),
        ).fetchone()
        if target is None or (
            target["target_id"] != signed.target_id
            or target["target_fingerprint"] != signed.target_fingerprint
        ):
            raise Conflict("signed M1 Target Snapshot identity differs")
        if candidate is None or (
            candidate["task_id"] != signed.task_id
            or candidate["baseline_epoch_id"] != signed.baseline_epoch_id
            or candidate["state"] != CandidateState.ACCEPTED.value
            or candidate["evidence_bundle_id"] != signed.evidence_bundle_id
        ):
            raise Conflict("signed M1 Candidate identity or accepted state differs")
        if baseline is None or baseline["task_id"] != signed.task_id:
            raise Conflict("signed M1 Baseline Epoch differs")
        if source is None or source["source_hash"] != signed.candidate_source_hash:
            raise Conflict("signed M1 Candidate Source Hash differs")
        if artifact is None or (
            artifact["task_id"] != signed.task_id
            or artifact["candidate_id"] != signed.candidate_id
            or artifact["content_hash"] != signed.artifact_hash
            or artifact["synthetic"]
        ):
            raise Conflict("signed M1 Artifact identity differs")
        if evidence is None or (
            evidence["task_id"] != signed.task_id
            or evidence["candidate_id"] != signed.candidate_id
            or evidence["baseline_epoch_id"] != signed.baseline_epoch_id
            or evidence["synthetic"]
        ):
            raise Conflict("signed M1 EvidenceBundle identity differs")
        if signoff is None or (
            signoff["task_id"] != signed.task_id
            or signoff["candidate_id"] != signed.candidate_id
            or signoff["evidence_bundle_id"] != signed.evidence_bundle_id
            or signoff["decision"] != "approved"
        ):
            raise Conflict("signed M1 approval identity differs")

    def create_endpoint_validation_run(
        self, request: EndpointValidationRunCreate
    ) -> dict[str, Any]:
        endpoint_run_id = uuid5(
            NAMESPACE_URL, f"hcuopt:endpoint-run:{request.idempotency_key}"
        )
        task_id = uuid5(NAMESPACE_URL, f"hcuopt:endpoint-task:{request.idempotency_key}")
        job_id = uuid5(NAMESPACE_URL, f"hcuopt:endpoint-job:{request.idempotency_key}")
        plan_hash = endpoint_create_plan_hash(request)
        request_payload = request.model_dump(mode="json")
        with self.connection() as connection:
            replay = connection.execute(
                """
                SELECT * FROM endpoint_validation_runs
                WHERE idempotency_key = %s FOR UPDATE
                """,
                (request.idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["request_payload"] != request_payload:
                    raise Conflict(
                        "endpoint idempotency_key was reused with different inputs"
                    )
                return replay
            self._verify_signed_m1(connection, request)
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s
                )
                """,
                (
                    task_id,
                    request.name,
                    request.workload.workload_id,
                    f"endpoint-task:{request.idempotency_key}",
                    TaskState.ENDPOINT_PROBING.value,
                    Jsonb(
                        {
                            "run_mode": request.plan.run_mode,
                            "acquisition_count": len(request.plan.acquisition_order),
                            "warmup_requests": request.plan.warmup_requests,
                            "measured_requests_per_acquisition": (
                                request.plan.measured_requests_per_acquisition
                            ),
                        }
                    ),
                    WorkflowType.ENDPOINT_VALIDATION.value,
                    request.workload.target_id,
                    request.signed_m1.target_snapshot_id,
                    request.adapter_profile,
                ),
            )
            payload = {
                "endpoint_run_id": str(endpoint_run_id),
                "task_id": str(task_id),
                "target_snapshot_id": str(request.signed_m1.target_snapshot_id),
                "adapter_profile": request.adapter_profile,
                "environment_fingerprint": request.environment_fingerprint,
                "signed_m1": request.signed_m1.model_dump(mode="json"),
                "workload": request.workload.model_dump(mode="json"),
                "plan": request.plan.model_dump(mode="json"),
                "plan_hash": plan_hash,
                "producer_verdict": None,
                "automatic_release_allowed": False,
            }
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    adapter_profile, lease_scope, payload, idempotency_key,
                    priority, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 1)
                """,
                (
                    job_id,
                    task_id,
                    JobType.ENDPOINT_VALIDATION.value,
                    WorkerType.GPU.value,
                    request.adapter_profile,
                    LeaseScope.EXCLUSIVE.value,
                    Jsonb(payload),
                    f"{endpoint_run_id}:provisional:v1",
                ),
            )
            row = connection.execute(
                """
                INSERT INTO endpoint_validation_runs (
                    endpoint_run_id, task_id, job_id, signed_m1_task_id,
                    target_snapshot_id, adapter_profile, environment_fingerprint,
                    workload, plan, plan_hash, state, request_payload,
                    idempotency_key, automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'queued', %s, %s, FALSE
                ) RETURNING *
                """,
                (
                    endpoint_run_id,
                    task_id,
                    job_id,
                    request.signed_m1.task_id,
                    request.signed_m1.target_snapshot_id,
                    request.adapter_profile,
                    request.environment_fingerprint,
                    Jsonb(request.workload.model_dump(mode="json")),
                    Jsonb(request.plan.model_dump(mode="json")),
                    plan_hash,
                    Jsonb(request_payload),
                    request.idempotency_key,
                ),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'endpoint_validation_created', %s)
                """,
                (
                    task_id,
                    Jsonb(
                        {
                            "endpoint_run_id": str(endpoint_run_id),
                            "job_id": str(job_id),
                            "signed_m1_task_id": str(request.signed_m1.task_id),
                            "plan_hash": plan_hash,
                            "lease_scope": LeaseScope.EXCLUSIVE.value,
                            "producer_verdict": None,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        assert row is not None
        return row

    def get_endpoint_validation_run(self, endpoint_run_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM endpoint_validation_runs WHERE endpoint_run_id = %s",
                (endpoint_run_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"endpoint validation run not found: {endpoint_run_id}")
        return row

    def endpoint_validation_summary(self, endpoint_run_id: UUID) -> dict[str, Any]:
        run = self.get_endpoint_validation_run(endpoint_run_id)
        return {
            "run": run,
            "task": self.get_task(run["task_id"]),
            "job": self.get_job(run["job_id"]),
            "events": self.list_task_events(run["task_id"]),
        }

    def create_endpoint_validation_campaign(
        self, request: EndpointCampaignCreate
    ) -> dict[str, Any]:
        campaign_id = uuid5(
            NAMESPACE_URL, f"hcuopt:endpoint-campaign:{request.idempotency_key}"
        )
        adjudication_job_id = uuid5(
            NAMESPACE_URL,
            f"hcuopt:endpoint-campaign-adjudication:{request.idempotency_key}",
        )
        with self.connection() as connection:
            replay = connection.execute(
                """
                SELECT * FROM endpoint_validation_campaigns
                WHERE idempotency_key = %s FOR UPDATE
                """,
                (request.idempotency_key,),
            ).fetchone()
            if replay is not None:
                frozen = replay["adjudication_request"]
                if (
                    replay["name"] != request.name
                    or replay["endpoint_run_ids"]
                    != list(request.endpoint_run_ids)
                    or frozen["baseline_module_hash"] != BASELINE_MODULE_HASH
                    or frozen["raw_evidence_manifest_uri"]
                    != request.raw_evidence_manifest_uri
                    or frozen["raw_evidence_manifest_sha256"]
                    != request.raw_evidence_manifest_sha256
                    or replay["adjudication_job_id"] != adjudication_job_id
                ):
                    raise Conflict(
                        "endpoint campaign idempotency_key was reused with different inputs"
                    )
                return replay

            rows = []
            for endpoint_run_id in request.endpoint_run_ids:
                row = connection.execute(
                    """
                    SELECT r.*, t.state AS task_state, j.state AS job_state,
                           j.workflow_advanced_at, j.result AS job_result
                    FROM endpoint_validation_runs AS r
                    JOIN tasks AS t ON t.task_id = r.task_id
                    JOIN jobs AS j ON j.job_id = r.job_id
                    WHERE r.endpoint_run_id = %s
                    FOR SHARE OF r, t, j
                    """,
                    (endpoint_run_id,),
                ).fetchone()
                if row is None:
                    raise NotFound(f"endpoint validation run not found: {endpoint_run_id}")
                rows.append(row)

            first = rows[0]
            signed_m1 = first["request_payload"]["signed_m1"]
            workload = first["workload"]
            plan = first["plan"]
            groups = []
            for group_ordinal, row in enumerate(rows):
                if (
                    row["state"] != "provisional_passed"
                    or row["task_state"] != TaskState.ENDPOINT_PROVISIONAL_PASSED.value
                    or row["job_state"] != JobState.SUCCEEDED.value
                    or row["workflow_advanced_at"] is None
                    or row["result"] is None
                    or row["job_result"] != row["result"]
                    or row["automatic_release_allowed"] is not False
                ):
                    raise Conflict(
                        "endpoint campaign requires eight fully advanced successful Runs"
                    )
                if any(
                    row[name] != first[name]
                    for name in (
                        "signed_m1_task_id",
                        "target_snapshot_id",
                        "adapter_profile",
                        "environment_fingerprint",
                        "workload",
                        "plan",
                        "plan_hash",
                    )
                ) or row["request_payload"]["signed_m1"] != signed_m1:
                    raise Conflict("endpoint campaign Run bindings are not identical")
                result = EndpointValidationJobResult.model_validate(row["result"])
                if result.endpoint_run_id != row["endpoint_run_id"]:
                    raise Conflict("endpoint campaign result belongs to another Run")
                groups.append(
                    EndpointAdjudicationGroupRef(
                        group_ordinal=group_ordinal,
                        endpoint_run_id=row["endpoint_run_id"],
                        plan_hash=row["plan_hash"],
                        acquisitions=result.acquisitions,
                    )
                )

            adjudication = EndpointFormalAdjudicationRequest.model_validate(
                {
                    "campaign_id": campaign_id,
                    "signed_m1": signed_m1,
                    "workload": workload,
                    "group_plan": plan,
                    "environment_fingerprint": first["environment_fingerprint"],
                    "baseline_module_hash": BASELINE_MODULE_HASH,
                    "groups": groups,
                    "raw_evidence_manifest_uri": request.raw_evidence_manifest_uri,
                    "raw_evidence_manifest_sha256": (
                        request.raw_evidence_manifest_sha256
                    ),
                    "producer_verdict": None,
                    "automatic_release_allowed": False,
                }
            )
            adjudication_payload = adjudication.model_dump(mode="json")
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    adapter_profile, lease_scope, payload, idempotency_key,
                    priority, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 1)
                """,
                (
                    adjudication_job_id,
                    first["signed_m1_task_id"],
                    JobType.ENDPOINT_ADJUDICATE.value,
                    WorkerType.EVALUATION.value,
                    ENDPOINT_FORMAL_ADJUDICATION_PROFILE,
                    LeaseScope.NONE.value,
                    Jsonb(adjudication_payload),
                    f"endpoint-campaign-adjudication:{campaign_id}:v1",
                ),
            )
            row = connection.execute(
                """
                INSERT INTO endpoint_validation_campaigns (
                    campaign_id, name, signed_m1_task_id, target_snapshot_id,
                    adapter_profile, environment_fingerprint, endpoint_run_ids,
                    adjudication_request, adjudication_job_id, state, idempotency_key,
                    automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'awaiting_adjudication', %s, FALSE
                ) RETURNING *
                """,
                (
                    campaign_id,
                    request.name,
                    first["signed_m1_task_id"],
                    first["target_snapshot_id"],
                    first["adapter_profile"],
                    first["environment_fingerprint"],
                    list(request.endpoint_run_ids),
                    Jsonb(adjudication_payload),
                    adjudication_job_id,
                    request.idempotency_key,
                ),
            ).fetchone()
        assert row is not None
        return row

    def get_endpoint_validation_campaign(self, campaign_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM endpoint_validation_campaigns
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"endpoint validation campaign not found: {campaign_id}")
        return row

    def signoff_endpoint_validation_campaign(
        self,
        campaign_id: UUID,
        request: EndpointCampaignSignoffRequest,
    ) -> dict[str, Any]:
        signoff_id = uuid5(
            NAMESPACE_URL, f"hcuopt:endpoint-campaign-signoff:{request.idempotency_key}"
        )
        with self.connection() as connection:
            replay = connection.execute(
                """
                SELECT s.*, c.state AS campaign_state
                FROM endpoint_campaign_signoffs AS s
                JOIN endpoint_validation_campaigns AS c
                  ON c.campaign_id = s.campaign_id
                WHERE s.idempotency_key = %s
                FOR UPDATE OF s, c
                """,
                (request.idempotency_key,),
            ).fetchone()
            if replay is not None:
                expected = {
                    "signoff_id": signoff_id,
                    "campaign_id": campaign_id,
                    "decision": request.decision,
                    "actor": request.actor,
                    "reason": request.reason,
                    "adjudication_result_sha256": request.adjudication_result_sha256,
                    "automatic_release_allowed": False,
                }
                if any(replay[name] != value for name, value in expected.items()):
                    raise Conflict(
                        "endpoint Campaign signoff idempotency key was reused"
                    )
                return replay

            campaign = connection.execute(
                """
                SELECT * FROM endpoint_validation_campaigns
                WHERE campaign_id = %s FOR UPDATE
                """,
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise NotFound(f"endpoint validation campaign not found: {campaign_id}")
            if campaign["state"] != "awaiting_signoff":
                raise Conflict(
                    f"endpoint Campaign cannot be signed from {campaign['state']}"
                )
            if campaign["adjudication_result"] is None:
                raise Conflict("endpoint Campaign has no formal D result")
            result_hash = endpoint_adjudication_result_hash(
                campaign["adjudication_result"]
            )
            if result_hash != request.adjudication_result_sha256:
                raise Conflict("endpoint Campaign signoff differs from the current D result")
            next_state = "completed" if request.decision == "accepted" else "rejected"
            row = connection.execute(
                """
                INSERT INTO endpoint_campaign_signoffs (
                    signoff_id, campaign_id, decision, actor, reason,
                    adjudication_result_sha256, idempotency_key,
                    automatic_release_allowed
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                RETURNING *
                """,
                (
                    signoff_id,
                    campaign_id,
                    request.decision,
                    request.actor,
                    request.reason,
                    result_hash,
                    request.idempotency_key,
                ),
            ).fetchone()
            connection.execute(
                """
                UPDATE endpoint_validation_campaigns
                SET state = %s, updated_at = now()
                WHERE campaign_id = %s
                """,
                (next_state, campaign_id),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'endpoint_campaign_signed_off', %s)
                """,
                (
                    campaign["signed_m1_task_id"],
                    Jsonb(
                        {
                            "campaign_id": str(campaign_id),
                            "signoff_id": str(signoff_id),
                            "decision": request.decision,
                            "actor": request.actor,
                            "adjudication_result_sha256": result_hash,
                            "campaign_state": next_state,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        assert row is not None
        return {**row, "campaign_state": next_state}

    def get_endpoint_campaign_signoff(
        self, campaign_id: UUID
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT s.*, c.state AS campaign_state
                FROM endpoint_campaign_signoffs AS s
                JOIN endpoint_validation_campaigns AS c
                  ON c.campaign_id = s.campaign_id
                WHERE s.campaign_id = %s
                """,
                (campaign_id,),
            ).fetchone()

    def endpoint_validation_campaign_summary(
        self, campaign_id: UUID
    ) -> dict[str, Any]:
        campaign = self.get_endpoint_validation_campaign(campaign_id)
        result_hash = (
            endpoint_adjudication_result_hash(campaign["adjudication_result"])
            if campaign["adjudication_result"] is not None
            else None
        )
        return {
            "campaign": campaign,
            "adjudication_job": (
                self.get_job(campaign["adjudication_job_id"])
                if campaign["adjudication_job_id"] is not None
                else None
            ),
            "endpoint_runs": [
                self.get_endpoint_validation_run(endpoint_run_id)
                for endpoint_run_id in campaign["endpoint_run_ids"]
            ],
            "signoff": self.get_endpoint_campaign_signoff(campaign_id),
            "adjudication_result_sha256": result_hash,
            "formal_d_adjudication": campaign["adjudication_result"] is not None,
            "automatic_release_allowed": False,
        }

    def record_endpoint_adjudication_result(
        self,
        job: dict[str, Any],
        result: EndpointFormalAdjudicationResult,
    ) -> dict[str, Any]:
        """Persist D's result only when Job, Campaign, payload, and result agree."""

        if (
            job["job_type"] != JobType.ENDPOINT_ADJUDICATE.value
            or job["state"] != JobState.SUCCEEDED.value
            or job["accepted_worker_type"] != WorkerType.EVALUATION.value
            or job["lease_scope"] != LeaseScope.NONE.value
        ):
            raise Conflict("endpoint D result requires one succeeded Evaluation Job")
        encoded = result.model_dump(mode="json")
        with self.connection() as connection:
            campaign = connection.execute(
                """
                SELECT * FROM endpoint_validation_campaigns
                WHERE campaign_id = %s FOR UPDATE
                """,
                (result.campaign_id,),
            ).fetchone()
            if campaign is None:
                raise NotFound(
                    f"endpoint validation campaign not found: {result.campaign_id}"
                )
            if (
                campaign["adjudication_job_id"] != job["job_id"]
                or job["task_id"] != campaign["signed_m1_task_id"]
                or job["payload"] != campaign["adjudication_request"]
                or str(result.campaign_id)
                != str(campaign["adjudication_request"]["campaign_id"])
                or result.automatic_release_allowed is not False
            ):
                raise Conflict("endpoint D Job differs from the frozen Campaign binding")
            if campaign["state"] in {"awaiting_signoff", "invalid"}:
                if campaign["adjudication_result"] != encoded:
                    raise Conflict("endpoint D result replay differs")
                return campaign
            if campaign["state"] != "adjudicating":
                raise Conflict(
                    f"endpoint Campaign cannot accept D result from {campaign['state']}"
                )
            next_state = (
                "invalid" if result.verdict == "invalid" else "awaiting_signoff"
            )
            row = connection.execute(
                """
                UPDATE endpoint_validation_campaigns
                SET state = %s, adjudication_result = %s, updated_at = now()
                WHERE campaign_id = %s
                RETURNING *
                """,
                (next_state, Jsonb(encoded), result.campaign_id),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'endpoint_campaign_adjudicated', %s)
                """,
                (
                    campaign["signed_m1_task_id"],
                    Jsonb(
                        {
                            "campaign_id": str(result.campaign_id),
                            "job_id": str(job["job_id"]),
                            "verdict": result.verdict,
                            "state": next_state,
                            "formal_d_adjudication": True,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        assert row is not None
        return row

    def _fail_endpoint_adjudication_after_job_failure(
        self,
        connection: Any,
        job: dict[str, Any],
        error: dict[str, Any],
    ) -> None:
        """Fail closed without manufacturing a D verdict."""

        if job["job_type"] != JobType.ENDPOINT_ADJUDICATE.value:
            return
        campaign = connection.execute(
            """
            SELECT * FROM endpoint_validation_campaigns
            WHERE adjudication_job_id = %s FOR UPDATE
            """,
            (job["job_id"],),
        ).fetchone()
        if campaign is None:
            return
        if campaign["state"] in {"awaiting_adjudication", "adjudicating"}:
            connection.execute(
                """
                UPDATE endpoint_validation_campaigns
                SET state = 'adjudication_failed', updated_at = now()
                WHERE campaign_id = %s
                """,
                (campaign["campaign_id"],),
            )
        connection.execute(
            """
            INSERT INTO task_events (task_id, event_type, details)
            VALUES (%s, 'endpoint_campaign_adjudication_failed', %s)
            """,
            (
                campaign["signed_m1_task_id"],
                Jsonb(
                    {
                        "campaign_id": str(campaign["campaign_id"]),
                        "job_id": str(job["job_id"]),
                        "error": error,
                        "verdict": None,
                        "automatic_release_allowed": False,
                    }
                ),
            ),
        )

    def record_endpoint_validation_result(
        self, job: dict[str, Any], result: EndpointValidationJobResult
    ) -> dict[str, Any]:
        if (
            job["job_type"] != JobType.ENDPOINT_VALIDATION.value
            or job["state"] != JobState.SUCCEEDED.value
            or job["lease_scope"] != LeaseScope.EXCLUSIVE.value
            or any(job.get(name) is None for name in ("lease_id", "resource_id", "fencing_token"))
        ):
            raise Conflict("endpoint evidence requires one succeeded exclusive-Lease Job")
        if not cleanup_is_healthy(result.cleanup_evidence):
            raise Conflict("endpoint evidence requires healthy fenced cleanup")
        payload = job["payload"]
        if (
            str(result.endpoint_run_id) != payload.get("endpoint_run_id")
            or result.plan_hash != payload.get("plan_hash")
            or any(
                item.profile != payload.get("adapter_profile")
                for item in result.adapter_provenance
            )
        ):
            raise Conflict("endpoint result differs from the frozen Job binding")
        with self.connection() as connection:
            run = connection.execute(
                """
                SELECT * FROM endpoint_validation_runs
                WHERE endpoint_run_id = %s FOR UPDATE
                """,
                (result.endpoint_run_id,),
            ).fetchone()
            if run is None:
                raise NotFound(f"endpoint validation run not found: {result.endpoint_run_id}")
            if run["job_id"] != job["job_id"] or run["task_id"] != job["task_id"]:
                raise Conflict("endpoint result belongs to another Run")
            encoded = result.model_dump(mode="json")
            if run["state"] == "provisional_passed":
                if run["result"] != encoded:
                    raise Conflict("endpoint result replay differs")
                return run
            if run["state"] not in {"queued", "running"}:
                raise Conflict("terminal endpoint Run cannot accept evidence")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (run["task_id"],)
            ).fetchone()
            assert task is not None
            transition_task(
                TaskState(task["state"]), TaskState.ENDPOINT_PROVISIONAL_PASSED
            )
            row = connection.execute(
                """
                UPDATE endpoint_validation_runs
                SET state = 'provisional_passed', result = %s, updated_at = now()
                WHERE endpoint_run_id = %s RETURNING *
                """,
                (Jsonb(encoded), result.endpoint_run_id),
            ).fetchone()
            connection.execute(
                """
                UPDATE tasks SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.ENDPOINT_PROVISIONAL_PASSED.value, run["task_id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'endpoint_provisional_passed', %s)
                """,
                (
                    run["task_id"],
                    Jsonb(
                        {
                            "endpoint_run_id": str(result.endpoint_run_id),
                            "job_id": str(job["job_id"]),
                            "plan_hash": result.plan_hash,
                            "producer_verdict": None,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        assert row is not None
        return row

    def _fail_endpoint_validation_run_after_job_failure(
        self,
        connection: Any,
        job: dict[str, Any],
        error: dict[str, Any],
        cleanup_evidence: dict[str, Any] | None = None,
    ) -> None:
        """Converge a terminal endpoint Job failure without inventing a verdict."""
        if job["job_type"] != JobType.ENDPOINT_VALIDATION.value:
            return
        run = connection.execute(
            """
            SELECT * FROM endpoint_validation_runs
            WHERE job_id = %s FOR UPDATE
            """,
            (job["job_id"],),
        ).fetchone()
        if run is None:
            return
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE",
            (job["task_id"],),
        ).fetchone()
        if run["state"] in {"queued", "running"}:
            connection.execute(
                """
                UPDATE endpoint_validation_runs
                SET state = 'failed', failure_error = %s, cleanup_evidence = %s,
                    updated_at = now()
                WHERE endpoint_run_id = %s
                """,
                (
                    Jsonb(error),
                    Jsonb(cleanup_evidence) if cleanup_evidence is not None else None,
                    run["endpoint_run_id"],
                ),
            )
        if task is not None and task["state"] == TaskState.ENDPOINT_PROBING.value:
            transition_task(TaskState(task["state"]), TaskState.REJECTED)
            connection.execute(
                """
                UPDATE tasks SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.REJECTED.value, job["task_id"]),
            )
        connection.execute(
            """
            INSERT INTO task_events (task_id, event_type, details)
            VALUES (%s, 'endpoint_validation_failed', %s)
            """,
            (
                job["task_id"],
                Jsonb(
                    {
                        "endpoint_run_id": str(run["endpoint_run_id"]),
                        "job_id": str(job["job_id"]),
                        "error": error,
                        "cleanup_evidence_recorded": cleanup_evidence is not None,
                        "automatic_release_allowed": False,
                    }
                ),
            ),
        )


__all__ = ["EndpointValidationRepositoryMixin"]
