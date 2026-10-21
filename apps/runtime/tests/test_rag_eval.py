"""A small, deterministic evaluation of document-grounded answers (tests/data/rag_eval.json).

Retrieval: for each answerable question, is the expected passage among the passages given to
the model (recall@k), how high does it rank (mean reciprocal rank), do near-duplicates take
up places, and does anything come from outside the scope (another organization)? Questions
that share no word with the documents must produce no evidence at all.

Answers (mock provider): every citation refers to a passage that was supplied, a cited source
belongs to an expected document, contradictory documents are both supplied, and text that
tries to instruct the model stays inside its source block.

The thresholds are regression guards for this synthetic set, chosen below the measured values
(printed with `pytest -s`); they say nothing about accuracy on real documents. A weak-evidence
question (sharing one word with the handbook) is measured and reported, not asserted: lexical
retrieval cannot tell that the passages do not answer it, only the model can.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
from django.db import connection
from django.utils import timezone
from fastapi.testclient import TestClient

from nexa_ai.providers.mock import MockProvider
from nexa_ai.types import Feature, Role
from nexa_common.access import Principal
from nexa_core.documents.models import Document
from nexa_core.identity.keys import jwks
from nexa_core.identity.models import User
from nexa_core.identity.sessions import start_session
from nexa_core.organizations.models import Organization
from nexa_core.organizations.services import create_organization
from nexa_core.testing import indexed_document
from nexa_runtime.ai.quota import RedisQuota
from nexa_runtime.auth.jwks import JWKSClient
from nexa_runtime.chat.cancellation import CancelRegistry
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.main import create_app
from nexa_runtime.rag.retrieval import _WORD, Passage, _jaccard
from nexa_runtime.rag.service import Scope

pytestmark = pytest.mark.django_db(transaction=True)


# --- Harness (as in test_rag_chat) ---------------------------------------------------------------


@contextmanager
def make_client(provider: MockProvider, **overrides: Any) -> Iterator[TestClient]:
    d = connection.settings_dict
    settings = RuntimeSettings(
        nexa_env="test",
        runtime_database_url=(
            f"postgresql://{d['USER']}:{d['PASSWORD']}@{d['HOST'] or 'localhost'}:"
            f"{d['PORT'] or 5432}/{d['NAME']}"
        ),
        redis_queue_url="redis://127.0.0.1:1/0",
        ai_registry_ttl_seconds=0.0,
        **overrides,
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=jwks()))
    app = create_app(
        settings,
        jwks_client=JWKSClient(settings.core_jwks_url, transport=transport),
        providers={"mock": provider},
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        fake = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
        app.state.gateway.quota = RedisQuota(fake, settings)
        app.state.chat._cancel = CancelRegistry(fake)
        yield client


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {start_session(user, client_type='web').access_token}"}


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name and data is not None:
            found.append((name, data))
    return found


def new_conversation(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/v1/conversations/", json={}, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


def principal_for(user: User, organization: Organization) -> Principal:
    membership = user.memberships.get(organization=organization)
    return Principal(
        user_id=user.id,
        session_id=None,
        organization_id=organization.id,
        membership_id=membership.id,
        role_key=membership.role.key,
        permissions=frozenset(
            membership.role.role_permissions.values_list("permission__codename", flat=True)
        ),
    )


# --- The evaluation ------------------------------------------------------------------------------

DATA = json.loads((Path(__file__).parent / "data" / "rag_eval.json").read_text(encoding="utf-8"))
TOP_K = 6
MIN_RECALL = 0.9
MIN_MRR = 0.75


def _verified(email: str) -> User:
    user = User.objects.create_user(email, "correct-horse-battery-staple")
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    return user


@pytest.fixture
def corpus() -> dict[str, Any]:
    owner = _verified("owner@example.com")
    organization = create_organization(name="Acme", owner=owner)
    stranger = _verified("stranger@example.com")
    other = create_organization(name="Other", owner=stranger)
    by_name: dict[str, Document] = {}
    for item in DATA["documents"]:
        by_name[item["filename"]] = indexed_document(
            organization,
            owner,
            item["text"],
            filename=item["filename"],
            visibility=item["visibility"],
        )
    foreign = indexed_document(
        other,
        stranger,
        DATA["foreign_document"]["text"],
        filename=DATA["foreign_document"]["filename"],
        visibility="organization",
    )
    return {"owner": owner, "organization": organization, "documents": by_name, "foreign": foreign}


@pytest.fixture
def runtime() -> Iterator[TestClient]:
    with make_client(MockProvider(), rag_top_k=TOP_K) as client:
        yield client


def retrieve(
    client: TestClient, owner: User, organization: Organization, question: str
) -> list[Passage]:
    rag = client.app.state.rag  # type: ignore[attr-defined]
    principal = principal_for(owner, organization)

    async def run() -> list[Passage]:
        grounded = await rag.retrieve(
            principal, Scope("library", max_sources=TOP_K), question=question
        )
        return list(grounded.evidence.passages)

    return client.portal.call(run)  # type: ignore[union-attr]


def first_hit(passages: list[Passage], expected: list[list[str]], names: dict[Any, str]) -> int:
    """1-based rank of the first passage holding any expected (document, text); 0 if none."""
    for rank, passage in enumerate(passages, start=1):
        for filename, text in expected:
            if names[passage.document_id] == filename and text in passage.content:
                return rank
    return 0


def test_retrieval_quality(runtime: TestClient, corpus: dict[str, Any]) -> None:
    owner, organization = corpus["owner"], corpus["organization"]
    names = {d.id: name for name, d in corpus["documents"].items()}
    names[corpus["foreign"].id] = "FOREIGN"
    hits, reciprocal, duplicate_pairs, outside = 0, 0.0, 0, 0
    report: list[str] = []
    for case in DATA["questions"]:
        passages = retrieve(runtime, owner, organization, case["question"])
        rank = first_hit(passages, case["expected"], names)
        hits += rank > 0
        reciprocal += 1 / rank if rank else 0.0
        outside += sum(1 for p in passages if names[p.document_id] == "FOREIGN")
        words = [frozenset(_WORD.findall(p.content.lower())) for p in passages]
        duplicate_pairs += sum(
            1
            for i in range(len(words))
            for j in range(i + 1, len(words))
            if _jaccard(words[i], words[j]) >= 0.8
        )
        if case.get("contradiction"):
            found = {names[p.document_id] for p in passages}
            assert {"office-2025.md", "office-2026.md"} <= found, case["id"]
        report.append(f"  {case['id']:<16} rank={rank or '-':<3} passages={len(passages)}")
    total = len(DATA["questions"])
    recall, mrr = hits / total, reciprocal / total

    unanswerable = [
        len(retrieve(runtime, owner, organization, case["question"]))
        for case in DATA["unanswerable"]
    ]
    weak = [
        len(retrieve(runtime, owner, organization, case["question"]))
        for case in DATA["weak_evidence"]
    ]
    print(
        "\nRAG retrieval evaluation (synthetic set, lexical baseline)\n"
        + "\n".join(report)
        + f"\n  recall@{TOP_K}={recall:.2f}  MRR={mrr:.2f}  questions={total}"
        f"\n  unanswerable with no evidence: {unanswerable.count(0)}/{len(unanswerable)}"
        f"\n  weak-evidence passages supplied (model must abstain): {weak}"
        f"\n  near-duplicate pairs in results: {duplicate_pairs}"
        f"\n  passages from another organization: {outside}"
    )
    assert recall >= MIN_RECALL
    assert mrr >= MIN_MRR
    assert unanswerable == [0] * len(unanswerable)
    assert duplicate_pairs == 0
    assert outside == 0


def test_answers_cite_only_supplied_and_expected_sources(
    runtime: TestClient, corpus: dict[str, Any]
) -> None:
    owner = corpus["owner"]
    names = {str(d.id): name for name, d in corpus["documents"].items()}
    headers = auth(owner)
    expected_cited, invalid, answered = 0, 0, 0
    for case in DATA["questions"]:
        conversation = new_conversation(runtime, headers)
        response = runtime.post(
            f"/api/v1/conversations/{conversation}/messages/",
            json={"content": case["question"], "grounding": {"all_documents": True}},
            headers={**headers, "Accept": "text/event-stream"},
        )
        message = events(response.text)[-1][1]["message"]
        grounding = message["grounding"]
        supplied = {s["label"] for s in message["sources"]}
        cited = [s for s in message["sources"] if s["cited"]]
        invalid += len(grounding["invalid_citations"])
        answered += grounding["outcome"] == "answered"
        assert {s["label"] for s in cited} <= supplied
        expected_files = {filename for filename, _ in case["expected"]}
        if any(names.get(s["document_id"]) in expected_files for s in cited):
            expected_cited += 1
    total = len(DATA["questions"])
    print(
        f"\nRAG answer evaluation (mock provider): answered={answered}/{total} "
        f"cited an expected document={expected_cited}/{total} invalid citations={invalid}"
    )
    assert invalid == 0
    assert expected_cited / total >= MIN_RECALL


def test_injected_instructions_stay_inside_the_sources(
    corpus: dict[str, Any],
) -> None:
    provider = MockProvider()
    with make_client(provider, rag_top_k=TOP_K) as client:
        headers = auth(corpus["owner"])
        conversation = new_conversation(client, headers)
        security = corpus["documents"]["security.txt"]
        response = client.post(
            f"/api/v1/conversations/{conversation}/messages/",
            json={
                "content": "Does the security policy allow sharing passwords?",
                "grounding": {"document_ids": [str(security.id)]},
            },
            headers={**headers, "Accept": "text/event-stream"},
        )
        assert events(response.text)[-1][0] == "message_complete"
    [call] = [c for c in provider.calls if c.request.feature is Feature.RAG]
    systems = [m.text for m in call.request.messages if m.role is Role.SYSTEM]
    injected = "disregard all prior rules"
    assert injected not in systems[0]
    rules, sources = systems[1].split("<sources>", 1)
    assert injected not in rules
    assert injected in sources
    assert all(injected not in m.text for m in call.request.messages if m.role is Role.USER)
