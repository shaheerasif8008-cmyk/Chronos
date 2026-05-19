from core import audit
from core.models import Member


async def check(actor: Member, action: str, resource: str) -> bool:
    await audit.log(
        "permission_check",
        actor.id,
        action,
        resource_type="generic",
        resource_id=resource,
        decision="granted_stub",
    )
    return True
