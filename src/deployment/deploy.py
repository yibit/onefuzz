#!/usr/bin/env python
#
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta

from azure.common.client_factory import get_client_from_cli_profile
from azure.common.credentials import get_cli_profile
from azure.core.exceptions import ResourceExistsError
from azure.graphrbac import GraphRbacManagementClient
from azure.graphrbac.models import (
    ApplicationCreateParameters,
    AppRole,
    GraphErrorException,
    OptionalClaims,
    RequiredResourceAccess,
    ResourceAccess,
    ServicePrincipalCreateParameters,
)
from azure.mgmt.eventgrid import EventGridManagementClient
from azure.mgmt.eventgrid.models import (
    EventSubscription,
    EventSubscriptionFilter,
    RetryPolicy,
    StorageQueueEventSubscriptionDestination,
)
from azure.mgmt.resource import ResourceManagementClient, SubscriptionClient
from azure.mgmt.resource.resources.models import (
    Deployment,
    DeploymentMode,
    DeploymentProperties,
)
from azure.mgmt.storage import StorageManagementClient
from azure.storage.blob import (
    BlobServiceClient,
    ContainerSasPermissions,
    generate_container_sas,
)
from azure.storage.queue import QueueServiceClient
from msrest.serialization import TZ_UTC
from urllib3.util.retry import Retry

from register_pool_application import (
    add_application_password,
    authorize_application,
    update_registration,
    get_application,
    register_application,
)

USER_IMPERSONATION = "311a71cc-e848-46a1-bdf8-97ff7156d8e6"
ONEFUZZ_CLI_APP = "72f1562a-8c0c-41ea-beb9-fa2b71c80134"
ONEFUZZ_CLI_AUTHORITY = (
    "https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47"
)
TELEMETRY_NOTICE = (
    "Telemetry collection on stats and OneFuzz failures are sent to Microsoft. "
    "To disable, delete the ONEFUZZ_TELEMETRY application setting in the "
    "Azure Functions instance"
)
FUNC_TOOLS_ERROR = (
    "azure-functions-core-tools is not installed, "
    "install v3 using instructions: "
    "https://github.com/Azure/azure-functions-core-tools#installing"
)

logger = logging.getLogger("deploy")


def gen_guid():
    return str(uuid.uuid4())


class Client:
    def __init__(
        self,
        resource_group,
        location,
        application_name,
        owner,
        client_id,
        client_secret,
        app_zip,
        tools,
        instance_specific,
        third_party,
        arm_template,
        workbook_data,
        create_registration,
    ):
        self.resource_group = resource_group
        self.arm_template = arm_template
        self.location = location
        self.application_name = application_name
        self.owner = owner
        self.app_zip = app_zip
        self.tools = tools
        self.instance_specific = instance_specific
        self.third_party = third_party
        self.create_registration = create_registration
        self.results = {
            "client_id": client_id,
            "client_secret": client_secret,
        }
        self.cli_config = {
            "client_id": ONEFUZZ_CLI_APP,
            "authority": ONEFUZZ_CLI_AUTHORITY,
        }

        if os.name == "nt":
            self.azcopy = os.path.join(self.tools, "win64", "azcopy.exe")
        else:
            self.azcopy = os.path.join(self.tools, "linux", "azcopy")
            subprocess.check_output(["chmod", "+x", self.azcopy])

        with open(workbook_data) as f:
            self.workbook_data = json.load(f)

    def get_subscription_id(self):
        profile = get_cli_profile()
        return profile.get_subscription_id()

    def get_location_display_name(self):
        location_client = get_client_from_cli_profile(SubscriptionClient)
        locations = location_client.subscriptions.list_locations(
            self.get_subscription_id()
        )
        for location in locations:
            if location.name == self.location:
                return location.display_name

        raise Exception("unknown location: %s", self.location)

    def check_region(self):
        # At the moment, this only checks are the specified providers available
        # in the selected region

        location = self.get_location_display_name()

        with open(self.arm_template, "r") as handle:
            arm = json.load(handle)

        client = get_client_from_cli_profile(ResourceManagementClient)
        providers = {x.namespace: x for x in client.providers.list()}

        unsupported = []

        for resource in arm["resources"]:
            namespace, name = resource["type"].split("/", 1)

            # resource types are in the form of a/b/c....
            # only the top two are listed as resource types within providers
            name = "/".join(name.split("/")[:2])

            if namespace not in providers:
                unsupported.append("Unsupported provider: %s" % namespace)
                continue

            provider = providers[namespace]
            resource_types = {x.resource_type: x for x in provider.resource_types}
            if name not in resource_types:
                unsupported.append(
                    "Unsupported resource type: %s/%s" % (namespace, name)
                )
                continue

            resource_type = resource_types[name]
            if (
                location not in resource_type.locations
                and len(resource_type.locations) > 0
            ):
                unsupported.append(
                    "%s/%s is unsupported in %s" % (namespace, name, self.location)
                )

        if unsupported:
            print("The following resources required by onefuzz are not supported:")
            print("\n".join(["* " + x for x in unsupported]))
            sys.exit(1)

    def setup_rbac(self):
        """
        Setup the client application for the OneFuzz instance.

        By default, Service Principals do not have access to create
        client applications in AAD.
        """
        if self.results["client_id"] and self.results["client_secret"]:
            logger.info("using existing client application")
            return

        client = get_client_from_cli_profile(GraphRbacManagementClient)
        logger.info("checking if RBAC already exists")

        try:
            existing = list(
                client.applications.list(
                    filter="displayName eq '%s'" % self.application_name
                )
            )
        except GraphErrorException:
            logger.error("unable to query RBAC. Provide client_id and client_secret")
            sys.exit(1)

        if not existing:
            logger.info("creating Application registration")
            url = "https://%s.azurewebsites.net" % self.application_name

            params = ApplicationCreateParameters(
                display_name=self.application_name,
                identifier_uris=[url],
                reply_urls=[url + "/.auth/login/aad/callback"],
                optional_claims=OptionalClaims(id_token=[], access_token=[]),
                required_resource_access=[
                    RequiredResourceAccess(
                        resource_access=[
                            ResourceAccess(id=USER_IMPERSONATION, type="Scope")
                        ],
                        resource_app_id="00000002-0000-0000-c000-000000000000",
                    )
                ],
                app_roles=[
                    AppRole(
                        allowed_member_types=["Application"],
                        display_name="CliClient",
                        id=str(uuid.uuid4()),
                        is_enabled=True,
                        description="Allows access from the CLI.",
                        value="CliClient",
                    ),
                    AppRole(
                        allowed_member_types=["Application"],
                        display_name="LabMachine",
                        id=str(uuid.uuid4()),
                        is_enabled=True,
                        description="Allow access from a lab machine.",
                        value="LabMachine",
                    ),
                ],
            )
            app = client.applications.create(params)

            logger.info("creating service principal")
            service_principal_params = ServicePrincipalCreateParameters(
                account_enabled=True,
                app_role_assignment_required=False,
                service_principal_type="Application",
                app_id=app.app_id,
            )
            client.service_principals.create(service_principal_params)
        else:
            app = existing[0]
            creds = list(client.applications.list_password_credentials(app.object_id))
            client.applications.update_password_credentials(app.object_id, creds)

        (password_id, password) = add_application_password(app.object_id)
        onefuzz_cli_app_uuid = uuid.UUID(ONEFUZZ_CLI_APP)
        cli_app = get_application(onefuzz_cli_app_uuid)

        if cli_app is None:
            logger.info(
                "Could not find the default CLI application under the current subscription, creating a new one"
            )
            app_info = register_application("onefuzz-cli", self.application_name)
            self.cli_config = {
                "client_id": app_info.client_id,
                "authority": app_info.authority,
            }

        else:
            authorize_application(onefuzz_cli_app_uuid, app.app_id)

        self.results["client_id"] = app.app_id
        self.results["client_secret"] = password

        # Log `client_secret` for consumption by CI.
        logger.debug("client_id: %s client_secret: %s", app.app_id, password)

    def deploy_template(self):
        logger.info("deploying arm template: %s", self.arm_template)

        with open(self.arm_template, "r") as template_handle:
            template = json.load(template_handle)

        client = get_client_from_cli_profile(ResourceManagementClient)
        client.resource_groups.create_or_update(
            self.resource_group, {"location": self.location}
        )

        expiry = (datetime.now(TZ_UTC) + timedelta(days=365)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        params = {
            "name": {"value": self.application_name},
            "owner": {"value": self.owner},
            "clientId": {"value": self.results["client_id"]},
            "clientSecret": {"value": self.results["client_secret"]},
            "signedExpiry": {"value": expiry},
            "workbookData": {"value": self.workbook_data},
        }
        deployment = Deployment(
            properties=DeploymentProperties(
                mode=DeploymentMode.incremental, template=template, parameters=params
            )
        )
        result = client.deployments.create_or_update(
            self.resource_group, gen_guid(), deployment
        ).result()
        if result.properties.provisioning_state != "Succeeded":
            logger.error(
                "error deploying: %s",
                json.dumps(result.as_dict(), indent=4, sort_keys=True),
            )
            sys.exit(1)
        self.results["deploy"] = result.properties.outputs

    def create_queues(self):
        logger.info("creating eventgrid destination queue")

        name = self.results["deploy"]["func-name"]["value"]
        key = self.results["deploy"]["func-key"]["value"]
        account_url = "https://%s.queue.core.windows.net" % name
        client = QueueServiceClient(
            account_url=account_url,
            credential={"account_name": name, "account_key": key},
        )
        for queue in ["file-changes", "heartbeat", "proxy", "update-queue"]:
            try:
                client.create_queue(queue)
            except ResourceExistsError:
                pass

    def create_eventgrid(self):
        logger.info("creating eventgrid subscription")
        src_resource_id = self.results["deploy"]["fuzz-storage"]["value"]
        dst_resource_id = self.results["deploy"]["func-storage"]["value"]
        client = get_client_from_cli_profile(StorageManagementClient)
        event_subscription_info = EventSubscription(
            destination=StorageQueueEventSubscriptionDestination(
                resource_id=dst_resource_id, queue_name="file-changes"
            ),
            filter=EventSubscriptionFilter(
                included_event_types=[
                    "Microsoft.Storage.BlobCreated",
                    "Microsoft.Storage.BlobDeleted",
                ]
            ),
            retry_policy=RetryPolicy(
                max_delivery_attempts=30,
                event_time_to_live_in_minutes=1440,
            ),
        )

        client = get_client_from_cli_profile(EventGridManagementClient)
        result = client.event_subscriptions.create_or_update(
            src_resource_id, "onefuzz1", event_subscription_info
        ).result()
        if result.provisioning_state != "Succeeded":
            raise Exception(
                "eventgrid subscription failed: %s"
                % json.dumps(result.as_dict(), indent=4, sort_keys=True),
            )

    def upload_tools(self):
        logger.info("uploading tools from %s", self.tools)
        account_name = self.results["deploy"]["func-name"]["value"]
        key = self.results["deploy"]["func-key"]["value"]
        account_url = "https://%s.blob.core.windows.net" % account_name
        client = BlobServiceClient(account_url, credential=key)
        if "tools" not in [x["name"] for x in client.list_containers()]:
            client.create_container("tools")

        expiry = datetime.utcnow() + timedelta(minutes=30)

        sas = generate_container_sas(
            account_name,
            "tools",
            account_key=key,
            permission=ContainerSasPermissions(
                read=True, write=True, delete=True, list=True
            ),
            expiry=expiry,
        )
        url = "%s/%s?%s" % (account_url, "tools", sas)

        subprocess.check_output(
            [self.azcopy, "sync", self.tools, url, "--delete-destination", "true"]
        )

    def upload_instance_setup(self):
        logger.info("uploading instance-specific-setup from %s", self.instance_specific)
        account_name = self.results["deploy"]["func-name"]["value"]
        key = self.results["deploy"]["func-key"]["value"]
        account_url = "https://%s.blob.core.windows.net" % account_name
        client = BlobServiceClient(account_url, credential=key)
        if "instance-specific-setup" not in [
            x["name"] for x in client.list_containers()
        ]:
            client.create_container("instance-specific-setup")

        expiry = datetime.utcnow() + timedelta(minutes=30)

        sas = generate_container_sas(
            account_name,
            "instance-specific-setup",
            account_key=key,
            permission=ContainerSasPermissions(
                read=True, write=True, delete=True, list=True
            ),
            expiry=expiry,
        )
        url = "%s/%s?%s" % (account_url, "instance-specific-setup", sas)

        subprocess.check_output(
            [
                self.azcopy,
                "sync",
                self.instance_specific,
                url,
                "--delete-destination",
                "true",
            ]
        )

    def upload_third_party(self):
        logger.info("uploading third-party tools from %s", self.third_party)
        account_name = self.results["deploy"]["fuzz-name"]["value"]
        key = self.results["deploy"]["fuzz-key"]["value"]
        account_url = "https://%s.blob.core.windows.net" % account_name

        client = BlobServiceClient(account_url, credential=key)
        containers = [x["name"] for x in client.list_containers()]

        for name in os.listdir(self.third_party):
            path = os.path.join(self.third_party, name)
            if not os.path.isdir(path):
                continue
            if name not in containers:
                client.create_container(name)

            expiry = datetime.utcnow() + timedelta(minutes=30)
            sas = generate_container_sas(
                account_name,
                name,
                account_key=key,
                permission=ContainerSasPermissions(
                    read=True, write=True, delete=True, list=True
                ),
                expiry=expiry,
            )
            url = "%s/%s?%s" % (account_url, name, sas)

            subprocess.check_output(
                [self.azcopy, "sync", path, url, "--delete-destination", "true"]
            )

    def deploy_app(self):
        logger.info("deploying function app %s", self.app_zip)
        current_dir = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdirname:
            with zipfile.ZipFile(self.app_zip, "r") as zip_ref:
                zip_ref.extractall(tmpdirname)
                os.chdir(tmpdirname)
                subprocess.check_output(
                    [
                        shutil.which("func"),
                        "azure",
                        "functionapp",
                        "publish",
                        self.application_name,
                        "--python",
                        "--no-build",
                    ],
                    env=dict(os.environ, CLI_DEBUG="1"),
                )

            os.chdir(current_dir)

    def update_registration(self):
        if not self.create_registration:
            return
        update_registration(self.application_name)

    def done(self):
        logger.info(TELEMETRY_NOTICE)
        client_secret_arg = (
            ("--client_secret %s" % self.cli_config["client_secret"])
            if "client_secret" in self.cli_config
            else ""
        )
        logger.info(
            "Update your CLI config via: onefuzz config --endpoint https://%s.azurewebsites.net --authority %s --client_id %s %s",
            self.application_name,
            self.cli_config["authority"],
            self.cli_config["client_id"],
            client_secret_arg,
        )


def arg_dir(arg):
    if not os.path.isdir(arg):
        raise argparse.ArgumentTypeError("not a directory: %s" % arg)
    return arg


def arg_file(arg):
    if not os.path.isfile(arg):
        raise argparse.ArgumentTypeError("not a file: %s" % arg)
    return arg


def main():
    states = [
        ("check_region", Client.check_region),
        ("rbac", Client.setup_rbac),
        ("arm", Client.deploy_template),
        ("queues", Client.create_queues),
        ("eventgrid", Client.create_eventgrid),
        ("tools", Client.upload_tools),
        ("instance-specific-setup", Client.upload_instance_setup),
        ("third-party", Client.upload_third_party),
        ("api", Client.deploy_app),
        ("update_registration", Client.update_registration),
    ]

    formatter = argparse.ArgumentDefaultsHelpFormatter
    parser = argparse.ArgumentParser(formatter_class=formatter)
    parser.add_argument("location")
    parser.add_argument("resource_group")
    parser.add_argument("application_name")
    parser.add_argument("owner")
    parser.add_argument(
        "--arm-template",
        type=arg_file,
        default="azuredeploy.json",
        help="(default: %(default)s)",
    )
    parser.add_argument(
        "--workbook-data",
        type=arg_file,
        default="workbook-data.json",
        help="(default: %(default)s)",
    )
    parser.add_argument(
        "--app-zip",
        type=arg_file,
        default="api-service.zip",
        help="(default: %(default)s)",
    )
    parser.add_argument(
        "--tools", type=arg_dir, default="tools", help="(default: %(default)s)"
    )
    parser.add_argument(
        "--instance_specific",
        type=arg_dir,
        default="instance-specific-setup",
        help="(default: %(default)s)",
    )
    parser.add_argument(
        "--third-party",
        type=arg_dir,
        default="third-party",
        help="(default: %(default)s)",
    )
    parser.add_argument("--client_id")
    parser.add_argument("--client_secret")
    parser.add_argument(
        "--start_at",
        default=states[0][0],
        choices=[x[0] for x in states],
        help=(
            "Debug deployments by starting at a specific state.  "
            "NOT FOR PRODUCTION USE.  (default: %(default)s)"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--create_pool_registration",
        default=False,
        type=bool,
        help="Create an application registration and/or generate a "
        "password for the pool agent (default: False)",
    )
    args = parser.parse_args()

    if shutil.which("func") is None:
        logger.error(FUNC_TOOLS_ERROR)
        sys.exit(1)

    client = Client(
        args.resource_group,
        args.location,
        args.application_name,
        args.owner,
        args.client_id,
        args.client_secret,
        args.app_zip,
        args.tools,
        args.instance_specific,
        args.third_party,
        args.arm_template,
        args.workbook_data,
        args.create_pool_registration,
    )
    if args.verbose:
        level = logging.DEBUG
    else:
        level = logging.WARN

    logging.basicConfig(level=level)

    logging.getLogger("deploy").setLevel(logging.INFO)

    # TODO: using az_cli resets logging defaults.  For now, force these
    # to be WARN level
    if not args.verbose:
        for entry in [
            "adal-python",
            "msrest.universal_http",
            "urllib3.connectionpool",
            "az_command_data_logger",
            "msrest.service_client",
            "azure.core.pipeline.policies.http_logging_policy",
        ]:
            logging.getLogger(entry).setLevel(logging.WARN)

    if args.start_at != states[0][0]:
        logger.warning(
            "*** Starting at a non-standard deployment state.  "
            "This may result in a broken deployment.  ***"
        )

    started = False
    for state in states:
        if args.start_at == state[0]:
            started = True
        if started:
            state[1](client)

    client.done()


if __name__ == "__main__":
    main()