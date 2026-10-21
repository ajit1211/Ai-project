"""Lexical, semantic and hybrid retrieval compared on one synthetic set
(tests/data/rag_eval_v2.json), with the mock embedding provider.

For every mode: recall@k (share of the expected passages found), precision@k (share of the
passages supplied that are relevant), MRR, nDCG@k, latency, the question embeddings' tokens,
passages from outside the member's scope (another organization, a colleague's private
document) and whether unanswerable questions get no evidence. Then: hybrid answers cite only
supplied sources; a changed document is found by its new text only, in every mode; retrieval
stays fast on a larger generated library.

The mock's vectors hash words and character trigrams, so this measures the pipeline and the
fusion, not semantic quality: the mock helps with typos and shared word pieces, not with
synonyms. The thresholds are regression guards chosen below the values measured here (printed
with `pytest -s`). A live run with a real provider is opt-in (`test_live_openai`, see
docs/architecture/vector-search.md#evaluation) and never runs in CI.

Needs PostgreSQL with pgvector; skipped otherwise.
"""

import json
import math
import os
import random
import statistics
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
from django.db import connection, transaction
from django.utils import timezone
from fastapi.testclient import TestClient

from nexa_ai.embeddings import MockEmbeddingProvider
from nexa_ai.providers.mock import MockProvider
from nexa_common.access import Principal
from nexa_core.ai.models import AIUsageEvent
from nexa_core.celery import app as celery_app
from nexa_core.documents import embedding, vectors
from nexa_core.documents.indexing import request_indexing
from nexa_core.documents.models import Document, DocumentEmbedding, DocumentText
from nexa_core.identity.keys import jwks
from nexa_core.identity.models import User
from nexa_core.identity.sessions import start_session
from nexa_core.organizations.models import Organization, TeamMembership
from nexa_core.organizations.services import create_organization
from nexa_core.testing import add_member, indexed_document
from nexa_runtime.ai.quota import RedisQuota
from nexa_runtime.auth.jwks import JWKSClient
from nexa_runtime.chat.cancellation import CancelRegistry
from nexa_runtime.config import RuntimeSettings
from nexa_runtime.main import create_app
from nexa_runtime.rag.retrieval import Passage
from nexa_runtime.rag.service import Grounded, Scope

pytestmark = pytest.mark.django_db(transaction=True)

DATA = json.loads((Path(__file__).parent / "data" / "rag_eval_v2.json").read_text(encoding="utf-8"))
TOP_K = 6
MODES = ("lexical", "semantic", "hybrid")
# Regression guards, below the values measured on this set with the mock (see the module doc).
# Measured: recall lexical 0.87, semantic 0.67, hybrid 0.87; MRR 0.87, 0.67, 0.83.
MIN_RECALL = {"lexical": 0.8, "semantic": 0.6, "hybrid": 0.8}
MIN_MRR = {"lexical": 0.8, "semantic": 0.6, "hybrid": 0.75}


@pytest.fixture(autouse=True)
def pgvector(monkeypatch: pytest.MonkeyPatch) -> None:
    if not vectors.status(connection).ready:
        pytest.skip("needs PostgreSQL with the pgvector extension")
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)


@contextmanager
def make_client(*, live: bool = False, **overrides: Any) -> Iterator[TestClient]:
    """`live`: embedding providers built from the settings (a real API key), not the mock."""
    d = connection.settings_dict
    values: dict[str, Any] = {
        "nexa_env": "test",
        "runtime_database_url": (
            f"postgresql://{d['USER']}:{d['PASSWORD']}@{d['HOST'] or 'localhost'}:"
            f"{d['PORT'] or 5432}/{d['NAME']}"
        ),
        "redis_queue_url": "redis://127.0.0.1:1/0",
        "ai_registry_ttl_seconds": 0.0,
        "memory_enabled": False,
        "rag_semantic_status_ttl_seconds": 0.0,
        "rag_top_k": TOP_K,
    }
    values.update(overrides)
    settings = RuntimeSettings(**values)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=jwks()))
    app = create_app(
        settings,
        jwks_client=JWKSClient(settings.core_jwks_url, transport=transport),
        providers={"mock": MockProvider()},
        embedding_providers=None if live else {"mock": MockEmbeddingProvider()},
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        fake = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
        app.state.gateway.quota = RedisQuota(fake, settings)
        app.state.chat._cancel = CancelRegistry(fake)
        yield client


def _verified(email: str) -> User:
    user = User.objects.create_user(email, "correct-horse-battery-staple")
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    return user


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
        team_ids=frozenset(
            TeamMembership.objects.filter(membership=membership).values_list("team_id", flat=True)
        ),
    )


def embedded(org: Organization, user: User, text: str, **extra: Any) -> Document:
    document: Document = indexed_document(org, user, text, **extra)
    state = DocumentEmbedding.objects.get(document=document)
    assert state.status == "ready", (state.status, state.error_code)
    return document


@pytest.fixture
def corpus() -> dict[str, Any]:
    return build_corpus()


def build_corpus() -> dict[str, Any]:
    owner = _verified("owner@example.com")
    organization = create_organization(name="Acme", owner=owner)
    member = _verified("member@example.com")
    add_member(organization, member, "member")
    stranger = _verified("stranger@example.com")
    other = create_organization(name="Other", owner=stranger)
    names: dict[Any, str] = {}
    for item in DATA["documents"]:
        document = embedded(
            organization,
            owner,
            item["text"],
            filename=item["filename"],
            visibility=item["visibility"],
        )
        names[document.id] = item["filename"]
    foreign = embedded(
        other,
        stranger,
        DATA["foreign_document"]["text"],
        filename=DATA["foreign_document"]["filename"],
        visibility="organization",
    )
    names[foreign.id] = "FOREIGN"
    out_of_scope = {foreign.id} | {
        d.id for d in Document.objects.filter(organization=organization, visibility="private")
    }
    return {
        "member": member,
        "organization": organization,
        "names": names,
        "out_of_scope": out_of_scope,
    }


def retrieve(
    client: TestClient, user: User, organization: Organization, question: str, mode: str
) -> Grounded:
    rag = client.app.state.rag  # type: ignore[attr-defined]
    principal = principal_for(user, organization)

    async def run() -> Grounded:
        grounded: Grounded = await rag.retrieve(
            principal, Scope("library", retrieval_mode=mode, max_sources=TOP_K), question=question
        )
        return grounded

    return client.portal.call(run)  # type: ignore[union-attr]


# --- Metrics -------------------------------------------------------------------------------------


def judge(
    passages: list[Passage], expected: list[list[str]], names: dict[Any, str]
) -> list[int | None]:
    """For each passage, the index of the expected item it is the first to satisfy (None when
    it satisfies none, or only items already found higher up)."""
    found: set[int] = set()
    marks: list[int | None] = []
    for passage in passages[:TOP_K]:
        mark = None
        for i, (filename, text) in enumerate(expected):
            if (
                i not in found
                and names.get(passage.document_id) == filename
                and (text in passage.content)
            ):
                mark = i
                found.add(i)
                break
        marks.append(mark)
    return marks


def relevant(passage: Passage, expected: list[list[str]], names: dict[Any, str]) -> bool:
    return any(
        names.get(passage.document_id) == filename and text in passage.content
        for filename, text in expected
    )


def scores(
    passages: list[Passage], expected: list[list[str]], names: dict[Any, str]
) -> dict[str, float]:
    marks = judge(passages, expected, names)
    hits = [m is not None for m in marks]
    supplied = passages[:TOP_K]
    first = next((rank for rank, hit in enumerate(hits, start=1) if hit), 0)
    dcg = sum(1 / math.log2(rank + 1) for rank, hit in enumerate(hits, start=1) if hit)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(expected), TOP_K) + 1))
    return {
        "recall": sum(hits) / len(expected),
        "precision": (
            sum(relevant(p, expected, names) for p in supplied) / len(supplied) if supplied else 0.0
        ),
        "mrr": 1 / first if first else 0.0,
        "ndcg": dcg / ideal,
    }


def _rag_query_usage() -> tuple[int, int]:
    rows = AIUsageEvent.objects.filter(feature="embedding", source_type="rag_query")
    return rows.count(), sum(r.input_tokens or 0 for r in rows)


def evaluate(client: TestClient, corpus: dict[str, Any], mode: str) -> dict[str, Any]:
    member, organization, names = corpus["member"], corpus["organization"], corpus["names"]
    totals = {"recall": 0.0, "precision": 0.0, "mrr": 0.0, "ndcg": 0.0}
    latencies: list[float] = []
    per_question: dict[str, str] = {}
    outside, fallbacks = 0, 0
    calls_before, tokens_before = _rag_query_usage()
    for case in DATA["questions"]:
        started = time.perf_counter()
        grounded = retrieve(client, member, organization, case["question"], mode)
        latencies.append((time.perf_counter() - started) * 1000)
        passages = list(grounded.evidence.passages)
        fallbacks += grounded.fallback is not None
        outside += sum(1 for p in passages if p.document_id in corpus["out_of_scope"])
        result = scores(passages, case["expected"], names)
        for key in totals:
            totals[key] += result[key]
        per_question[case["id"]] = f"{result['recall']:.1f}/{result['mrr']:.2f}"
    unanswered = 0
    for case in DATA["unanswerable"]:
        grounded = retrieve(client, member, organization, case["question"], mode)
        unanswered += not grounded.evidence.passages
    weak = [
        len(retrieve(client, member, organization, case["question"], mode).evidence.passages)
        for case in DATA["weak_evidence"]
    ]
    calls_after, tokens_after = _rag_query_usage()
    total = len(DATA["questions"])
    latencies.sort()
    return {
        **{key: value / total for key, value in totals.items()},
        "latency_p50": statistics.median(latencies),
        "latency_p95": latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)],
        "embedding_calls": calls_after - calls_before,
        "embedding_tokens": tokens_after - tokens_before,
        "outside": outside,
        "fallbacks": fallbacks,
        "no_evidence": unanswered,
        "weak": weak,
        "per_question": per_question,
    }


def report(title: str, results: dict[str, dict[str, Any]]) -> str:
    lines = [f"\n{title} (k={TOP_K}, questions={len(DATA['questions'])})"]
    lines.append(
        f"  {'mode':<9}{'recall':>8}{'prec':>7}{'MRR':>7}{'nDCG':>7}{'p50 ms':>9}{'p95 ms':>9}"
        f"{'emb calls':>11}{'emb tok':>9}{'leaks':>7}{'no-evid':>9}"
    )
    for mode, r in results.items():
        lines.append(
            f"  {mode:<9}{r['recall']:>8.2f}{r['precision']:>7.2f}{r['mrr']:>7.2f}"
            f"{r['ndcg']:>7.2f}{r['latency_p50']:>9.1f}{r['latency_p95']:>9.1f}"
            f"{r['embedding_calls']:>11}{r['embedding_tokens']:>9}{r['outside']:>7}"
            f"{r['no_evidence']:>5}/{len(DATA['unanswerable'])}"
        )
    lines.append(
        "  weak-evidence passages supplied (the model must abstain): "
        + ", ".join(f"{mode}={r['weak']}" for mode, r in results.items())
    )
    lines.append("  per question (recall/reciprocal rank):")
    for case in DATA["questions"]:
        cells = "  ".join(f"{mode}={results[mode]['per_question'][case['id']]}" for mode in results)
        lines.append(f"    {case['id']:<20} {case['kind']:<13} {cells}")
    return "\n".join(lines)


# --- The evaluation ------------------------------------------------------------------------------


def test_modes_compared(corpus: dict[str, Any]) -> None:
    with make_client() as client:
        results = {mode: evaluate(client, corpus, mode) for mode in MODES}
    print(report("Retrieval modes compared (synthetic set, mock embeddings)", results))
    for mode, r in results.items():
        assert r["outside"] == 0, mode
        assert r["fallbacks"] == 0, mode
        assert r["no_evidence"] == len(DATA["unanswerable"]), mode
        assert r["recall"] >= MIN_RECALL[mode], mode
        assert r["mrr"] >= MIN_MRR[mode], mode
    # Only modes that search vectors embed the question, once per question.
    asked = len(DATA["questions"]) + len(DATA["unanswerable"]) + len(DATA["weak_evidence"])
    assert results["lexical"]["embedding_calls"] == 0
    assert results["semantic"]["embedding_calls"] == asked
    assert results["hybrid"]["embedding_calls"] == asked
    # Fusion never loses what both lists agree on: hybrid recall is at least lexical recall.
    assert results["hybrid"]["recall"] >= results["lexical"]["recall"]


def test_hybrid_answers_cite_only_supplied_sources(corpus: dict[str, Any]) -> None:
    member = corpus["member"]
    headers = {
        "Authorization": f"Bearer {start_session(member, client_type='web').access_token}",
    }
    invalid, outcomes = 0, Counter[str]()
    with make_client() as client:
        cases = [(c["question"], True) for c in DATA["questions"]] + [
            (c["question"], False) for c in DATA["unanswerable"]
        ]
        for question, answerable in cases:
            conversation = client.post("/api/v1/conversations/", json={}, headers=headers).json()
            response = client.post(
                f"/api/v1/conversations/{conversation['data']['id']}/messages/",
                json={
                    "content": question,
                    "grounding": {"all_documents": True, "retrieval_mode": "hybrid"},
                },
                headers={**headers, "Accept": "text/event-stream"},
            )
            message = _last_message(response.text)
            grounding = message["grounding"]
            assert grounding["retrieval_mode"] == "hybrid"
            invalid += len(grounding["invalid_citations"])
            supplied = {s["label"] for s in message["sources"]}
            assert {s["label"] for s in message["sources"] if s["cited"]} <= supplied
            assert all(
                s["document_id"] not in map(str, corpus["out_of_scope"]) for s in message["sources"]
            )
            outcomes[grounding["outcome"]] += 1
            if not answerable:
                assert grounding["outcome"] == "no_evidence", question
    print(f"\nHybrid grounded answers (mock model): {dict(outcomes)}, invalid citations={invalid}")
    assert invalid == 0


def _last_message(body: str) -> dict[str, Any]:
    for block in reversed(body.split("\n\n")):
        if "event: message_complete" in block:
            data = next(line[6:] for line in block.splitlines() if line.startswith("data: "))
            return json.loads(data)["message"]  # type: ignore[no-any-return]
    raise AssertionError(body[-500:])


def test_a_changed_document_is_found_by_its_new_text_only(corpus: dict[str, Any]) -> None:
    member, organization = corpus["member"], corpus["organization"]
    changed = DATA["changed_document"]
    document = embedded(
        organization,
        member,
        changed["before"],
        filename=changed["filename"],
        visibility="organization",
    )
    first = DocumentEmbedding.objects.get(document=document)
    with transaction.atomic():
        DocumentText.objects.filter(document=document).update(content=changed["after"])
        request_indexing(Document.objects.get(id=document.id))
    # Committing ran the index task, which requested a new embedding generation, which ran.
    state = DocumentEmbedding.objects.get(document=document)
    assert state.status == "ready"
    assert state.generation > first.generation
    assert (state.source_generation or 0) > (first.source_generation or 0)
    with make_client() as client:
        for mode in MODES:
            grounded = retrieve(client, member, organization, changed["question"], mode)
            mine = [p.content for p in grounded.evidence.passages if p.document_id == document.id]
            assert mine, mode
            assert all("green badge" in text and "red badge" not in text for text in mine), mode


def test_retrieval_stays_fast_on_a_larger_library(corpus: dict[str, Any]) -> None:
    """A reproducible library of 150 generated documents (seeded), searched exactly (no ANN
    index). The bound is generous: it catches a lost index or a query plan gone wrong, not small
    regressions. Measured values are printed."""
    member, organization = corpus["member"], corpus["organization"]
    rng = random.Random(9)  # noqa: S311 - reproducible test data, not security
    words = [
        "policy",
        "budget",
        "invoice",
        "contract",
        "supplier",
        "warehouse",
        "delivery",
        "shipment",
        "customer",
        "account",
        "training",
        "safety",
        "audit",
        "report",
        "quarter",
        "forecast",
        "project",
        "milestone",
        "review",
        "approval",
        "request",
        "schedule",
        "meeting",
        "holiday",
        "travel",
        "laptop",
        "network",
        "backup",
        "server",
    ]
    for n in range(150):
        paragraphs = [
            " ".join(rng.choice(words) for _ in range(rng.randint(25, 60))) + "."
            for _ in range(rng.randint(2, 5))
        ]
        embedded(
            organization,
            member,
            f"# Generated {n}\n\n" + "\n\n".join(paragraphs),
            filename=f"generated-{n}.md",
            visibility="organization",
        )
    questions = [" ".join(rng.choice(words) for _ in range(4)) for _ in range(15)]
    timings: dict[str, list[float]] = {}
    with make_client() as client:
        for mode in MODES:
            retrieve(client, member, organization, questions[0], mode)  # warm up
            timings[mode] = []
            for question in questions:
                started = time.perf_counter()
                retrieve(client, member, organization, question, mode)
                timings[mode].append((time.perf_counter() - started) * 1000)
    chunks = vectors_count()
    print(f"\nRetrieval latency, {chunks} chunk vectors, exact search (ms):")
    for mode, values in timings.items():
        values.sort()
        p95 = values[math.ceil(0.95 * len(values)) - 1]
        print(f"  {mode:<9} p50={statistics.median(values):7.1f}  p95={p95:7.1f}")
        assert p95 < 2_000, mode


def vectors_count() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM chunk_embeddings")
        return int(cursor.fetchone()[0])


@pytest.mark.skipif(
    not (os.environ.get("NEXA_LIVE_EMBEDDING_EVAL") and os.environ.get("NEXA_AI_OPENAI_API_KEY")),
    reason="live provider evaluation: set NEXA_LIVE_EMBEDDING_EVAL=1 and NEXA_AI_OPENAI_API_KEY",
)
def test_live_openai(settings: Any) -> None:
    """Opt-in: the same comparison with `nexa-embed-small` (OpenAI text-embedding-3-small).
    Sends the synthetic documents and questions to OpenAI and is billed; reports, asserts only
    that nothing leaks and nothing falls back."""
    key = os.environ["NEXA_AI_OPENAI_API_KEY"]
    settings.NEXA_EMBEDDING_MODEL = "nexa-embed-small"
    settings.NEXA_AI_OPENAI_API_KEY = key
    embedding.availability(fresh=True)
    corpus = build_corpus()
    with make_client(
        live=True, embedding_model="nexa-embed-small", ai_openai_api_key=key
    ) as client:
        results = {mode: evaluate(client, corpus, mode) for mode in MODES}
    print(
        report("Retrieval modes compared (synthetic set, OpenAI text-embedding-3-small)", results)
    )
    for mode, r in results.items():
        assert r["outside"] == 0, mode
        assert r["fallbacks"] == 0, mode
