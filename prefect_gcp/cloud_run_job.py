
"""
Examples:
    Run a job using Google Cloud Run Jobs
    >>> CloudRunJob(
    >>>     image="gcr.io/my-project/my-image",
    >>>     region="us-east1",
    >>>     credentials=my_gcp_credentials
    >>> ).run()
    Run a job that runs the command `echo hello world` using Google Cloud Run Jobs
    >>> CloudRunJob(
    >>>     image="gcr.io/my-project/my-image",
    >>>     region="us-east1",
    >>>     credentials=my_gcp_credentials
    >>>     command=["echo", "hello world"]
    >>> ).run()
"""
from __future__ import annotations
import datetime
from ast import Delete
from importlib.metadata import metadata
import json
import os
import re
import sys
import time
from typing import Any, List, Literal, Optional, Union
from unicodedata import name
from uuid import uuid4
from pydantic import BaseModel, Field, root_validator, validator
from google.api_core.client_options import ClientOptions
from google.oauth2 import service_account
from googleapiclient import discovery
import googleapiclient
from prefect.blocks.core import Block
from prefect.infrastructure.base import Infrastructure, InfrastructureResult
from google.cloud import logging
from google.cloud.logging import TextEntry, ProtobufEntry
from google.cloud.logging import ASCENDING
import pytz
from prefect_gcp.credentials import GcpCredentials
from prefect.docker import get_prefect_image_name
from anyio.abc import TaskStatus
from prefect.utilities.asyncutils import run_sync_in_worker_thread, sync_compatible

class CloudRunJobResult(InfrastructureResult):
    """Result from a Cloud Run Job."""

class Job(BaseModel):
    metadata: dict
    spec: dict
    status: dict
    name: str
    ready_condition: dict
    execution_status: dict

    def _is_missing_container(self):
        """Check if Job status is not ready because the specified container cannot be found."""
        if (
            self.ready_condition.get("status") == "False" and
            self.ready_condition.get("reason") == "ContainerMissing"
        ):
            return True
        return False

    def is_ready(self) -> bool:
        """Whether a job is finished registering and ready to be executed"""
        if self._is_missing_container():
            raise Exception(f"{self.ready_condition['message']}")
        return self.ready_condition.get("status") == "True"

    def has_execution_in_progress(self) -> bool:
        """See if job has a run in progress."""
        return (
            self.execution_status == {} or
            self.execution_status.get("completionTimestamp") is None
        )

    @staticmethod
    def _get_ready_condition(job: dict) -> dict:
        if job["status"].get("conditions"):
            for condition in job["status"]["conditions"]:
                if condition["type"] == 'Ready':
                    return condition
        
        return {}

    @staticmethod 
    def _get_execution_status(job: dict):
        if job["status"].get("latestCreatedExecution"):
            return job["status"]["latestCreatedExecution"]
        
        return {}
    
    @classmethod
    def from_json(cls, job: dict):
        """Construct a Job instance from a Jobs JSON response."""

        return cls(
            metadata = job["metadata"],
            spec = job["spec"],
            status = job["status"],
            name = job["metadata"]["name"],
            ready_condition = cls._get_ready_condition(job),
            execution_status = cls._get_execution_status(job)
        )
    
class Execution(BaseModel):
    name: str
    metadata: dict
    spec: dict
    status: dict
    log_uri: str

    def is_running(self) -> bool:
        """Returns True if Execution is not completed."""
        return self.status.get("completionTime") is None

    def condition_after_completion(self):
        """Returns Execution condition if Execution has completed."""
        for condition in self.status["conditions"]:
            if condition["type"] == "Completed":
                return condition

    def succeeded(self):
        """Whether or not the Execution completed is a successful state."""
        completed_condition = self.condition_after_completion()
        if completed_condition and completed_condition["status"] == "True":
            return True
        
        return False

    @classmethod
    def from_json(cls, execution):
        """Create an Execution instance from a GCP Execution API request."""
        return cls(
            name = execution['metadata']['name'],
            metadata = execution['metadata'],
            spec = execution['spec'],
            status = execution['status'],
            log_uri = execution['status']['logUri'],
        )

class CloudRunJob(Infrastructure):
    """Infrastructure block used to run GCP Cloud Run Jobs.

    Project name information is provided by the Credentials object, and should always
    be correct as long as the Credentials object is for the correct project.
    """
    _logo_url = "https://images.ctfassets.net/gm98wzqotmnx/4CD4wwbiIKPkZDt4U3TEuW/c112fe85653da054b6d5334ef662bec4/gcp.png?h=250"  # noqa
    _block_type_name = "Cloud Run Job"

    type: Literal["cloud-run-job"] = Field(
        "cloud-run-job", description="The slug for this task type."
    )
    existing_job_name: Optional[str] = Field(
        description=(
            "The name of an existing Cloud Run Job. This value must"
            "refer to a job listed under Cloud Run Jobs. If the job"
            "does not yet exist, use the `image` field instead."
        ),

    )
    image: Optional[str] = Field(
        description=(
            "The image to use for a new Cloud Run Job. This value must"
            "refer to an image within either Google Container Registry"
            "or Google Artifact Registry."
        ),
    )

    region: str
    credentials: GcpCredentials

    # Job settings
    cpu: Optional[str] = None 
    memory: Optional[str] = None 

    args: Optional[List[str]] = None 
    env: dict[str, str] = Field(default_factory=dict)
    stream_output: bool = False

    # Cleanup behavior
    keep_job_after_completion: Optional[bool] = True

    # For private use
    _job_name: str = None
    _execution: Optional[Execution] = None
    _project_id: str = None

    @property
    def project_id(self):
        if self._project_id is None:
            self._project_id = self.credentials.get_project_id()
        
        return self._project_id

    @property
    def job_name(self):
        """Create a unique and valid job name."""

        if self._job_name is None:
            if self.existing_job_name:
                self._job_name = self.existing_job_name
            else:
                components = self.image.split("/")
                #gcr.io/<project_name>/repo/whatever
                image_name = components[2]
                modified_image_name = image_name.replace((":"),"-").replace(("."),"-") # only alphanumeric and '-' allowed
                self._job_name = f"{modified_image_name}-{uuid4()}"
        
        return self._job_name

    @validator("image")
    def remove_image_spaces(cls, value):
        """Deal with spaces in image names."""
        if value is not None:
            return value.replace(" ", "")

    @validator("existing_job_name")
    def remove_job_spaces(cls, value):
        """Deal with spaces in Job names."""
        if value is not None:
            return value.replace(" ", "")

    @root_validator
    def check_image_or_job_name(cls, values):
        if values.get("image") is None and values.get("existing_job_name") is None:
            raise ValueError("You must provide either an existing job name or a container image.")
        elif values.get("image") is not None and values.get("existing_job_name") is not None:
            raise ValueError("You can only provide one of: an existing job name or a container image name for a new job.")
        return values

    def _create_job_error(self, exc):
        """Provides a nicer error for 404s when trying to create a Cloud Run Job."""
        if exc.status_code == 404:
            raise RuntimeError(
                f"Failed to find resources at {exc.uri}. Confirm that region '{self.region}' is "
                f"the correct region for your Cloud Run Job and that {self.project_id} is the "
                "correct GCP project. If your project ID is not correct, you are using a Credentials "
                "block with permissions for the wrong project."
            ) from exc
        
        raise exc

    def _job_run_submission_error(self, exc):
        """Provides a nicer error for 404s when submitting job runs."""
        if exc.status_code == 404:
            pat1 = r"The requested URL [^ ]+ was not found on this server"
            pat2 = r"Resource '[^ ]+' of kind 'JOB' in region '[\w\-0-9]+' in project '[\w\-0-9]+' does not exist"
            if re.findall(pat1, str(exc)):
                raise RuntimeError(
                    f"Failed to find resources at {exc.uri}. Confirm that region '{self.region}' is "
                    f"the correct region for your Cloud Run Job and that {self.project_id} is the "
                    "correct GCP project. If your project ID is not correct, you are using a Credentials "
                    "block with permissions for the wrong project."
                ) from exc
            elif re.findall(pat2, str(exc)):
                raise RuntimeError(
                    f"Failed to find Job at {exc.uri}. Confirm that {self.job_name} is "
                    f"the correct Cloud Run Job name, that region '{self.region}' is the "
                    f"correct region for your Cloud Run Job, and that {self.project_id} is the "
                    "correct GCP project. If your project ID is not correct, you are using "
                    "a Credentials block with permissions for the wrong project."
                ) from exc
            else:
                raise exc
        
        raise exc
        
    @sync_compatible
    async def run(self, task_status: Optional[TaskStatus] = None):
        with self._get_jobs_client() as jobs_client:
            if self.image:
                # Cloud Run Job does not yet exist
                try:
                    self.logger.info(f"Creating Cloud Run Job {self.job_name}")
                    self._create_job(jobs_client=jobs_client, body=self._body_for_create())
                except googleapiclient.errors.HttpError as exc:
                    self._create_job_error(exc)

                try:
                    self._wait_for_job_creation(client=jobs_client)
                except Exception as exc:
                    self.logger.exception(
                        f"Encountered an exception while waiting for job run creation {exc!r}"
                        )
                    if not self.keep_job_after_completion:
                        try:
                            self.logger.info(f"Deleting Cloud Run Job {self.job_name} from Google Cloud Run.")
                            self._delete_job(jobs_client=jobs_client)
                        except Exception as exc:
                            self.logger.exception(f"Encountered an exception while attempting to delete failed Cloud Run Job.'{self.job_name}':\n{exc!r}")
                    sys.exit(1)

            try:
                self.logger.info(f"Submitting Cloud Run Job {self.job_name} for execution.")
                job_execution = Execution.from_json(self._submit_job_for_execution(jobs_client=jobs_client))

                command = ' '.join(self.command) if self.command else "'default container command'"

                self.logger.info(
                    f"Cloud Run Job {self.job_name}: Running command '{command}'"
                )
            except Exception as exc:
                self._job_run_submission_error(exc)

            if task_status:
                task_status.started(self.job_name) 

            return await run_sync_in_worker_thread(
                self._watch_job_and_get_result,
                jobs_client,
                job_execution,
                5,
            )

    def _watch_job_and_get_result(self, client, execution, poll_interval):
        try:
            job_execution = self._watch_job_execution(job_execution=execution, poll_interval=poll_interval)
        except Exception as exc:
            self.logger.exception(f"Received an unexpected exception while monitoring Cloud Run Job '{self.job_name}':\n{exc!r}")
            raise

        if job_execution.succeeded():
            status_code = 0
            self.logger.info(f"Job Run {self.job_name} completed successfully")
        else:
            status_code = 1
            self.logger.error(f"Job Run {self.job_name} did not complete successfully. {job_execution.condition_after_completion()['message']}")

        self.logger.info(f"Job Run logs can be found on GCP at: {job_execution.log_uri}") 

        if not self.keep_job_after_completion:
            try:
                self.logger.info(f"Deleting completed Cloud Run Job {self.job_name} from Google Cloud Run...")
                self._delete_job(jobs_client=client)
            except Exception as exc:
                self.logger.exception(f"Received an unexpected exception while attempting to delete completed Cloud Run Job.'{self.job_name}':\n{exc!r}")

        return CloudRunJobResult(identifier=self.job_name, status_code=status_code)

    def _body_for_create(self):
        """Create properly formatted body used for a Job CREATE request.
        See: https://cloud.google.com/run/docs/reference/rest/v1/namespaces.jobs
        """
        jobs_metadata = {
            "name": self.job_name,
            "annotations": {
                # See: https://cloud.google.com/run/docs/troubleshooting#launch-stage-validation
                "run.googleapis.com/launch-stage": "BETA"
            },
        }

        execution_template_spec_metadata = {"annotations": {}}

        # env and command here
        containers = [
            self._add_container_settings({"image": self.image})
        ]

        body = {
            "apiVersion": "run.googleapis.com/v1",
            "kind": "Job",
            "metadata": jobs_metadata,
            "spec": {  # JobSpec
                "template": {  # ExecutionTemplateSpec
                    "metadata": execution_template_spec_metadata,
                    "spec": {  # ExecutionSpec
                        "template": {  # TaskTemplateSpec
                            "spec": {  # TaskSpec
                                "containers": containers
                            }
                        }
                    }
                }
            },
        }
        return body

    def preview(self):
        """Generate a preview of the job definition that will be sent to GCP."
        """
        body = self._body_for_create()

        return json.dumps(body, indent=2)

    def _watch_job_execution(self, job_execution: Execution, poll_interval=5):
        """Update job_execution status until it is no longer running."""
        client = self._get_executions_client()

        while job_execution.is_running():
            time.sleep(poll_interval)

            job_execution = Execution.from_json(
                self._get_execution(executions_client=client, job_execution=job_execution)
            )

        return job_execution

    def _wait_for_job_creation(self, client, poll_interval=5):
        """Give created job time to register"""
        job = Job.from_json(self._get_job(jobs_client=client))
        while not job.is_ready():
            ready_condition = job.ready_condition if job.ready_condition else "waiting for condition update"
            self.logger.info(f"Job is not yet ready... Current condition: {ready_condition}")
            time.sleep(poll_interval)

            job = Job.from_json(self._get_job(jobs_client=client))

    # GCP API CALLS
    def _create_job(self, jobs_client, body):
        """Submit a create request to Cloud Run Job API."""
        request = jobs_client.create(
            parent=f"namespaces/{self.project_id}", body=body
        )
        response = request.execute()
        return response

    def _delete_job(self, jobs_client):
        """Make a delete request for the Cloud Run Job."""
        request = jobs_client.delete(name=f"namespaces/{self.project_id}/jobs/{self.job_name}")
        response = request.execute()
        return response

    def _submit_job_for_execution(self, jobs_client):
        """Submit a request to begin a new run of the Cloud Run Job."""
        request = jobs_client.run(
            name=f"namespaces/{self.project_id}/jobs/{self.job_name}"
        )
        response = request.execute()
        return response

    def _get_job(self, jobs_client):
        """Get the Job associated with the CloudRunJob."""
        request = jobs_client.get(name=f"namespaces/{self.project_id}/jobs/{self.job_name}")
        response = request.execute()
        return response

    def _get_execution(self, executions_client, job_execution):
        request = executions_client.get(
            name=f"namespaces/{job_execution.metadata['namespace']}/executions/{job_execution.metadata['name']}"
        )
        response = request.execute()
        return response

    # GCP API CLIENT
    def _get_client(self):
        """Get the base client needed for interacting with GCP APIs."""
        # region needed for 'v1' API
        api_endpoint = f"https://{self.region}-run.googleapis.com"
        credentials = self.credentials.get_credentials_from_service_account()
        options = ClientOptions(api_endpoint=api_endpoint)

        return discovery.build(
                "run", "v1", client_options=options, credentials=credentials
            ).namespaces()

    def _get_jobs_client(self):
        """Get the client needed for interacting with Cloud Run Jobs."""
        return self._get_client().jobs()

    def _get_executions_client(self):
        """Get the client needed for interacting with container executions."""
        return self._get_client().executions()

    # CONTAINER SETTINGS
    def _add_container_settings(self, d: dict) -> dict:
        """
        Add settings related to containers for Cloud Run Jobs to a dictionary.
        Includes environment variables, entrypoint command, entrypoint arguments,
        and cpu and memory limits.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        and https://cloud.google.com/run/docs/reference/rest/v1/Container#ResourceRequirements
        """
        d = self._add_env(d)
        d = self._add_resources(d)
        d = self._add_command(d)
        d = self._add_args(d)

        return d

    def _add_args(self, d: dict):
        """Set the arguments that will be passed to the entrypoint for a Cloud Run Job.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        """
        if self.args:
            d["args"] = self.args
        
        return d

    def _add_command(self, d: dict):
        """Set the command that a container will run for a Cloud Run Job.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        """
        d["command"] = self.command
        
        return d

    def _add_resources(self, d: dict):
        """Set specified resources limits for a Cloud Run Job.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container#ResourceRequirements
        """
        resources = {"limits": {}}
        if self.cpu is not None:
            resources["limits"]["cpu"] = self.cpu
        if self.memory is not None:
            resources["limits"]["memory"] = self.memory
        
        if resources["limits"]:
            d["resources"] = resources
        
        return d

    def _add_env(self, d: dict):
        """Add environment variables for a Cloud Run Job.

        Method `self._base_environment()` gets necessary Prefect environment variables
        from the config.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container#envvar for 
        how environment variables are specified for Cloud Run Jobs.
        """
        env = {**self._base_environment(), **self.env}
        cloud_run_job_env = [{"name": k, "value": v} for k,v in env.items()]
        d["env"] = cloud_run_job_env
        return d


if __name__ == "__main__":
    creds = GcpCredentials(service_account_file="creds.json")
    job = CloudRunJob(
        credentials=creds,
        region="us-east1",
        existing_job_name="crj-97ff3452-a73a-41db-8d82-21129cd92252 ",
        # image="gcr.io/helical-bongo-360018/crj",
        keep_job_after_completion=True
    )

    job.run()