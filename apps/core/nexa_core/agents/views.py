"""`/api/v1/agents/*` (docs/api/agents.md#agents): agent definitions, served by Core. Tasks
(runs) are Runtime's `/api/v1/runs/`.

The organization comes from the access token, as for documents; the service applies the
`agents.*` permissions and the creator rule.
"""

from typing import Any
from uuid import UUID

from rest_framework.request import Request
from rest_framework.response import Response

from nexa_common.errors import PermissionDenied
from nexa_core.access.drf import AuthenticatedView, current_auth, current_user
from nexa_core.access.principal import OrganizationContext, load_organization_context
from nexa_core.agents import services
from nexa_core.agents.serializers import AgentInput, agent_payload
from nexa_core.api import ok


def _validated(data: Any, *, partial: bool) -> dict[str, Any]:
    serializer = AgentInput(data=data, partial=partial)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


class AgentsView(AuthenticatedView):
    context: OrganizationContext

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        auth = current_auth(request)
        if auth.claims.organization_id is None:
            raise PermissionDenied("Select an organization first.")
        self.context = load_organization_context(
            current_user(request), auth.claims.organization_id, auth.session.id
        )


class AgentCollectionView(AgentsView):
    def get(self, request: Request) -> Response:
        agents = services.visible_agents(self.context)
        if request.query_params.get("enabled") == "true":
            agents = agents.filter(enabled=True)
        return ok([agent_payload(agent, self.context) for agent in agents[:200]])

    def post(self, request: Request) -> Response:
        agent = services.create_agent(self.context, _validated(request.data, partial=False))
        return ok(
            agent_payload(agent, self.context, detail=True), message="Agent created.", status=201
        )


class AgentDetailView(AgentsView):
    def get(self, request: Request, agent_id: UUID) -> Response:
        agent = services.get_agent(self.context, agent_id)
        return ok(agent_payload(agent, self.context, detail=True))

    def patch(self, request: Request, agent_id: UUID) -> Response:
        agent = services.update_agent(
            self.context, agent_id, _validated(request.data, partial=True)
        )
        return ok(agent_payload(agent, self.context, detail=True), message="Agent updated.")

    def delete(self, request: Request, agent_id: UUID) -> Response:
        services.delete_agent(self.context, agent_id)
        return ok(None, message="Agent deleted.")
