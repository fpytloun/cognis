"""Controller-only usage tool, bound to immutable trusted runtime context."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from cognis.core.tool_arguments import validate_tool_arguments
from cognis.core.usage.access import UsageAccessError, UsageInvocation
from cognis.core.usage.availability import UsageResolutionError
from cognis.core.usage.query_models import UsageScope, UsageToolArguments
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.models import Conversation, Task
from cognis.tools.builtin.workflow import QUERY_USAGE_TOOL


def build_usage_tool_handlers(service: Any, runtime_access: Any) -> dict[str, Any]:
    """Bind server-selected authority, never metadata supplied in tool arguments."""

    async def query_usage(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        validation = validate_tool_arguments(
            "query_usage",
            arguments,
            schema=QUERY_USAGE_TOOL.parameters,
        )
        if validation:
            return validation.as_tool_result()
        if service is None:
            return {
                "error": "usage_service_unavailable",
                "message": "Usage service is not composed",
            }
        access = runtime_access
        if (
            access is None
            or not access.user_email
            or not access.agent_id
            or not access.agent_owner_email
        ):
            return {
                "error": "usage_access_denied",
                "message": "Trusted usage invocation is unavailable",
            }
        try:
            request = UsageToolArguments.model_validate(arguments).request
            requested = request.query.scope
            if requested.mode == "admin":
                raise UsageAccessError("Global usage is not delegated to this agent invocation")
            project_id = None
            async with service.session_factory() as session:
                if access.conversation_id:
                    row = (
                        await session.execute(
                            select(
                                Conversation.project_id,
                                Conversation.status,
                                Conversation.agent_id,
                            ).where(Conversation.conversation_id == access.conversation_id)
                        )
                    ).first()
                    if row is None or row.status == "deleted" or row.agent_id != access.agent_id:
                        raise UsageAccessError("Usage conversation binding is unavailable")
                    project_id = row.project_id
                if access.task_id:
                    task = (
                        await session.execute(
                            select(Task.project_id).where(
                                Task.task_id == access.task_id,
                                Task.status != "deleted",
                            )
                        )
                    ).first()
                    if task is None or (project_id is not None and project_id != task.project_id):
                        raise UsageAccessError("Usage task/project binding is inconsistent")
                    project_id = task.project_id
            if requested.project_id not in (None, project_id):
                raise UsageAccessError("Requested project is outside the trusted invocation")
            if requested.mode == "project" and project_id is None:
                raise UsageAccessError("Project usage requires a current bound project")
            invocation = UsageInvocation(
                agent_id=access.agent_id,
                agent_owner_email=access.agent_owner_email,
                project_id=project_id,
                conversation_id=access.conversation_id
                if not access.is_interactive_root_chat
                else None,
                task_id=access.task_id,
                allowed_modes=frozenset({"personal", "project"})
                if project_id
                else frozenset({"personal"}),
            )
            query = request.query.model_copy(
                update={"scope": UsageScope(mode=requested.mode, project_id=project_id)}
            )
            method = getattr(service, request.operation)
            result = (
                await method(access.user_email, request.provider_id, query, invocation=invocation)
                if request.operation == "provider_history"
                else await method(access.user_email, query, invocation=invocation)
            )
            return dict(result.model_dump(mode="json"))
        except UsageAccessError:
            return {
                "error": "usage_access_denied",
                "message": "Usage scope is not currently authorized",
            }
        except UsageQueryError as exc:
            return {"error": exc.code, "message": str(exc)}
        except UsageResolutionError as exc:
            return {"error": exc.code, "message": str(exc)}
        except ValidationError:
            return {"error": "invalid_usage_arguments", "message": "Usage arguments are invalid"}

    return {"query_usage": query_usage}
