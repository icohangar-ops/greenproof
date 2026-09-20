"""CHP-hardened mint gate (consensus-hardening-protocol, ported from the
erp-control-plane GenBI promotion gate, commit 70678cc).

Every verification that can end in a minted on-chain credit becomes a CHP
decision case, so the question "why was this credit minted?" has a mechanical
answer. Four hardening stages wrap the verify→mint path:

1. **R0 gate — before the verification engine.** ``evaluate_r0_gate`` with
   verification-shaped criteria: the request is *solvable* (project name and
   documentation are non-empty), *scoped* (documentation and credit estimate
   are bounded), *valid* (standard, country, and vintage are well-formed
   project data), and *worth_it* (the documentation carries quantitative
   measurement evidence — without measurements there is nothing to verify
   against). HALT refuses the verification with nothing executed or persisted.
2. **Foundation pass — after the LLM scoring.** The deterministic adversary
   scores the verification's foundation out of 100: 40 for input guardrails,
   30 for a well-formed bounded result, and 30 for measurement parity — the
   recommended credit amount matching the project's declared measurement data
   (``estimated_annual_credits``) within a 20% MRV-style uncertainty band.
   Domain is ``blockchain`` because the decision ends in on-chain asset
   issuance, so CHP gates it at the blockchain foundation floor of **85**
   (mirrors ``.chp/R0_CONFIG.yaml`` ``foundation.pass_threshold: 85``):
   a 70-score general-domain decision may self-certify elsewhere, but a
   minted credit cannot — without full parity evidence (guardrails + result
   + parity = 100) a named human confirmer is required. A parity *mismatch*
   is fatal — a verification contradicting the project's own measurement
   data must not persist, and no confirmer can wave it through.
3. **Human lock.** Sessions start EXPLORING; a hardened case is collapsed to
   PROVISIONAL_LOCK before ``apply_third_party_validation``; a named
   ``confirmed_by`` locks it (LOCKED). ``GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK=1``
   (default ON) makes that confirmation mandatory for every mint — nothing
   mints without approval while the flag is set.
4. **Decision record.** The case, verdicts, parity evidence, and mint
   metadata are serialised into a CHP payload envelope and appended to the
   decision ledger (JSONL). The CHP envelope validates structure only, so
   the ledger adds its own SHA-256 body digest, re-validated on every read;
   tampered records surface as ``integrity_valid: false``. The sealed
   record's id and digest are anchored onto the minted CreditNFT.

The on-chain mint itself is executed by the ink! contract
(``contracts/carbon-credit``). The Python path mints the platform's CreditNFT
representation and anchors the decision trail onto it; where no contract
transaction exists yet the recorded ``onchain_tx_hash`` is a deterministic
mock derived from the sealed body — no live chain integration is claimed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chp import (
    CHPOrchestrator,
    CHPReport,
    DecisionCase,
    Dossier,
    FoundationAttack,
    FoundationDisclosure,
    ThirdPartyValidation,
    ValidationResult,
    Verdict,
    apply_third_party_validation,
    build_payload_envelope,
    validate_payload_envelope,
)
from chp.gates import GateEvaluation, evaluate_r0_gate
from chp.models import SessionStatus

# Deterministic adversary scoring (out of 100). Domain "blockchain" gates at
# CHP's blockchain floor of 85, so only a parity-verified verification (100)
# can self-certify a mint; sub-floor mints need a named human confirmer.
_GUARDRAIL_POINTS = 40
_BOUNDED_RESULT_POINTS = 30
_PARITY_POINTS = 30
_FULL_SCORE = _GUARDRAIL_POINTS + _BOUNDED_RESULT_POINTS + _PARITY_POINTS

# On-chain asset issuance: the foundation domain whose CHP floor is 85.
_BLOCKCHAIN_DOMAIN = "blockchain"
_BLOCKCHAIN_FLOOR_NOTE = "85 (on-chain asset issuance)"

# Parity tolerance: carbon MRV practice accepts roughly ±20% measurement
# uncertainty; the recommended credit amount must reproduce the project's
# declared measurement data within this band.
_PARITY_TOLERANCE = 0.20

# Input guardrails: bounded verification scope.
_MAX_DOCUMENTATION_CHARS = 100_000
_MAX_ESTIMATED_CREDITS = 10_000_000

# R0 worth_it proxy: the documentation must carry quantitative measurement
# evidence (tonnage, hectares, capacity, emission figures) to be verifiable.
_MEASUREMENT_EVIDENCE = re.compile(
    r"\b\d+([.,]\d+)?\s*(t|tonnes|tons|ha|hectares|mw|gw|gwh|mwh|kwh|"
    r"co2e?|m3|m³|trees|%)\b",
    re.IGNORECASE,
)

_REQUIRE_HUMAN_LOCK_ENV = "GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK"
_DECISIONS_PATH_ENV = "GREENVERIFY_CHP_DECISIONS_PATH"
_DEFAULT_DECISIONS_PATH = ".chp_registry.jsonl"


class ChpRejection(Exception):
    """CHP refused the verification/mint (R0 HALT, parity mismatch, floor, or lock)."""

    def __init__(self, reason: str, evaluation: GateEvaluation | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evaluation = evaluation


@dataclass(frozen=True)
class ParityEvidence:
    """The verification's recommended credits vs the declared measurement data."""

    case_id: str
    metric: str
    unit: str
    expected: float
    tolerance: float
    actual: float | None  # None = no comparable credit amount to check
    within_tolerance: bool | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FoundationAssessment:
    """The deterministic adversary's verdict on a verification result."""

    score: int
    domain: str
    findings: list[str] = field(default_factory=list)
    parity: ParityEvidence | None = None
    parity_matched: bool = False


@dataclass(frozen=True)
class ChpDecision:
    """A hardened verification: the CHP case, its report, and the assessment."""

    case: DecisionCase
    report: CHPReport
    assessment: FoundationAssessment


class DecisionLedger:
    """Append-only JSONL of CHP decision records; envelope integrity re-checked on read."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _read_all(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first records with envelope and body integrity re-validated on read."""
        return [self._checked(entry) for entry in self._read_all()[-limit:]][::-1]

    def get(self, decision_id: str) -> dict[str, Any] | None:
        for entry in reversed(self._read_all()):
            if entry.get("decision_id") == decision_id:
                return self._checked(entry)
        return None

    @staticmethod
    def _checked(entry: dict[str, Any]) -> dict[str, Any]:
        """Re-validate a record on read: envelope structure and body digest.

        The CHP payload envelope validates structure only, so the ledger adds
        its own SHA-256 digest over the sealed body — a tampered record reads
        as ``integrity_valid: false``.
        """
        body = entry.get("body", "")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return {
            **entry,
            "envelope_valid": validate_payload_envelope(entry.get("envelope", "")),
            "integrity_valid": digest == entry.get("body_sha256"),
        }


def _input_guardrail_findings(
    *,
    name: str,
    documentation_text: str,
    estimated_credits: int,
    credit_standard: str,
    country: str,
    vintage_year: int,
) -> list[str]:
    """Deterministic input guardrails for the verification request."""
    findings: list[str] = []
    if name.strip():
        findings.append("guardrails passed: project name non-empty")
    if 0 < len(documentation_text.strip()) <= _MAX_DOCUMENTATION_CHARS:
        findings.append(
            f"guardrails passed: documentation bounded ({len(documentation_text)} chars"
            f" ≤ {_MAX_DOCUMENTATION_CHARS})"
        )
    if 0 <= estimated_credits <= _MAX_ESTIMATED_CREDITS:
        findings.append(
            f"guardrails passed: credit estimate bounded ({estimated_credits} tCO2e"
            f" ≤ {_MAX_ESTIMATED_CREDITS})"
        )
    if credit_standard.strip():
        findings.append(f"guardrails passed: credit standard declared ({credit_standard})")
    if country.strip() and 2000 <= vintage_year <= 2035:
        findings.append(f"guardrails passed: project data well-formed ({country}, {vintage_year})")
    return findings


def _has_measurement_evidence(documentation_text: str) -> bool:
    return _MEASUREMENT_EVIDENCE.search(documentation_text) is not None


class ChpMintGate:
    """Runs a verification→mint request through CHP: R0 -> foundation -> human lock -> record."""

    def __init__(self, ledger_path: Path, *, require_human_lock: bool = True) -> None:
        self.records = DecisionLedger(ledger_path)
        self.require_human_lock = require_human_lock
        self._pending: dict[str, ChpDecision] = {}
        self._pending_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> ChpMintGate:
        """Build a gate from environment configuration.

        ``GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK`` defaults ON: when set (or
        unset), every mint needs a named confirmer.
        """
        raw = os.getenv(_REQUIRE_HUMAN_LOCK_ENV)
        if raw is None:
            require_lock = True
        else:
            require_lock = raw.strip().lower() in {"1", "true", "yes", "on"}
        path = Path(os.getenv(_DECISIONS_PATH_ENV, _DEFAULT_DECISIONS_PATH))
        return cls(path, require_human_lock=require_lock)

    # ------------------------------------------------------------------- R0
    def open_r0(self, form_data: Any) -> GateEvaluation:
        """The pre-execution gate: HALT before the LLM engine sees the request."""
        evaluation = evaluate_r0_gate(
            solvable=bool(form_data.name.strip()) and bool(form_data.documentation_text.strip()),
            scoped=(
                0 < len(form_data.documentation_text.strip()) <= _MAX_DOCUMENTATION_CHARS
                and 0 <= form_data.estimated_credits <= _MAX_ESTIMATED_CREDITS
            ),
            valid=(
                bool(form_data.credit_standard.strip())
                and bool(form_data.country.strip())
                and 2000 <= form_data.vintage_year <= 2035
            ),
            worth_it=_has_measurement_evidence(form_data.documentation_text),
        )
        if evaluation.verdict != Verdict.PASS:
            failed = [name for name, result in evaluation.results.items() if result != "PASS"]
            raise ChpRejection(
                "CHP R0 gate: the verification request failed " + ", ".join(sorted(failed)),
                evaluation,
            )
        return evaluation

    # ------------------------------------------------------------ foundation
    def assess_foundation(
        self,
        *,
        name: str,
        documentation_text: str,
        estimated_credits: int,
        credit_standard: str,
        country: str,
        vintage_year: int,
        llm_result: dict,
    ) -> FoundationAssessment:
        """The deterministic adversary scores the verification result (0-100)."""
        findings: list[str] = []

        guardrail_findings = _input_guardrail_findings(
            name=name,
            documentation_text=documentation_text,
            estimated_credits=estimated_credits,
            credit_standard=credit_standard,
            country=country,
            vintage_year=vintage_year,
        )
        complete_guardrails = len(guardrail_findings) == 5
        if complete_guardrails:
            score = _GUARDRAIL_POINTS
        else:
            score = 0
            findings.append("input guardrails incomplete — verification scope not bounded")

        # Bounded result: the LLM returned a well-formed, in-range verification.
        bounded, bounded_finding = _bounded_result_finding(llm_result)
        if bounded:
            score += _BOUNDED_RESULT_POINTS
            findings.append(bounded_finding)
        else:
            findings.append(bounded_finding)

        # Parity vs measurement data: the recommended credit amount must be
        # reproducible from the project's declared measurements.
        expected = float(estimated_credits)
        actual_raw = llm_result.get("credit_amount_recommended")
        if actual_raw is None:
            actual_raw = llm_result.get("recommended_credit_amount")
        actual = float(actual_raw) if isinstance(actual_raw, (int, float)) else None

        parity: ParityEvidence | None = None
        if expected <= 0:
            findings.append(
                "no declared measurement to verify against (estimated credits is zero)"
                " — parity evidence unavailable"
            )
        elif actual is None:
            findings.append(
                "no comparable recommended credit amount — parity evidence unavailable"
            )
        else:
            parity = ParityEvidence(
                case_id="estimated_annual_credits",
                metric="credit_amount_recommended",
                unit="tco2e",
                expected=expected,
                tolerance=_PARITY_TOLERANCE,
                actual=actual,
                within_tolerance=abs(actual - expected) / expected <= _PARITY_TOLERANCE,
            )
            if parity.within_tolerance:
                score += _PARITY_POINTS
                findings.append(
                    f"measurement parity: recommended {parity.actual:g} {parity.unit} within"
                    f" {parity.tolerance:.0%} of declared {parity.expected:g} {parity.unit}"
                )
            else:
                findings.append(
                    f"measurement parity MISMATCH: recommended {parity.actual:g}"
                    f" {parity.unit} is outside {parity.tolerance:.0%} of declared"
                    f" {parity.expected:g} {parity.unit}"
                )

        return FoundationAssessment(
            score=min(score, _FULL_SCORE),
            domain=_BLOCKCHAIN_DOMAIN,
            findings=findings,
            parity=parity,
            parity_matched=parity is not None and parity.within_tolerance is True,
        )

    # ---------------------------------------------------------------- session
    def harden(
        self,
        *,
        request_id: str,
        form_data: Any,
        llm_result: dict,
    ) -> ChpDecision:
        """Run the CHP foundation pass and open the case as PROVISIONAL_LOCK.

        A measurement-parity mismatch is fatal: the verification must not
        persist and no confirmer can wave it through.
        """
        assessment = self.assess_foundation(
            name=form_data.name,
            documentation_text=form_data.documentation_text,
            estimated_credits=form_data.estimated_credits,
            credit_standard=form_data.credit_standard,
            country=form_data.country,
            vintage_year=form_data.vintage_year,
            llm_result=llm_result,
        )
        if assessment.parity is not None and assessment.parity.within_tolerance is False:
            raise ChpRejection(
                f"CHP foundation: {assessment.findings[-1]} — a verification contradicting"
                " the project's declared measurement data must not proceed to minting;"
                " correct the verification or the project measurements."
            )

        case = DecisionCase(
            decision_id=f"mint-{request_id}",
            title=f"Mint credit for verification {request_id}",
            domain=assessment.domain,
            created_at=datetime.now(timezone.utc).isoformat(),
            owner="greenverify-mint",
            high_stakes=True,  # on-chain asset issuance
            dossier=Dossier(
                core_problem=(
                    f"Mint the verified carbon credit for project submission: {form_data.name}"
                ),
                goal_state=[
                    "anchor the verification decision trail underneath the minted credit"
                ],
                current_state=[
                    f"verification scored {assessment.score}/100 by the deterministic adversary",
                    f"parity vs declared measurement data: {assessment.findings[-1]}",
                ],
                constraints=[
                    f"blockchain foundation floor {_BLOCKCHAIN_FLOOR_NOTE}",
                    "on-chain issuance via the ink! carbon-credit contract",
                ],
                scope=[
                    f"request_id:{request_id}",
                    f"credit_standard:{form_data.credit_standard}",
                    f"vintage_year:{form_data.vintage_year}",
                ],
            ),
        )
        disclosure = FoundationDisclosure(
            weakest_assumptions=[
                "the LLM verification faithfully assessed the project documentation",
                "the project's declared measurements (estimated annual credits) are accurate",
                "the Qwen API response is parsed and persisted unmodified",
            ],
            invalidation_conditions=[
                (
                    "recommended credit amount contradicts the declared measurement beyond"
                    " the MRV tolerance"
                ),
                "verification score out of range or assessment narrative empty",
            ],
            key_vulnerability=(
                "single-source parity: the recommended amount is only checked against"
                " the project's own declared measurement"
                if assessment.parity is None
                else f"single-source parity: only the declared {assessment.parity.metric}"
                " anchors this verification"
            ),
        )
        # The adversary must address each disclosed weak assumption
        # (validate_foundation_pair requires min(3, len(assumptions)) attacks).
        attack = FoundationAttack(
            attack_summary="; ".join(assessment.findings),
            foundation_score=assessment.score,
            vulnerability_strike=(
                "below full parity evidence the verification rests only on structural"
                " guardrails, not on the project's measurement data — an on-chain asset"
                " minted from it needs a named human confirmer"
            ),
            assumption_attacks=[
                "measurement parity: recommended amount vs declared annual credits",
                "documentation bounding: guardrails cap the verification scope server-side",
                "parsing integrity: the structured result is validated before persistence",
            ],
        )

        # Fresh orchestrator per case: the protocol registry is in-memory state
        # we do not rely on — the decision ledger is the durable record.
        report = CHPOrchestrator().run_initial_session(
            case=case, foundation_disclosure=disclosure, foundation_attack=attack
        )

        # The gate collapses CHP's multi-round phase flow into one mint step:
        # every hardened verification opens as a provisional decision pending
        # human confirmation (which apply_third_party_validation then locks).
        # A REFRAME verdict (below the blockchain floor) keeps that status too
        # — the mint may only proceed through the same human lock, never
        # self-certify.
        case.status = SessionStatus.PROVISIONAL_LOCK
        decision = ChpDecision(case, report, assessment)
        with self._pending_lock:
            self._pending[case.decision_id] = decision
        return decision

    def pending(self, decision_id: str) -> ChpDecision | None:
        with self._pending_lock:
            return self._pending.get(decision_id)

    # ------------------------------------------------------------- human lock
    def lock(self, decision: ChpDecision, confirmed_by: str) -> SessionStatus:
        """Third-party confirmation: PROVISIONAL_LOCK -> LOCKED (recorded in the case)."""
        return apply_third_party_validation(
            decision.case,
            ThirdPartyValidation(
                validator=confirmed_by,
                item=decision.case.decision_id,
                challenge=(
                    "Confirm the verification is scoped, solvable from the project's"
                    " measurement data, and safe to mint on-chain"
                ),
                result=ValidationResult.CONFIRM,
                rationale="Named confirmer approved the mint via the GreenVerify API",
            ),
        )

    # ----------------------------------------------------------------- record
    def record(
        self,
        decision: ChpDecision,
        *,
        request_id: str,
        project_id: str,
        mint_metadata: dict[str, Any],
        confirmed_by: str | None,
    ) -> dict[str, Any]:
        """Seal the decision into a CHP payload envelope and append the ledger."""
        case = decision.case
        parity = decision.assessment.parity
        body = json.dumps(
            {
                "decision_id": case.decision_id,
                "title": case.title,
                "domain": case.domain,
                "request_id": request_id,
                "project_id": project_id,
                "r0_verdict": decision.report.r0_verdict.value,
                "foundation_verdict": decision.report.foundation_verdict.value,
                "foundation_score": case.foundation_score,
                "adversary_findings": decision.assessment.findings,
                "parity": parity.to_dict() if parity else None,
                "mint_metadata": mint_metadata,
                "locked_decisions": list(case.locked_decisions),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        envelope = build_payload_envelope(body, route="MINT")
        entry = {
            "decision_id": case.decision_id,
            "created_at": case.created_at,
            "request_id": request_id,
            "project_id": project_id,
            "session_status": case.status.value,
            "r0_verdict": decision.report.r0_verdict.value,
            "foundation_verdict": decision.report.foundation_verdict.value,
            "foundation_score": case.foundation_score,
            "confirmed_by": confirmed_by,
            "mint_metadata": mint_metadata,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "envelope": envelope.render(),
        }
        self.records.append(entry)
        return entry


def _bounded_result_finding(llm_result: dict) -> tuple[bool, str]:
    """A well-formed bounded result: score in range, non-empty assessment, non-negative amount."""
    score = llm_result.get("score")
    assessment = llm_result.get("assessment")
    amount_raw = llm_result.get("credit_amount_recommended")
    if amount_raw is None:
        amount_raw = llm_result.get("recommended_credit_amount")
    amount_ok = isinstance(amount_raw, (int, float)) and amount_raw >= 0
    if (
        isinstance(score, int)
        and not isinstance(score, bool)
        and 0 <= score <= 100
        and isinstance(assessment, str)
        and assessment.strip()
        and amount_ok
    ):
        return True, f"bounded result: score {score}/100 with a non-empty assessment narrative"
    return False, "LLM result is not a well-formed bounded verification — no result evidence"
