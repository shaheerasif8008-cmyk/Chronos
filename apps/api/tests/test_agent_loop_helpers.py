"""Unit tests for agent-loop pure helpers touched by CodeRabbit review fixes."""


def test_args_preview_redacts_credential_shaped_keys():
    from runtime.agent_loop import _args_preview

    preview = _args_preview({
        "api_key": "sk-secret",
        "access_token": "tok",
        "client_secret": "cs",
        "Authorization": "Bearer x",
        "password": "p",
        "body": "long body",
        "to": "alex@example.com",
        "vault_ref": "vault://abc",
    })

    # Credential-shaped + bulky keys are omitted...
    for omitted in ("api_key", "access_token", "client_secret", "Authorization", "password", "body"):
        assert preview[omitted] == "[omitted]", omitted
    # ...non-sensitive args pass through...
    assert preview["to"] == "alex@example.com"
    # ...and vault_ref (a reference, not a credential) is preserved for audit.
    assert preview["vault_ref"] == "vault://abc"


def test_to_broker_name_preserves_subagent():
    from runtime.tool_registry import to_broker_name

    assert to_broker_name("spawn__subagent") == "spawn__subagent"
    assert to_broker_name("browser__search") == "browser.search"
    assert to_broker_name("gmail__draft") == "gmail.draft"
    assert to_broker_name("think") == "think"
