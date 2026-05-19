from pydantic import BaseModel


class Member(BaseModel):
    id: str
    organization_id: str = "default"
    region: str = "us"
    email: str
    role: str = "user"
    name: str | None = None


class RequesterContext(BaseModel):
    org_id: str = "default"
    member_id: str
    workspace_id: str | None = None
    persona_id: str | None = None
    task_id: str | None = None
    role: str = "user"

    @classmethod
    def from_member(cls, member: Member) -> "RequesterContext":
        return cls(org_id=member.organization_id, member_id=member.id, role=member.role)


class AgentContext(BaseModel):
    id: str
    org_id: str = "default"
    member_id: str
    workspace_id: str | None = None
    persona_id: str | None = None
    task_id: str | None = None

    def as_member(self) -> Member:
        return Member(
            id=self.member_id,
            organization_id=self.org_id,
            email="chronos@local",
            role="agent",
        )


class MemoryEntry(BaseModel):
    id: str
    content: str
    scope: str = "org"
    source: str = "stub"


class ToolResult(BaseModel):
    data: dict
    summary: str
