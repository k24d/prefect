# Licensed under LICENSE.md; also available at https://www.prefect.io/licenses/alpha-eula

import datetime
import json
import os
from typing import TYPE_CHECKING, Optional, Union

import prefect
from prefect.utilities.graphql import (
    EnumValue,
    parse_graphql,
    with_args,
    GraphQLResult,
    as_nested_dict,
)

if TYPE_CHECKING:
    import requests
    from prefect.core import Flow


BuiltIn = Union[bool, dict, list, str, set, tuple]


class AuthorizationError(Exception):
    pass


class Client:
    """
    Client for communication with Prefect Cloud

    If the arguments aren't specified the client initialization first checks the prefect
    configuration and if the server is not set there it checks the current context. The
    token will only be present in the current context.

    Args:
        - token (str, optional): Authentication token server connection
    """

    def __init__(self, token: str = None) -> None:
        api_server = prefect.config.cloud.get("api", None)

        if not api_server:
            raise ValueError("Could not determine API server.")

        self.api_server = api_server

        graphql_server = prefect.config.cloud.get("graphql", None)

        # Default to the API server
        if not graphql_server:
            graphql_server = api_server

        self.graphql_server = graphql_server

        if token is None:
            token = prefect.config.cloud.get("auth_token", None)

            if token is None:
                token_path = os.path.expanduser("~/.prefect/.credentials/auth_token")
                if os.path.exists(token_path):
                    with open(token_path, "r") as f:
                        token = f.read()

        self.token = token

    # -------------------------------------------------------------------------
    # Utilities

    def post(self, path: str, server: str = None, **params: BuiltIn) -> dict:
        """
        Convenience function for calling the Prefect API with token auth and POST request

        Args:
            - path (str): the path of the API url. For example, to POST
                http://prefect-server/v1/auth/login, path would be 'auth/login'.
            - server (str, optional): the server to send the POST request to;
                defaults to `self.api_server`
            - params (dict): POST parameters

        Returns:
            - dict: Dictionary representation of the request made
        """
        response = self._request(method="POST", path=path, params=params, server=server)
        if response.text:
            return response.json()
        else:
            return {}

    def graphql(self, query: str, **variables: Union[bool, dict, str]) -> dict:
        """
        Convenience function for running queries against the Prefect GraphQL API

        Args:
            - query (str): A string representation of a graphql query to be executed
            - **variables (kwarg): Variables to be filled into a query with the key being
                equivalent to the variables that are accepted by the query

        Returns:
            - dict: Data returned from the GraphQL query

        Raises:
            - ValueError if there are errors raised in the graphql query
        """
        result = self.post(
            path="",
            query=query,
            variables=json.dumps(variables),
            server=self.graphql_server,
        )

        if "errors" in result:
            raise ValueError(result["errors"])
        else:
            return as_nested_dict(result, GraphQLResult).data  # type: ignore

    def _request(
        self, method: str, path: str, params: dict = None, server: str = None
    ) -> "requests.models.Response":
        """
        Runs any specified request (GET, POST, DELETE) against the server

        Args:
            - method (str): The type of request to be made (GET, POST, DELETE)
            - path (str): Path of the API URL
            - params (dict, optional): Parameters used for the request
            - server (str, optional): The server to make requests against, base API
                server is used if not specified

        Returns:
            - requests.models.Response: The response returned from the request

        Raises:
            - ValueError if the client token is not in the context (due to not being logged in)
            - ValueError if a method is specified outside of the accepted GET, POST, DELETE
            - requests.HTTPError if a status code is returned that is not `200` or `401`
        """
        # lazy import for performance
        import requests

        if server is None:
            server = self.api_server
        assert isinstance(server, str)  # mypy assert

        if self.token is None:
            raise ValueError("Call Client.login() to set the client token.")

        url = os.path.join(server, path.lstrip("/")).rstrip("/")

        params = params or {}

        # write this as a function to allow reuse in next try/except block
        def request_fn() -> "requests.models.Response":
            headers = {"Authorization": "Bearer {}".format(self.token)}
            if method == "GET":
                response = requests.get(url, headers=headers, params=params)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=params)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers)
            else:
                raise ValueError("Invalid method: {}".format(method))

            # Check if request returned a successful status
            response.raise_for_status()

            return response

        # If a 401 status code is returned, refresh the login token
        try:
            return request_fn()
        except requests.HTTPError as err:
            if err.response.status_code == 401:
                self.refresh_token()
                return request_fn()
            raise

    # -------------------------------------------------------------------------
    # Auth
    # -------------------------------------------------------------------------

    def login(
        self,
        email: str = None,
        password: str = None,
        account_slug: str = None,
        account_id: str = None,
    ) -> None:
        """
        Login to the server in order to gain access

        Args:
            - email (str): User's email on the platform; if not provided, pulled
                from config
            - password (str): User's password on the platform; if not provided,
                pulled from config
            - account_slug (str, optional): Slug that is unique to the user
            - account_id (str, optional): Specific Account ID for this user to use

        Raises:
            - ValueError if unable to login to the server (request does not return `200`)
        """

        # lazy import for performance
        import requests

        email = email or prefect.config.cloud.email
        password = password or prefect.config.cloud.password

        url = os.path.join(self.api_server, "login")
        response = requests.post(
            url,
            auth=(email, password),
            json=dict(account_id=account_id, account_slug=account_slug),
        )

        # Load the current auth token if able to login
        if not response.ok:
            raise ValueError("Could not log in.")
        self.token = response.json().get("token")
        if self.token:
            creds_path = os.path.expanduser("~/.prefect/.credentials")
            if not os.path.exists(creds_path):
                os.makedirs(creds_path)
            with open(os.path.join(creds_path, "auth_token"), "w+") as f:
                f.write(self.token)

    def logout(self) -> None:
        """
        Logs out by clearing all tokens, including deleting `~/.prefect/credentials/auth_token`
        """
        token_path = os.path.expanduser("~/.prefect/.credentials/auth_token")
        if os.path.exists(token_path):
            os.remove(token_path)
        del self.token

    def refresh_token(self) -> None:
        """
        Refresh the auth token for this user on the server. It is only valid for fifteen minutes.
        """
        # lazy import for performance
        import requests

        url = os.path.join(self.api_server, "refresh_token")
        response = requests.post(
            url, headers={"Authorization": "Bearer {}".format(self.token)}
        )
        self.token = response.json().get("token")

    def get_flow_run_info(self, flow_run_id: str) -> dict:
        query = {
            "query": {
                with_args("flow_run_by_pk", {"id": flow_run_id}): {
                    "version": True,
                    "current_state": {"serialized_state"},
                }
            }
        }
        result = self.graphql(parse_graphql(query)).flow_run_by_pk
        result.state = prefect.serialization.state.StateSchema().load(
            result.current_state.serialized_state
        )
        return result

    def set_flow_run_state(
        self, flow_run_id: str, version: int, state: "prefect.engine.state.State"
    ) -> dict:
        mutation = {
            "mutation($state: String!)": {
                with_args(
                    "setFlowRunState",
                    {
                        "input": {
                            "flowRunId": flow_run_id,
                            "version": version,
                            "state": EnumValue("$state"),
                        }
                    },
                ): {"flow_run": {"version"}}
            }
        }
        return self.graphql(
            parse_graphql(mutation), state=json.dumps(state.serialize())
        ).setFlowRunState.flow_run

    def get_task_run_info(
        self, flow_run_id: str, task_id: str, map_index: Optional[int]
    ) -> dict:
        mutation = {
            "mutation": {
                with_args(
                    "getOrCreateTaskRun",
                    {
                        "input": {
                            "flowRunId": flow_run_id,
                            "taskId": task_id,
                            "mapIndex": map_index,
                        }
                    },
                ): {
                    "task_run": {
                        "id": True,
                        "version": True,
                        "current_state": {"serialized_state"},
                    }
                }
            }
        }
        result = self.graphql(parse_graphql(mutation)).getOrCreateTaskRun.task_run
        result.state = prefect.serialization.state.StateSchema().load(
            result.current_state.serialized_state
        )
        return result

    def set_task_run_state(
        self, task_run_id: str, version: int, state: "prefect.engine.state.State"
    ) -> dict:
        mutation = {
            "mutation($state: String!)": {
                with_args(
                    "setTaskRunState",
                    {
                        "input": {
                            "taskRunId": task_run_id,
                            "version": version,
                            "state": EnumValue("$state"),
                        }
                    },
                ): {"task_run": {"version"}}
            }
        }
        return self.graphql(
            parse_graphql(mutation), state=json.dumps(state.serialize())
        ).setTaskRunState.task_run


class Secret:
    """
    A Secret is a serializable object used to represent a secret key & value.

    Args:
        - name (str): The name of the secret

    The value of the `Secret` is not set upon initialization and instead is set
    either in `prefect.context` or on the server, with behavior dependent on the value
    of the `use_local_secrets` flag in your Prefect configuration file.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def get(self) -> Optional[str]:
        """
        Retrieve the secret value.

        If not found, returns `None`.

        Raises:
            - ValueError: if `use_local_secrets=False` and the Client fails to retrieve your secret
        """
        if prefect.config.cloud.use_local_secrets is True:
            secrets = prefect.context.get("_secrets", {})
            return secrets.get(self.name)
        else:
            client = Client()
            return client.graphql(  # type: ignore
                """
                query($name: String!) {
                    secret(name: $name) {
                        value
                    }
                }""",
                name=self.name,
            ).secret.value
