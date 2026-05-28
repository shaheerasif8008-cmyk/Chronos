from pydantic import BaseModel, Field
from datetime import datetime


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
    project_id: str | None = None
    role: str = "user"
    memory_context: str = "chat"
    # Side-channel: surfaced source citations, set by assemble_context so the
    # caller can persist them without changing assemble_context's return type.
    surfaced_citations: list = Field(default_factory=list)

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

    @classmethod
    def from_task(cls, task_dict: dict) -> "AgentContext":
        return cls(
            id=f"task:{task_dict['id']}",
            org_id=task_dict.get("organization_id", "default"),
            member_id=task_dict.get("triggered_by_member_id") or "chronos",
            workspace_id=task_dict.get("workspace_id"),
            persona_id=task_dict.get("persona_id"),
            task_id=task_dict["id"],
        )

    def as_member(self) -> Member:
        return Member(
            id=self.member_id,
            organization_id=self.org_id,
            email="chronos@local",
            role="agent",
        )


class MemoryEntry(BaseModel):
    id: str
    organization_id: str = "default"
    region: str = "us"
    content: str
    scope: str = "org"
    source: str = "stub"
    scope_id: str = "default"
    importance_score: float = 0.5
    is_deleted: bool = False
    created_by: str | None = None
    created_at: datetime | None = None


class ToolResult(BaseModel):
    data: dict
    summary: str
