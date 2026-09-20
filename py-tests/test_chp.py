"""CHP-hardened mint gate: R0 refusal, deterministic foundation scoring, human lock,
and the decision ledger.

Covers the consensus-hardening-protocol integration (``greenverify/chp.py``):

- the verification-shaped R0 gate refuses ill-posed requests before the LLM engine;
- the deterministic adversary scores input guardrails + bounded result +
  measurement parity (blockchain-domain mints gate at CHP's blockchain floor
  of 85 through parity evidence, and a parity mismatch is fatal);
- every hardened case opens ``PROVISIONAL_LOCK`` and a named confirmer locks
  it through CHP third-party validation;
- every mint seals a CHP payload envelope into the append-only decision
  ledger, whose reads re-validate envelope integrity, and anchors the sealed
  record's id and digest onto the minted CreditNFT.

Measurement parity is exercised against the verification form's declared
measurement data (``estimated_credits``): the mocked LLM recommends 9000
against a declared 10000, which sits inside the 20% MRV tolerance.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chp import Verdict
from chp.models import SessionStatus
from fastapi.testclient import TestClient
from greenverify.api import create_app
from greenverify.chp import ChpMintGate, ChpRejection
from greenverify.engines.verifier import VerificationEngine
from greenverify.models.carbon import VerificationFormData
from greenverify.services.market_data import GreenMarketDataService

CONFIRMER = "sam@cubiczan.com"
DECLARED_CREDITS = 10000
RECOMMENDED_CREDITS = 9000  # within the 20% MRV parity tolerance

DOC_WITH_MEASUREMENTS = (
    "Project Documentation for Test Reforestation Project\n\n"
    "1. Project Description\n"
    "This project involves reforesting 500 hectares of degraded land "
    "in the state of Para, Brazil. Native species including Brazil nut "
    "and Ipe will be planted. The project follows VCS methodology AR-ACM0003 "
    "and has third-party monitoring by SGS.\n\n"
    "2. Additionality\n"
    "Without this project, the degraded land would remain unproductive "
    "and continue to emit CO2 through soil degradation. Baseline scenario "
    "shows no natural regeneration expected within 30 years.\n\n"
    "3. Monitoring Plan\n"
    "Annual monitoring using satellite imagery and ground surveys. "
    "Tree survival rates and carbon stock measurements recorded quarterly."
)

DOC_WITHOUT_MEASUREMENTS = (
    "A narrative description of a wonderful environmental initiative that "
    "describes its goals in broad strokes without any quantitative evidence."
)

FULL_LLM_RESULT = {
    "score": 85,
    "risk_level": "Low",
    "assessment": "Strong additionality with third-party monitoring and measurable reductions.",
    "recommendations": ["Expand monitoring coverage"],
    "credit_amount_recommended": RECOMMENDED_CREDITS,
    "pass_fail": True,
}


def make_form(
    documentation_text: str = DOC_WITH_MEASUREMENTS,
    estimated_credits: int = DECLARED_CREDITS,
    name: str = "Test Reforestation Project",
    credit_standard: str = "VCS",
) -> VerificationFormData:
    return VerificationFormData(
        name=name,
        description="A reforestation project that plants native trees on degraded land.",
        project_type="Reforestation",
        country="Brazil",
        vintage_year=2024,
        estimated_credits=estimated_credits,
        credit_standard=credit_standard,
        documentation_text=documentation_text,
    )


def make_gate(tmp_path: Path, require_lock: bool = False) -> ChpMintGate:
    return ChpMintGate(
        tmp_path / "chp_decisions.jsonl", require_human_lock=require_lock
    )


def make_engine(
    tmp_path: Path,
    llm_result: dict,
    require_lock: bool = False,
) -> tuple[VerificationEngine, ChpMintGate]:
    gate = make_gate(tmp_path, require_lock=require_lock)
    qwen = MagicMock()
    qwen.verify_carbon_project = AsyncMock(return_value=llm_result)
    engine = VerificationEngine(
        qwen_client=qwen, market_data=GreenMarketDataService(), chp_gate=gate
    )
    return engine, gate


# ----------------------------------------------------------------------- R0


def test_r0_refuses_a_request_without_measurement_evidence(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    with pytest.raises(ChpRejection) as excinfo:
        gate.open_r0(make_form(documentation_text=DOC_WITHOUT_MEASUREMENTS))
    assert excinfo.value.evaluation.results["Worth_it"] == "FATAL"


def test_r0_refuses_an_empty_request(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    with pytest.raises(ChpRejection) as excinfo:
        gate.open_r0(make_form(name="   ", documentation_text=" " * 60))
    assert excinfo.value.evaluation.results["Solvable"] == "FATAL"


def test_r0_accepts_a_well_formed_verification(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    evaluation = gate.open_r0(make_form())
    assert evaluation.verdict == Verdict.PASS


# ---------------------------------------------------------------- foundation


def test_measurement_parity_scores_a_full_blockchain_foundation(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    form = make_form()
    assessment = gate.assess_foundation(
        name=form.name,
        documentation_text=form.documentation_text,
        estimated_credits=form.estimated_credits,
        credit_standard=form.credit_standard,
        country=form.country,
        vintage_year=form.vintage_year,
        llm_result=FULL_LLM_RESULT,
    )
    assert assessment.domain == "blockchain"
    assert assessment.score == 100
    assert assessment.parity is not None
    assert assessment.parity.within_tolerance is True


def test_measurement_parity_mismatch_is_fatal(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    mismatched = dict(FULL_LLM_RESULT, credit_amount_recommended=999000)
    with pytest.raises(ChpRejection, match="MISMATCH"):
        gate.harden(request_id="req_mismatch", form_data=make_form(), llm_result=mismatched)


def test_unverifiable_amount_passes_without_parity(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    form = make_form(estimated_credits=0)
    assessment = gate.assess_foundation(
        name=form.name,
        documentation_text=form.documentation_text,
        estimated_credits=form.estimated_credits,
        credit_standard=form.credit_standard,
        country=form.country,
        vintage_year=form.vintage_year,
        llm_result=FULL_LLM_RESULT,
    )
    assert assessment.score == 70  # guardrails 40 + bounded result 30; no parity evidence


def test_malformed_llm_result_cannot_self_certify(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    form = make_form()
    assessment = gate.assess_foundation(
        name=form.name,
        documentation_text=form.documentation_text,
        estimated_credits=form.estimated_credits,
        credit_standard=form.credit_standard,
        country=form.country,
        vintage_year=form.vintage_year,
        llm_result={"score": "high", "assessment": "", "recommended_credit_amount": None},
    )
    assert assessment.score == 40


def test_hardened_case_opens_provisional_and_locks_with_a_confirmer(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    decision = gate.harden(
        request_id="req_lock", form_data=make_form(), llm_result=FULL_LLM_RESULT
    )
    assert decision.case.status == SessionStatus.PROVISIONAL_LOCK
    assert gate.lock(decision, CONFIRMER) == SessionStatus.LOCKED


# -------------------------------------------------------------------- ledger


def hardened_record(gate: ChpMintGate) -> dict:
    decision = gate.harden(
        request_id="req_ledger", form_data=make_form(), llm_result=FULL_LLM_RESULT
    )
    return gate.record(
        decision,
        request_id="req_ledger",
        project_id="proj_test",
        mint_metadata={"token_id": "nft_test", "amount": RECOMMENDED_CREDITS},
        confirmed_by=None,
    )


def test_decision_record_seals_an_envelope(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    record = hardened_record(gate)

    listing = gate.records.list()
    assert len(listing) == 1
    assert listing[0]["envelope_valid"] is True
    assert listing[0]["decision_id"] == record["decision_id"]
    assert gate.records.get(record["decision_id"])["request_id"] == "req_ledger"
    assert gate.records.get("mint-missing") is None


def test_tampered_ledger_records_read_as_invalid(tmp_path: Path) -> None:
    gate = make_gate(tmp_path)
    sub_floor = dict(FULL_LLM_RESULT, credit_amount_recommended=0)
    gate.record(
        gate.harden(
            request_id="req_tamper",
            form_data=make_form(estimated_credits=0),
            llm_result=sub_floor,
        ),
        request_id="req_tamper",
        project_id="proj_test",
        mint_metadata={"token_id": "nft_test", "amount": 0},
        confirmed_by=None,
    )

    path = gate.records.path
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    # tamper with the sealed payload body: inflate the foundation score
    entry["body"] = entry["body"].replace('"foundation_score": 70', '"foundation_score": 100')
    lines[0] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    record = gate.records.list()[0]
    assert record["integrity_valid"] is False
    assert record["envelope_valid"] is True  # the CHP envelope checks structure only


# ------------------------------------------------------- verify→mint pipeline


async def test_mint_requires_the_hardened_decision(tmp_path: Path) -> None:
    engine, gate = make_engine(tmp_path, FULL_LLM_RESULT)
    with pytest.raises(ValueError, match="No hardened CHP decision"):
        engine.mint_credit("req_unknown", owner="0xOwner")
    assert gate.records.list() == []


async def test_require_human_lock_refuses_unconfirmed_mints(tmp_path: Path) -> None:
    engine, gate = make_engine(tmp_path, FULL_LLM_RESULT, require_lock=True)
    result = await engine.submit_verification(make_form())
    with pytest.raises(ChpRejection, match="human lock"):
        engine.mint_credit(result.request_id, owner="0xOwner")
    assert gate.records.list() == []  # nothing minted, nothing recorded
    assert len(engine._market_data.get_credits()) == 5  # only the seeded demo credits


async def test_a_named_confirmer_locks_and_mints(tmp_path: Path) -> None:
    engine, gate = make_engine(tmp_path, FULL_LLM_RESULT, require_lock=True)
    result = await engine.submit_verification(make_form())

    credit = engine.mint_credit(
        result.request_id, owner="0xOwner", confirmed_by=CONFIRMER, tx_hash="0xmocktx"
    )

    assert credit.chp_decision_id == f"mint-{result.request_id}"
    assert credit.chp_body_sha256 is not None
    assert credit.onchain_tx_hash == "0xmocktx"  # mocked chain
    record = gate.records.get(credit.chp_decision_id)
    assert record is not None
    assert record["session_status"] == SessionStatus.LOCKED.value
    assert record["confirmed_by"] == CONFIRMER
    assert record["integrity_valid"] is True
    assert record["mint_metadata"]["amount"] == RECOMMENDED_CREDITS


async def test_sub_floor_mint_cannot_self_certify_at_the_blockchain_floor(
    tmp_path: Path,
) -> None:
    # No declared measurement -> parity unavailable -> foundation 70 < 85.
    engine, gate = make_engine(tmp_path, FULL_LLM_RESULT, require_lock=False)
    result = await engine.submit_verification(make_form(estimated_credits=0))

    with pytest.raises(ChpRejection, match="cannot self-certify"):
        engine.mint_credit(result.request_id, owner="0xOwner")
    assert gate.records.list() == []

    credit = engine.mint_credit(result.request_id, owner="0xOwner", confirmed_by=CONFIRMER)
    assert gate.records.get(credit.chp_decision_id)["foundation_score"] == 70


async def test_full_parity_mint_self_certifies_at_the_blockchain_floor(
    tmp_path: Path,
) -> None:
    engine, _gate = make_engine(tmp_path, FULL_LLM_RESULT, require_lock=False)
    result = await engine.submit_verification(make_form())
    credit = engine.mint_credit(result.request_id, owner="0xOwner")
    assert credit.chp_decision_id is not None
    assert engine._chp_gate.records.get(credit.chp_decision_id)["foundation_score"] == 100


async def test_parity_mismatch_refuses_the_verification(tmp_path: Path) -> None:
    engine, gate = make_engine(tmp_path, dict(FULL_LLM_RESULT, credit_amount_recommended=500000))
    with pytest.raises(ChpRejection, match="MISMATCH"):
        await engine.submit_verification(make_form())
    # FATAL: nothing persisted, nothing recorded (22 projects are seeded demo data)
    assert gate.records.list() == []
    assert len(engine._market_data.get_all_projects()) == 22
    assert len(engine._market_data.get_credits()) == 5


# ------------------------------------------------------------- env / from_env


def test_from_env_defaults_human_lock_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREENVERIFY_CHP_DECISIONS_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK", raising=False)
    gate = ChpMintGate.from_env()
    assert gate.require_human_lock is True


def test_from_env_honours_the_lock_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GREENVERIFY_CHP_DECISIONS_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK", "0")
    assert ChpMintGate.from_env().require_human_lock is False
    monkeypatch.setenv("GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK", "1")
    assert ChpMintGate.from_env().require_human_lock is True


# ------------------------------------------------------------ API integration


def test_verify_then_mint_integration_via_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREENVERIFY_CHP_DECISIONS_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK", "1")

    app = create_app(market_data=GreenMarketDataService())
    client = TestClient(app)

    with patch("greenverify.engines.verifier.QwenClient") as mock_qwen_cls:
        mock_instance = MagicMock()
        mock_instance.verify_carbon_project = AsyncMock(return_value=FULL_LLM_RESULT)
        mock_qwen_cls.return_value = mock_instance

        # Verify: the decision trail summary rides on the verification result.
        verify_response = client.post(
            "/api/verify",
            json={
                "name": "Test Reforestation Project",
                "description": "A reforestation project that plants native trees on degraded land.",
                "project_type": "Reforestation",
                "country": "Brazil",
                "vintage_year": 2024,
                "estimated_credits": DECLARED_CREDITS,
                "credit_standard": "VCS",
                "documentation_text": DOC_WITH_MEASUREMENTS,
            },
        )
        assert verify_response.status_code == 200
        verification = verify_response.json()
        assert verification["chp_decision_id"] == f"mint-{verification['request_id']}"
        assert verification["chp_session_status"] == SessionStatus.PROVISIONAL_LOCK.value
        assert verification["chp_foundation_score"] == 100

        # Mint without a confirmer: the human lock refuses (nothing mints).
        locked_response = client.post(
            "/api/credits/mint",
            json={
                "request_id": verification["request_id"],
                "owner": "0xOwner",
            },
        )
        assert locked_response.status_code == 422

        # Mint with a named confirmer: LOCKED, chain mocked via tx_hash.
        mint_response = client.post(
            "/api/credits/mint",
            json={
                "request_id": verification["request_id"],
                "owner": "0xOwner",
                "confirmed_by": CONFIRMER,
                "tx_hash": "0xmocktx",
            },
        )
        assert mint_response.status_code == 200
        credit = mint_response.json()
        assert credit["chp_decision_id"] == verification["chp_decision_id"]
        assert credit["chp_body_sha256"]  # anchored digest, non-empty
        assert credit["onchain_tx_hash"] == "0xmocktx"

    # The decision ledger exposes the tamper-evident trail.
    decisions = client.get("/api/decisions").json()
    assert len(decisions) == 1
    assert decisions[0]["integrity_valid"] is True
    assert decisions[0]["session_status"] == SessionStatus.LOCKED.value
    fetched = client.get(f"/api/decisions/{credit['chp_decision_id']}").json()
    assert fetched["integrity_valid"] is True
    assert fetched["body_sha256"] == credit["chp_body_sha256"]
