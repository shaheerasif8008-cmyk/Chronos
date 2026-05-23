import pytest

from core.models import AgentContext


@pytest.mark.asyncio
async def test_internal_connector_registry_install_and_execute_success():
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.runtime import ConnectorExecutionService
    from connectors.framework.seed import seed_builtin_connectors

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)
    connector = await repo.install_connector("internal_echo", tenant_id="default", workspace_id="default")
    action = await repo.get_action(connector["id"], "echo")
    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id=connector["id"],
        action_name="echo",
        allowed_scopes=["internal.echo"],
        approval_required=False,
    )

    result = await ConnectorExecutionService(repo, adapter_registry()).execute(
        connector_id=connector["id"],
        action_name=action["name"],
        arguments={"message": "hello"},
        context=AgentContext(id="employee-1", org_id="default", member_id="member-1", workspace_id="default"),
    )

    assert result.status == "success"
    assert result.output == {"message": "hello"}
    logs = await repo.list_execution_logs(tenant_id="default", connector_id=connector["id"])
    assert logs[0]["result_status"] == "success"
    assert logs[0]["action_name"] == "echo"


@pytest.mark.asyncio
async def test_connector_action_schema_validation_error_is_logged():
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.runtime import ConnectorExecutionService
    from connectors.framework.seed import seed_builtin_connectors

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)
    connector = await repo.install_connector("internal_echo", tenant_id="default", workspace_id="default")
    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id=connector["id"],
        action_name="echo",
        allowed_scopes=["internal.echo"],
        approval_required=False,
    )

    result = await ConnectorExecutionService(repo, adapter_registry()).execute(
        connector_id=connector["id"],
        action_name="echo",
        arguments={},
        context=AgentContext(id="employee-1", org_id="default", member_id="member-1", workspace_id="default"),
    )

    assert result.status == "validation_error"
    assert "message is required" in result.error
    logs = await repo.list_execution_logs(tenant_id="default", connector_id=connector["id"])
    assert logs[0]["result_status"] == "validation_error"


@pytest.mark.asyncio
async def test_permission_denied_and_disabled_connector_cannot_execute():
    from connectors.framework.adapters import adapter_registry
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.runtime import ConnectorExecutionService
    from connectors.framework.seed import seed_builtin_connectors

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)
    connector = await repo.install_connector("internal_time", tenant_id="default", workspace_id="default")
    service = ConnectorExecutionService(repo, adapter_registry())

    denied = await service.execute(
        connector_id=connector["id"],
        action_name="now",
        arguments={},
        context=AgentContext(id="employee-1", org_id="default", member_id="member-1", workspace_id="default"),
    )

    assert denied.status == "permission_denied"

    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id=connector["id"],
        action_name="now",
        allowed_scopes=["internal.time"],
        approval_required=False,
    )
    await repo.disable_connector(connector["id"], tenant_id="default")

    disabled = await service.execute(
        connector_id=connector["id"],
        action_name="now",
        arguments={},
        context=AgentContext(id="employee-1", org_id="default", member_id="member-1", workspace_id="default"),
    )

    assert disabled.status == "failure"
    assert "disabled" in disabled.error


@pytest.mark.asyncio
async def test_approval_required_and_tool_schema_filtering():
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors
    from connectors.framework.tool_calling import get_available_tools_for_employee
    from connectors.framework.runtime import ConnectorExecutionService
    from connectors.framework.adapters import adapter_registry

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)
    echo = await repo.install_connector("internal_echo", tenant_id="default", workspace_id="default")
    time = await repo.install_connector("internal_time", tenant_id="default", workspace_id="default")
    await repo.grant_permission(
        tenant_id="default",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        connector_id=echo["id"],
        action_name="echo",
        allowed_scopes=["internal.echo"],
        approval_required=True,
    )

    tools = await get_available_tools_for_employee(repo, employee_id="employee-1", workspace_id="default", tenant_id="default")

    assert [tool["function"]["name"] for tool in tools] == ["internal_echo__echo"]
    assert tools[0]["function"]["parameters"]["required"] == ["message"]

    result = await ConnectorExecutionService(repo, adapter_registry()).execute(
        connector_id=echo["id"],
        action_name="echo",
        arguments={"message": "requires approval"},
        context=AgentContext(id="employee-1", org_id="default", member_id="member-1", workspace_id="default"),
    )

    assert result.status == "approval_required"
    assert time["id"] != echo["id"]


def test_redaction_never_logs_raw_secrets():
    from connectors.framework.audit import redact_arguments

    redacted = redact_arguments(
        {
            "message": "hello",
            "api_key": "sk-secret",
            "nested": {"password": "pw", "safe": "ok"},
        }
    )

    assert redacted == {
        "message": "hello",
        "api_key": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "safe": "ok"},
    }
