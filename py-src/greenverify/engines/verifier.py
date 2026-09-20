"""
Carbon credit verification engine.

Orchestrates the full verification pipeline: project creation, LLM-based
analysis via Qwen, result parsing, and verification result persistence.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from chp import Verdict

from ..chp import ChpMintGate, ChpRejection
from ..models.carbon import (
    CarbonProject,
    CreditNFT,
    ProjectStatus,
    RiskLevel,
    VerificationFormData,
    VerificationResult,
)
from ..services.market_data import GreenMarketDataService
from ..services.qwen_client import QwenClient

logger = logging.getLogger(__name__)


class VerificationEngine:
    """Orchestration engine for carbon credit verification workflows.

    Coordinates the full pipeline from form submission through AI analysis
    to final verification result. Integrates with the Qwen LLM client for
    AI-powered assessment and the market data service for persistence.

    Attributes:
        _qwen_client: Async client for the Qwen LLM.
        _market_data: Service for accessing and persisting demo data.
    """

    def __init__(
        self,
        qwen_client: QwenClient | None = None,
        market_data: GreenMarketDataService | None = None,
        chp_gate: ChpMintGate | None = None,
    ) -> None:
        """Initialise the verification engine.

        Args:
            qwen_client: An optional pre-configured QwenClient instance. If not
                         provided, a new one will be created from environment
                         variables.
            market_data: An optional pre-configured GreenMarketDataService. If
                         not provided, a new default instance will be created.
            chp_gate: An optional pre-configured CHP mint gate. If not provided,
                      one is built from environment configuration.
        """
        self._qwen_client: QwenClient = qwen_client or QwenClient()
        self._market_data: GreenMarketDataService = market_data or GreenMarketDataService()
        self._chp_gate: ChpMintGate = chp_gate or ChpMintGate.from_env()
        logger.info("VerificationEngine initialised")

    async def submit_verification(
        self,
        form_data: VerificationFormData,
    ) -> VerificationResult:
        """Submit a new carbon project for AI-powered verification.

        Executes the full verification pipeline:
        0. Runs the CHP R0 gate — HALT before the engine sees the request.
        1. Creates a CarbonProject from the form data.
        2. Calls the Qwen LLM to analyse the project documentation.
        3. Parses and validates the LLM's structured response.
        4. Runs the CHP foundation pass (deterministic adversary scoring with
           measurement parity; a parity mismatch is fatal) and opens a
           PROVISIONAL_LOCK decision case for the eventual mint.
        5. Persists the project and verification result.
        6. Returns the final VerificationResult.

        Args:
            form_data: The submitted verification form containing project
                       details and documentation text.

        Returns:
            A VerificationResult with the AI's assessment, score, risk
            classification, recommendations, and the CHP decision trail
            summary.

        Raises:
            RuntimeError: If the LLM analysis or result parsing fails.
            ValueError: If the form data is invalid.
            ChpRejection: If CHP refuses the request (R0 HALT or a
                          measurement-parity mismatch).
        """
        request_id = uuid.uuid4().hex
        logger.info(
            "Starting verification — request_id=%s, project=%s, type=%s",
            request_id,
            form_data.name,
            form_data.project_type,
        )

        # Step 0: CHP R0 gate — refuse ill-posed requests before the engine.
        self._chp_gate.open_r0(form_data)

        # Step 1: Create the CarbonProject
        project = CarbonProject(
            project_id=uuid.uuid4().hex,
            name=form_data.name,
            description=form_data.description,
            project_type=form_data.project_type,
            country=form_data.country,
            vintage_year=form_data.vintage_year,
            estimated_annual_credits=form_data.estimated_credits,
            credit_standard=form_data.credit_standard,
            status=ProjectStatus.VERIFYING,
        )

        # Step 2: Call Qwen for verification analysis
        logger.info("Calling Qwen LLM for verification analysis — project_id=%s", project.project_id)
        llm_result = await self._qwen_client.verify_carbon_project(
            documentation=form_data.documentation_text,
            project_type=form_data.project_type.value,
            country=form_data.country,
        )

        # Step 3: Parse and validate the result
        verification_result = self._build_verification_result(
            request_id=request_id,
            project_id=project.project_id,
            llm_result=llm_result,
        )

        # Step 3.5: CHP foundation pass — deterministic adversary scoring with
        # measurement parity. A parity mismatch is fatal: nothing is persisted.
        decision = self._chp_gate.harden(
            request_id=request_id,
            form_data=form_data,
            llm_result=llm_result,
        )
        verification_result.chp_decision_id = decision.case.decision_id
        verification_result.chp_session_status = decision.case.status.value
        verification_result.chp_foundation_score = decision.case.foundation_score

        # Step 4: Update project status based on verification outcome
        if verification_result.pass_fail:
            project.status = ProjectStatus.VERIFIED
            project.verified_at = verification_result.verified_at
        else:
            project.status = ProjectStatus.REJECTED
            project.verified_at = verification_result.verified_at

        # Step 5: Persist
        self._market_data.add_project(project)
        self._market_data.add_verification(verification_result)

        logger.info(
            "Verification complete — project_id=%s, score=%d, pass=%s",
            project.project_id,
            verification_result.score,
            verification_result.pass_fail,
        )

        return verification_result

    def mint_credit(
        self,
        request_id: str,
        owner: str,
        confirmed_by: str | None = None,
        tx_hash: str | None = None,
    ) -> CreditNFT:
        """Mint the credit NFT for a hardened verification (the verify→mint exit).

        Runs the mint through the CHP human lock:
        - ``GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK`` (default ON) — every mint
          needs a named ``confirmed_by``;
        - a sub-floor foundation verdict (below the blockchain floor of 85)
          cannot self-certify — it also needs a named ``confirmed_by``;
        - a confirmed decision is locked via CHP third-party validation
          (PROVISIONAL_LOCK -> LOCKED).

        The mint is then recorded in the decision ledger and the sealed
        record's id and body digest are anchored onto the CreditNFT.

        Args:
            request_id: The verification request to mint against.
            owner: Wallet address to receive the minted credit.
            confirmed_by: Identity of the human confirmer, when approving.
            tx_hash: Contract-side minting transaction hash. The on-chain
                mint itself is executed by the ink! carbon-credit contract;
                when no contract transaction is supplied (the current
                integration state), a deterministic mock hash derived from
                the sealed ledger body is recorded instead.

        Returns:
            The minted CreditNFT, anchored to its CHP decision record.

        Raises:
            ValueError: If no hardened verification exists for request_id.
            ChpRejection: If CHP refuses the mint (human lock or floor).
        """
        decision_id = f"mint-{request_id}"
        decision = self._chp_gate.pending(decision_id)
        if decision is None:
            raise ValueError(
                f"No hardened CHP decision exists for verification '{request_id}' —"
                " submit the project for verification first"
            )

        verification = self._market_data.get_verification(request_id)
        if verification is None:
            raise ValueError(f"Verification '{request_id}' not found")

        if self._chp_gate.require_human_lock and not confirmed_by:
            raise ChpRejection(
                "CHP human lock: GREENVERIFY_CHP_REQUIRE_HUMAN_LOCK is set —"
                " every mint needs a named confirmer (confirmed_by)."
            )
        if decision.report.foundation_verdict != Verdict.PASS and not confirmed_by:
            raise ChpRejection(
                f"CHP foundation: {decision.report.foundation_verdict.value}"
                f" (score {decision.case.foundation_score},"
                f" {decision.assessment.domain} domain, blockchain floor 85) —"
                " the mint cannot self-certify; retry with a named confirmer"
                " (confirmed_by)."
            )
        if confirmed_by:
            self._chp_gate.lock(decision, confirmed_by)

        project = self._market_data.get_project(verification.project_id)
        if project is None:
            raise ValueError(f"Project '{verification.project_id}' not found")

        mint_metadata: dict[str, Any] = {
            "token_id": f"nft_{uuid.uuid4().hex[:8]}",
            "owner": owner,
            "amount": verification.credit_amount_recommended,
            "score": verification.score,
            "credit_standard": project.credit_standard,
            "vintage_year": project.vintage_year,
        }
        record = self._chp_gate.record(
            decision,
            request_id=request_id,
            project_id=project.project_id,
            mint_metadata=mint_metadata,
            confirmed_by=confirmed_by,
        )

        credit = CreditNFT(
            token_id=mint_metadata["token_id"],
            project_id=project.project_id,
            owner=owner,
            amount=verification.credit_amount_recommended,
            vintage_year=project.vintage_year,
            credit_standard=project.credit_standard,
            project_name=project.name,
            project_type=project.project_type,
            country=project.country,
            minted_at=datetime.now(timezone.utc).isoformat(),
            onchain_tx_hash=tx_hash or f"0x{record['body_sha256'][:40]}",
            chp_decision_id=record["decision_id"],
            chp_body_sha256=record["body_sha256"],
        )
        self._market_data.add_credit(credit)

        logger.info(
            "Credit minted — token_id=%s, decision_id=%s, session_status=%s, confirmed_by=%s",
            credit.token_id,
            record["decision_id"],
            record["session_status"],
            confirmed_by,
        )
        return credit

    @staticmethod
    def _build_verification_result(
        request_id: str,
        project_id: str,
        llm_result: dict,
    ) -> VerificationResult:
        """Transform the raw LLM output into a validated VerificationResult.

        Args:
            request_id: The verification request identifier.
            project_id: The carbon project identifier.
            llm_result: The parsed dictionary from the Qwen LLM.

        Returns:
            A fully populated VerificationResult instance.

        Raises:
            ValueError: If the LLM result contains invalid risk_level.
        """
        # Parse and validate risk_level
        raw_risk = llm_result.get("risk_level", "Medium")
        try:
            risk_level = RiskLevel(raw_risk)
        except ValueError:
            logger.warning(
                "Invalid risk_level from LLM: %s, defaulting to Medium",
                raw_risk,
            )
            risk_level = RiskLevel.MEDIUM

        return VerificationResult(
            request_id=request_id,
            project_id=project_id,
            score=llm_result["score"],
            risk_level=risk_level,
            assessment=llm_result["assessment"],
            recommendations=llm_result.get("recommendations", []),
            credit_amount_recommended=llm_result.get(
                "credit_amount_recommended",
                llm_result.get("recommended_credit_amount", 0),
            ),
            pass_fail=llm_result["pass_fail"],
            verified_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def include_verify_prompt() -> str:
        """Return the system prompt used for carbon credit verification.

        This method is exposed for reference, testing, and documentation
        purposes. The actual prompt is used internally by the QwenClient.

        Returns:
            The system prompt string that instructs Qwen to act as a
            carbon credit verification expert.
        """
        return (
            "You are an expert carbon credit verification analyst working for GreenVerify AI. "
            "You have deep expertise in carbon markets, greenhouse gas accounting methodologies "
            "(IPCC, GHG Protocol), carbon credit standards (VCS, Gold Standard, CDM, American "
            "Carbon Registry, Climate Action Reserve), and environmental science. Your role is "
            "to rigorously analyse carbon credit project documentation and provide comprehensive "
            "verification assessments covering additionality, permanence, measurability, leakage, "
            "methodology appropriateness, documentation quality, and regulatory compliance."
        )
