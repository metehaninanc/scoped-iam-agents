"""Sign in as the human admin (device code) and build the admin Graph client.

No standing privileged app exists: the seed scripts run under the delegated sign-in of the admin
account, with the first-party public client "Microsoft Graph Command Line Tools". The admin
consents once to the delegated scopes below. The exact list the admin consented to is recorded
in JOURNAL.md at the first live run, and removing that grant is a step of the end of project
checklist (docs/END_OF_PROJECT.md).

A plan asks only for read scopes. `--apply` asks for the write scopes. Task 0.4 adds
AdministrativeUnit.ReadWrite.All for the administrative units.
"""

from collections.abc import Callable

GRAPH_CLI_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft Graph Command Line Tools
GRAPH_RESOURCE = "https://graph.microsoft.com/"

READ_SCOPES = (
    "User.Read.All",
    "Group.Read.All",
    "GroupMember.Read.All",
    "Directory.Read.All",
    "Organization.Read.All",
    "RoleManagement.Read.Directory",
    "AdministrativeUnit.Read.All",
)
WRITE_SCOPES = (
    "User.ReadWrite.All",  # create and update users, managers, assign licenses, delete users
    "Group.ReadWrite.All",  # create groups, owners, delete groups
    "GroupMember.ReadWrite.All",  # members of the assigned groups
    "RoleManagement.ReadWrite.Directory",  # role assignable groups, their members and role assignments
    "Directory.Read.All",
    "Organization.Read.All",
    "AdministrativeUnit.Read.All",
)


def scopes_for(apply: bool) -> tuple[str, ...]:
    return WRITE_SCOPES if apply else READ_SCOPES


def device_code_token(
    tenant_domain: str, scopes: tuple[str, ...], prompt: Callable[[str], None] = print
) -> Callable[[], str]:
    """A callable that returns a fresh access token. The device code message goes to `prompt`;
    the token itself is never printed."""
    from azure.identity import DeviceCodeCredential

    def show(uri: str, code: str, expires) -> None:
        prompt(f"Sign in as the admin account: open {uri} and enter the code {code}")

    credential = DeviceCodeCredential(
        client_id=GRAPH_CLI_CLIENT_ID, tenant_id=tenant_domain, prompt_callback=show
    )
    urls = [GRAPH_RESOURCE + s for s in scopes]
    return lambda: credential.get_token(*urls).token


def connect(tenant_domain: str, apply: bool, prompt: Callable[[str], None] = print):
    from .rest_admin import RestAdminGraph

    return RestAdminGraph(device_code_token(tenant_domain, scopes_for(apply), prompt))
