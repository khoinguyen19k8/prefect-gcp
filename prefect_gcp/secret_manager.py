from functools import partial
from typing import TYPE_CHECKING, Any, Optional, Union

from anyio import to_thread
from google.cloud.secretmanager_v1.types.resources import Secret, SecretPayload
from google.cloud.secretmanager_v1.types.service import (
    AccessSecretVersionRequest,
    AddSecretVersionRequest,
    CreateSecretRequest,
    DeleteSecretRequest,
)
from prefect import get_run_logger, task
from prefect.blocks.abstract import SecretBlock
from prefect.utilities.asyncutils import run_sync_in_worker_thread
from pydantic import Field

if TYPE_CHECKING:
    from prefect_gcp.credentials import GcpCredentials


@task
async def create_secret(
    secret_name: str,
    gcp_credentials: "GcpCredentials",
    timeout: float = 60,
    project: Optional[str] = None,
) -> str:
    """
    Creates a secret in Google Cloud Platform's Secret Manager.

    Args:
        secret_name: Name of the secret to retrieve.
        gcp_credentials: Credentials to use for authentication with GCP.
        timeout: The number of seconds the transport should wait
            for the server response.
        project: Name of the project to use; overrides the
            gcp_credentials project if provided.

    Returns:
        The path of the created secret.

    Example:
        ```python
        from prefect import flow
        from prefect_gcp import GcpCredentials
        from prefect_gcp.secret_manager import create_secret

        @flow()
        def example_cloud_storage_create_secret_flow():
            gcp_credentials = GcpCredentials(project="project")
            secret_path = create_secret("secret_name", gcp_credentials)
            return secret_path

        example_cloud_storage_create_secret_flow()
        ```
    """
    logger = get_run_logger()
    logger.info("Creating the %s secret", secret_name)

    client = gcp_credentials.get_secret_manager_client()
    project = project or gcp_credentials.project

    parent = f"projects/{project}"
    secret_settings = {"replication": {"automatic": {}}}

    partial_create = partial(
        client.create_secret,
        parent=parent,
        secret_id=secret_name,
        secret=secret_settings,
        timeout=timeout,
    )
    response = await to_thread.run_sync(partial_create)
    return response.name


@task
async def update_secret(
    secret_name: str,
    secret_data: Union[str, bytes],
    gcp_credentials: "GcpCredentials",
    timeout: float = 60,
    project: Optional[str] = None,
) -> str:
    """
    Updates a secret in Google Cloud Platform's Secret Manager.

    Args:
        secret_name: Name of the secret to retrieve.
        secret_data: Desired value of the secret. Can be either `str` or `bytes`.
        gcp_credentials: Credentials to use for authentication with GCP.
        timeout: The number of seconds the transport should wait
            for the server response.
        project: Name of the project to use; overrides the
            gcp_credentials project if provided.

    Returns:
        The path of the updated secret.

    Example:
        ```python
        from prefect import flow
        from prefect_gcp import GcpCredentials
        from prefect_gcp.secret_manager import update_secret

        @flow()
        def example_cloud_storage_update_secret_flow():
            gcp_credentials = GcpCredentials(project="project")
            secret_path = update_secret("secret_name", "secret_data", gcp_credentials)
            return secret_path

        example_cloud_storage_update_secret_flow()
        ```
    """
    logger = get_run_logger()
    logger.info("Updating the %s secret", secret_name)

    client = gcp_credentials.get_secret_manager_client()
    project = project or gcp_credentials.project

    parent = f"projects/{project}/secrets/{secret_name}"
    if isinstance(secret_data, str):
        secret_data = secret_data.encode("UTF-8")
    partial_add = partial(
        client.add_secret_version,
        parent=parent,
        payload={"data": secret_data},
        timeout=timeout,
    )
    response = await to_thread.run_sync(partial_add)
    return response.name


@task
async def read_secret(
    secret_name: str,
    gcp_credentials: "GcpCredentials",
    version_id: Union[str, int] = "latest",
    timeout: float = 60,
    project: Optional[str] = None,
) -> str:
    """
    Reads the value of a given secret from Google Cloud Platform's Secret Manager.

    Args:
        secret_name: Name of the secret to retrieve.
        gcp_credentials: Credentials to use for authentication with GCP.
        timeout: The number of seconds the transport should wait
            for the server response.
        project: Name of the project to use; overrides the
            gcp_credentials project if provided.

    Returns:
        Contents of the specified secret.

    Example:
        ```python
        from prefect import flow
        from prefect_gcp import GcpCredentials
        from prefect_gcp.secret_manager import read_secret

        @flow()
        def example_cloud_storage_read_secret_flow():
            gcp_credentials = GcpCredentials(project="project")
            secret_data = read_secret("secret_name", gcp_credentials, version_id=1)
            return secret_data

        example_cloud_storage_read_secret_flow()
        ```
    """
    logger = get_run_logger()
    logger.info("Reading %s version of %s secret", version_id, secret_name)

    client = gcp_credentials.get_secret_manager_client()
    project = project or gcp_credentials.project

    name = f"projects/{project}/secrets/{secret_name}/versions/{version_id}"
    partial_access = partial(client.access_secret_version, name=name, timeout=timeout)
    response = await to_thread.run_sync(partial_access)
    secret = response.payload.data.decode("UTF-8")
    return secret


@task
async def delete_secret(
    secret_name: str,
    gcp_credentials: "GcpCredentials",
    timeout: float = 60,
    project: Optional[str] = None,
) -> str:
    """
    Deletes the specified secret from Google Cloud Platform's Secret Manager.

    Args:
        secret_name: Name of the secret to delete.
        gcp_credentials: Credentials to use for authentication with GCP.
        timeout: The number of seconds the transport should wait
            for the server response.
        project: Name of the project to use; overrides the
            gcp_credentials project if provided.

    Returns:
        The path of the deleted secret.

    Example:
        ```python
        from prefect import flow
        from prefect_gcp import GcpCredentials
        from prefect_gcp.secret_manager import delete_secret

        @flow()
        def example_cloud_storage_delete_secret_flow():
            gcp_credentials = GcpCredentials(project="project")
            secret_path = delete_secret("secret_name", gcp_credentials)
            return secret_path

        example_cloud_storage_delete_secret_flow()
        ```
    """
    logger = get_run_logger()
    logger.info("Deleting %s secret", secret_name)

    client = gcp_credentials.get_secret_manager_client()
    project = project or gcp_credentials.project

    name = f"projects/{project}/secrets/{secret_name}/"
    partial_delete = partial(client.delete_secret, name=name, timeout=timeout)
    await to_thread.run_sync(partial_delete)
    return name


@task
async def delete_secret_version(
    secret_name: str,
    version_id: int,
    gcp_credentials: "GcpCredentials",
    timeout: float = 60,
    project: Optional[str] = None,
) -> str:
    """
    Deletes a version of a given secret from Google Cloud Platform's Secret Manager.

    Args:
        secret_name: Name of the secret to retrieve.
        version_id: Version number of the secret to use; "latest" can NOT be used.
        gcp_credentials: Credentials to use for authentication with GCP.
        timeout: The number of seconds the transport should wait
            for the server response.
        project: Name of the project to use; overrides the
            gcp_credentials project if provided.

    Returns:
        The path of the deleted secret version.

    Example:
        ```python
        from prefect import flow
        from prefect_gcp import GcpCredentials
        from prefect_gcp.secret_manager import delete_secret_version

        @flow()
        def example_cloud_storage_delete_secret_version_flow():
            gcp_credentials = GcpCredentials(project="project")
            secret_data = delete_secret_version("secret_name", 1, gcp_credentials)
            return secret_data

        example_cloud_storage_delete_secret_version_flow()
        ```
    """
    logger = get_run_logger()
    logger.info("Reading %s version of %s secret", version_id, secret_name)

    client = gcp_credentials.get_secret_manager_client()
    project = project or gcp_credentials.project

    if version_id == "latest":
        raise ValueError("The version_id cannot be 'latest'")

    name = f"projects/{project}/secrets/{secret_name}/versions/{version_id}"
    partial_destroy = partial(client.destroy_secret_version, name=name, timeout=timeout)
    await to_thread.run_sync(partial_destroy)
    return name


class SecretManager(SecretBlock):

    gcp_credentials: GcpCredentials
    secret_name: str = Field(default=..., description="Name of the secret to retrieve.")

    async def read_secret(
        self,
        version_id: Union[str, int] = "latest",
    ) -> Any:
        client = self.gcp_credentials.get_secret_manager_client()
        project = self.gcp_credentials.project
        name = f"projects/{project}/secrets/{self.secret_name}/versions/{version_id}"
        request = AccessSecretVersionRequest(name=name)
        response = await run_sync_in_worker_thread(
            client.access_secret_version, request=request
        )
        secret = response.payload.data.decode("UTF-8")
        return secret

    async def write_secret(self, secret_data: bytes) -> str:
        """
        Writes the secret to the secret storage service.

        Args:
            secret_data: The secret to write.

        Returns:
            The path that the secret was written to.
        """
        client = self.gcp_credentials.get_secret_manager_client()
        project = self.gcp_credentials.project

        parent = f"projects/{project}/"
        secret_id = self.secret_name
        secret = Secret(payload=SecretPayload(data=secret_data))
        request = CreateSecretRequest(parent=parent, secret_id=secret_id, secret=secret)
        response = await run_sync_in_worker_thread(
            client.create_secret, request=request
        )
        return response.name

    async def update_secret(self, secret_data) -> str:
        """
        Updates the secret to the secret storage service.

        Args:
            secret_data: The secret to update.

        Returns:
            The path that the secret was updated to.
        """
        client = self.gcp_credentials.get_secret_manager_client()
        project = self.gcp_credentials.project

        parent = f"projects/{project}/secrets/{self.secret_name}/"
        secret = Secret(payload=SecretPayload(data=secret_data))
        request = AddSecretVersionRequest(parent=parent, secret=secret)
        response = await run_sync_in_worker_thread(
            client.add_secret_version, request=request
        )
        return response.name

    async def delete_secret(self) -> str:
        """
        Deletes the secret from the secret storage service.

        Returns:
            The path that the secret was deleted from.
        """
        client = self.gcp_credentials.get_secret_manager_client()
        project = self.gcp_credentials.project

        name = f"projects/{project}/secrets/{self.secret_name}/"
        request = DeleteSecretRequest(name=name)
        await run_sync_in_worker_thread(client.delete_secret, request=request)
        return name
