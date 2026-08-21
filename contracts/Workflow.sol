// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import {AgentTask, AgentReply, RunStatus, IAgentWorkflow} from "@agent-ercs/execution/ERC8301/IAgentWorkflow.sol";
import {IBoundedAgentAction} from "@agent-ercs/metering/ERC8312/IBoundedAgentAction.sol";
import {IBudgetSubstrate} from "@agent-ercs/metering/ERC8312/IBudgetSubstrate.sol";
import {IAgentVerifier} from "@agent-ercs/verify/ERC8274/IAgentVerifier.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

interface ITAWGProfileRead {
    function identityRegistry() external view returns (address);

    function getAgent(uint256 agentId) external view returns (bool isMember, string memory data, address verifier);
}

interface IIdentityRegistryRead {
    function getAgentWallet(uint256 agentId) external view returns (address);
}

contract Workflow is IAgentWorkflow, IBudgetSubstrate {
    using SafeERC20 for IERC20;

    error ContributionLimitReached();
    error DuplicateContributionSource();
    error InvalidRoundInput();
    error InvalidReviewTarget();
    error InvalidScore();
    error InvalidProofBatch();
    error RestrictedBudgetMutation();
    error Unauthorized();
    error InvalidState();
    error UnknownRecord();
    error AlreadyCompleted();
    error InvalidReply();
    error Incomplete();
    error RoundBudgetExceeded();
    error ZeroPointsRecipient();
    error RoundNotReady();
    error InvalidConfiguration();
    error InvalidDataReference();

    bytes32 private constant EMPTY_INPUT_HASH = keccak256("");

    event AgentReplyAnchored(bytes32 indexed workflowRunId, bytes32 indexed replyHash, address indexed replier);

    enum RoundPhase {
        None,
        Open,
        Evaluating,
        Appealing,
        Settled
    }

    enum Stage {
        Collect,
        EvaluateContribution,
        AppealContribution,
        ReevaluateContribution,
        OpenAppealPhase,
        SummarizeRound,
        SettleRound
    }

    enum ContributionType {
        Work,
        Review
    }

    enum ActionKind {
        SubmitContribution,
        AppendSupportingMaterial,
        SubmitInitialEvaluation,
        SubmitAppeal,
        SubmitAppealEvaluation,
        SubmitRoundSummary,
        CloseCollection,
        OpenAppealPhase,
        SettleRound
    }

    enum EvaluationKind {
        Initial,
        Appeal
    }

    struct DataRef {
        string locator;
        bytes32 digest;
        uint64 expiresAt;
    }

    struct RoundView {
        RoundPhase phase;
        uint64 openedAt;
        uint64 minRolloverAt;
        uint64 appealDeadline;
        uint64 settledAt;
        uint32 contributionCount;
        uint32 initialEvaluatedCount;
        uint32 pendingAppealEvaluationCount;
        uint256 totalFinalScore;
        bytes32 collectTaskHash;
        bytes32 appealTaskHash;
    }

    struct ContributionView {
        bool exists;
        bytes32 workflowRunId;
        ContributionType contributionType;
        uint256 attributedAgentId;
        uint256 recorderAgentId;
        bytes32 sourceKey;
        DataRef source;
        bytes32 reviewedContributionId;
        bytes32 evaluateTaskHash;
        uint32 supportingMaterialCount;
        bytes32 supportingMaterialRoot;
        bool initialEvaluated;
        uint8 initialScore;
        uint8 finalScore;
        DataRef initialEvaluation;
        bytes32 initialEvaluationReplyHash;
        bool appealed;
        DataRef appeal;
        bytes32 appealReplyHash;
        bytes32 reevaluateTaskHash;
        bool appealEvaluated;
        uint8 appealScore;
        DataRef appealEvaluation;
        bytes32 appealEvaluationReplyHash;
    }

    struct EvaluationCandidate {
        bool exists;
        EvaluationKind kind;
        bytes32 contributionId;
        uint8 score;
        DataRef evaluation;
    }

    struct RunResult {
        RunStatus status;
        bytes32 finalTaskHash;
        uint256 completedAt;
    }

    address public immutable profile;
    address public immutable pointsToken;
    address public immutable passThroughVerifier;
    uint256 public immutable evaluatorAgentId;
    uint64 public immutable roundDuration;
    uint64 public immutable appealDuration;
    uint256 public immutable roundPointCap;
    uint32 public immutable maxContributionsPerRound;

    uint64 private _roundSequence;
    bytes32 public currentRoundId;

    mapping(bytes32 runId => RoundView round) private _rounds;
    mapping(bytes32 taskHash => AgentTask task) private _tasks;
    mapping(bytes32 taskHash => bool proven) private _taskProven;
    mapping(bytes32 replyHash => AgentReply reply) private _replies;
    mapping(bytes32 replyHash => address verifier) private _replyVerifier;
    mapping(bytes32 replyHash => bool proven) private _replyProven;
    mapping(bytes32 replyHash => bytes32 digest) private _replyVerificationDigest;
    mapping(bytes32 runId => uint256 nextSequence) private _nextTaskSequence;
    mapping(bytes32 runId => bytes32 replyHash) private _closeReplyHashes;
    mapping(bytes32 runId => bytes32 taskHash) private _openAppealTaskHashes;
    mapping(bytes32 runId => bytes32 replyHash) private _openAppealReplyHashes;
    mapping(bytes32 runId => bytes32 taskHash) private _summaryTaskHashes;
    mapping(bytes32 runId => bytes32 replyHash) private _summaryReplyHashes;
    mapping(bytes32 runId => bytes32 taskHash) private _settlementTaskHashes;
    mapping(bytes32 contributionId => ContributionView contribution) private _contributions;
    mapping(bytes32 sourceKey => bytes32 contributionId) public contributionBySourceKey;
    mapping(bytes32 replyHash => EvaluationCandidate candidate) private _evaluationCandidates;
    mapping(bytes32 runId => bytes32[] contributionIds) private _roundContributionIds;
    mapping(bytes32 runId => uint256[] agentIds) private _rewardedAgentIds;
    mapping(bytes32 runId => mapping(uint256 agentId => bool seen)) private _rewardedAgentSeen;
    mapping(bytes32 runId => mapping(uint256 agentId => uint256 score)) private _agentRoundScores;
    mapping(bytes32 runId => RunResult runResult) private _results;
    mapping(bytes32 envelopeId => IBoundedAgentAction.Envelope envelope) private _envelopes;
    mapping(bytes32 envelopeId => uint256 amount) private _spent;
    uint256 private _verifierCallActive;

    constructor(
        address profile_,
        address pointsToken_,
        address passThroughVerifier_,
        uint256 evaluatorAgentId_,
        uint64 roundDuration_,
        uint64 appealDuration_,
        uint256 roundPointCap_,
        uint32 maxContributionsPerRound_
    ) {
        if (
            profile_ == address(0) || pointsToken_ == address(0) || passThroughVerifier_.code.length == 0
                || roundDuration_ == 0 || appealDuration_ == 0 || roundPointCap_ == 0 || maxContributionsPerRound_ == 0
        ) revert InvalidConfiguration();
        (bool evaluatorIsMember,,) = ITAWGProfileRead(profile_).getAgent(evaluatorAgentId_);
        if (!evaluatorIsMember) revert InvalidConfiguration();

        profile = profile_;
        pointsToken = pointsToken_;
        passThroughVerifier = passThroughVerifier_;
        evaluatorAgentId = evaluatorAgentId_;
        roundDuration = roundDuration_;
        appealDuration = appealDuration_;
        roundPointCap = roundPointCap_;
        maxContributionsPerRound = maxContributionsPerRound_;
    }

    function run(bytes32 inputHash, bytes calldata input, uint256 expiresAt)
        external
        override
        returns (bytes32 workflowRunId)
    {
        if (inputHash != EMPTY_INPUT_HASH || input.length != 0 || expiresAt != type(uint256).max) {
            revert InvalidRoundInput();
        }

        bytes32 previousRoundId = currentRoundId;
        if (previousRoundId != bytes32(0)) {
            RoundView storage previousRound = _rounds[previousRoundId];
            if (previousRound.phase == RoundPhase.Open) revert RoundNotReady();
        }

        workflowRunId = keccak256(abi.encode(address(this), block.chainid, ++_roundSequence));
        currentRoundId = workflowRunId;

        AgentTask memory collectTask = AgentTask({
            stage: uint8(Stage.Collect),
            taskSeq: 0,
            inputHash: inputHash,
            input: input,
            timestamp: block.timestamp,
            expiresAt: expiresAt,
            prevReplyHashes: new bytes32[](0),
            workflowRunId: workflowRunId
        });
        bytes32 collectTaskHash = _hashTask(collectTask);
        _tasks[collectTaskHash] = collectTask;
        _taskProven[collectTaskHash] = true;
        _nextTaskSequence[workflowRunId] = 1;
        emit NewAgentTask(workflowRunId, uint8(Stage.Collect), collectTaskHash);

        _rounds[workflowRunId] = RoundView({
            phase: RoundPhase.Open,
            openedAt: uint64(block.timestamp),
            minRolloverAt: uint64(block.timestamp + roundDuration),
            appealDeadline: 0,
            settledAt: 0,
            contributionCount: 0,
            initialEvaluatedCount: 0,
            pendingAppealEvaluationCount: 0,
            totalFinalScore: 0,
            collectTaskHash: collectTaskHash,
            appealTaskHash: bytes32(0)
        });

        bytes32 capabilityRoot = keccak256(abi.encode(roundPointCap, pointsToken));
        _envelopes[workflowRunId] = IBoundedAgentAction.Envelope({
            id: workflowRunId,
            principal: address(this),
            capabilityRoot: capabilityRoot,
            cursorRoot: keccak256(abi.encode(uint256(0))),
            createdAt: uint64(block.timestamp),
            expiresAt: type(uint64).max,
            status: IBoundedAgentAction.Status.Active
        });
        emit EnvelopeRegistered(workflowRunId, address(this), capabilityRoot);
    }

    function getRound(bytes32 workflowRunId) external view returns (RoundView memory round) {
        return _rounds[workflowRunId];
    }

    function getRoundEvidence(bytes32 workflowRunId)
        external
        view
        returns (bytes32 summaryTaskHash, bytes32 summaryReplyHash, bytes32 settlementTaskHash)
    {
        return (
            _summaryTaskHashes[workflowRunId], _summaryReplyHashes[workflowRunId], _settlementTaskHashes[workflowRunId]
        );
    }

    function getContribution(bytes32 contributionId) external view returns (ContributionView memory contribution) {
        return _contributions[contributionId];
    }

    function _appendSupportingMaterial(AgentReply memory reply, bytes32 contributionId, DataRef memory material)
        private
        returns (bytes32 materialReplyHash)
    {
        _requireDataRef(material);
        ContributionView storage contribution = _contributions[contributionId];
        if (!contribution.exists) revert UnknownRecord();
        if (contribution.initialEvaluated) revert AlreadyCompleted();
        if (
            reply.workflowRunId != contribution.workflowRunId
                || reply.prevTaskHashes[0] != contribution.evaluateTaskHash
        ) revert InvalidReply();

        RoundPhase phase = _rounds[contribution.workflowRunId].phase;
        if (phase != RoundPhase.Open) revert InvalidState();
        _resolveContributionRecorder(contribution.attributedAgentId, msg.sender);

        materialReplyHash = _anchorProvidedReply(reply, passThroughVerifier, true);
        contribution.supportingMaterialRoot =
            keccak256(abi.encode(contribution.supportingMaterialRoot, materialReplyHash, material.digest));
        ++contribution.supportingMaterialCount;
        contribution.evaluateTaskHash = _createTask(
            contribution.workflowRunId,
            Stage.EvaluateContribution,
            abi.encode(contributionId, contribution.supportingMaterialRoot),
            materialReplyHash
        );
    }

    function _submitInitialEvaluation(
        AgentReply memory reply,
        bytes32 contributionId,
        uint8 score,
        DataRef memory evaluation
    ) private returns (bytes32 evaluationReplyHash) {
        _requireDataRef(evaluation);
        _requireEvaluator(msg.sender);
        if (score > 100) revert InvalidScore();

        ContributionView storage contribution = _contributions[contributionId];
        if (!contribution.exists) revert UnknownRecord();
        if (contribution.initialEvaluated) revert AlreadyCompleted();
        if (
            reply.workflowRunId != contribution.workflowRunId
                || reply.prevTaskHashes[0] != contribution.evaluateTaskHash
        ) revert InvalidReply();

        address verifier = _evaluationVerifier();
        evaluationReplyHash = _anchorProvidedReply(reply, verifier, false);
        _evaluationCandidates[evaluationReplyHash] = EvaluationCandidate({
            exists: true,
            kind: EvaluationKind.Initial,
            contributionId: contributionId,
            score: score,
            evaluation: evaluation
        });
    }

    function _submitAppeal(AgentReply memory reply, bytes32 contributionId, DataRef memory appeal)
        private
        returns (bytes32 appealReplyHash)
    {
        _requireDataRef(appeal);
        ContributionView storage contribution = _contributions[contributionId];
        if (!contribution.exists) revert UnknownRecord();
        if (!contribution.initialEvaluated) revert Incomplete();
        if (contribution.appealed) revert AlreadyCompleted();

        address registryAddress = ITAWGProfileRead(profile).identityRegistry();
        if (IIdentityRegistryRead(registryAddress).getAgentWallet(contribution.attributedAgentId) != msg.sender) {
            revert Unauthorized();
        }
        RoundView storage round = _rounds[contribution.workflowRunId];
        if (reply.workflowRunId != contribution.workflowRunId || reply.prevTaskHashes[0] != round.appealTaskHash) {
            revert InvalidReply();
        }

        if (round.phase != RoundPhase.Appealing || block.timestamp > round.appealDeadline) revert InvalidState();

        appealReplyHash = _anchorProvidedReply(reply, passThroughVerifier, true);
        bytes32 reevaluateTaskHash = _createTask(
            contribution.workflowRunId,
            Stage.ReevaluateContribution,
            abi.encode(contributionId, appealReplyHash),
            appealReplyHash
        );

        contribution.appealed = true;
        contribution.appeal = appeal;
        contribution.appealReplyHash = appealReplyHash;
        contribution.reevaluateTaskHash = reevaluateTaskHash;
        ++round.pendingAppealEvaluationCount;
    }

    function _submitAppealEvaluation(
        AgentReply memory reply,
        bytes32 contributionId,
        uint8 score,
        DataRef memory evaluation
    ) private returns (bytes32 evaluationReplyHash) {
        _requireDataRef(evaluation);
        _requireEvaluator(msg.sender);
        if (score > 100) revert InvalidScore();

        ContributionView storage contribution = _contributions[contributionId];
        if (!contribution.appealed) revert Incomplete();
        if (contribution.appealEvaluated) revert AlreadyCompleted();
        if (
            reply.workflowRunId != contribution.workflowRunId
                || reply.prevTaskHashes[0] != contribution.reevaluateTaskHash
        ) revert InvalidReply();

        address verifier = _evaluationVerifier();
        evaluationReplyHash = _anchorProvidedReply(reply, verifier, false);
        _evaluationCandidates[evaluationReplyHash] = EvaluationCandidate({
            exists: true,
            kind: EvaluationKind.Appeal,
            contributionId: contributionId,
            score: score,
            evaluation: evaluation
        });
    }

    function _submitCloseCollection(AgentReply memory reply, bytes32 workflowRunId)
        private
        returns (bytes32 closeReplyHash)
    {
        RoundView storage round = _rounds[workflowRunId];
        if (round.phase != RoundPhase.Open || block.timestamp < round.minRolloverAt) revert RoundNotReady();
        if (reply.workflowRunId != workflowRunId || reply.prevTaskHashes[0] != round.collectTaskHash) {
            revert InvalidReply();
        }

        closeReplyHash = _anchorProvidedReply(reply, passThroughVerifier, true);
        _closeReplyHashes[workflowRunId] = closeReplyHash;
        round.phase = RoundPhase.Evaluating;
    }

    function prepareOpenAppeal(bytes32 workflowRunId) external returns (bytes32 openAppealTaskHash) {
        RoundView storage round = _rounds[workflowRunId];
        if (round.phase != RoundPhase.Evaluating) revert InvalidState();
        if (round.initialEvaluatedCount != round.contributionCount) revert Incomplete();
        if (_openAppealTaskHashes[workflowRunId] != bytes32(0)) revert AlreadyCompleted();

        bytes32[] memory previousReplies = new bytes32[](_roundContributionIds[workflowRunId].length + 1);
        previousReplies[0] = _closeReplyHashes[workflowRunId];
        for (uint256 i; i < _roundContributionIds[workflowRunId].length; ++i) {
            previousReplies[i + 1] = _contributions[_roundContributionIds[workflowRunId][i]].initialEvaluationReplyHash;
        }

        openAppealTaskHash = _createTaskWithPreviousReplies(
            workflowRunId, Stage.OpenAppealPhase, abi.encode(workflowRunId), previousReplies
        );
        _openAppealTaskHashes[workflowRunId] = openAppealTaskHash;
    }

    function _submitOpenAppealPhase(AgentReply memory reply, bytes32 workflowRunId)
        private
        returns (bytes32 openAppealReplyHash)
    {
        RoundView storage round = _rounds[workflowRunId];
        bytes32 openAppealTaskHash = _openAppealTaskHashes[workflowRunId];
        if (round.phase != RoundPhase.Evaluating || openAppealTaskHash == bytes32(0)) revert InvalidState();
        if (reply.workflowRunId != workflowRunId || reply.prevTaskHashes[0] != openAppealTaskHash) {
            revert InvalidReply();
        }

        openAppealReplyHash = _anchorProvidedReply(reply, passThroughVerifier, true);

        round.phase = RoundPhase.Appealing;
        round.appealDeadline = uint64(block.timestamp + appealDuration);
        _openAppealReplyHashes[workflowRunId] = openAppealReplyHash;
        round.appealTaskHash =
            _createTask(workflowRunId, Stage.AppealContribution, abi.encode(workflowRunId), openAppealReplyHash);
    }

    function prepareRoundSummary(bytes32 workflowRunId) external returns (bytes32 summaryTaskHash) {
        RoundView storage round = _rounds[workflowRunId];
        if (round.phase != RoundPhase.Appealing) revert InvalidState();
        if (block.timestamp <= round.appealDeadline) revert InvalidState();
        if (round.pendingAppealEvaluationCount != 0) revert Incomplete();
        if (_summaryTaskHashes[workflowRunId] != bytes32(0)) revert AlreadyCompleted();

        bytes32[] memory finalEvaluationReplies = new bytes32[](_roundContributionIds[workflowRunId].length + 1);
        finalEvaluationReplies[0] = _openAppealReplyHashes[workflowRunId];
        for (uint256 i; i < _roundContributionIds[workflowRunId].length; ++i) {
            ContributionView storage contribution = _contributions[_roundContributionIds[workflowRunId][i]];
            finalEvaluationReplies[i + 1] =
                contribution.appealed ? contribution.appealEvaluationReplyHash : contribution.initialEvaluationReplyHash;
        }

        summaryTaskHash = _createTaskWithPreviousReplies(
            workflowRunId,
            Stage.SummarizeRound,
            abi.encode(workflowRunId, round.totalFinalScore),
            finalEvaluationReplies
        );
        _summaryTaskHashes[workflowRunId] = summaryTaskHash;
    }

    function _submitRoundSummary(AgentReply memory reply, bytes32 workflowRunId, DataRef memory summary)
        private
        returns (bytes32 summaryReplyHash)
    {
        _requireDataRef(summary);
        _requireEvaluator(msg.sender);
        bytes32 summaryTaskHash = _summaryTaskHashes[workflowRunId];
        if (summaryTaskHash == bytes32(0)) revert Incomplete();
        if (_summaryReplyHashes[workflowRunId] != bytes32(0)) revert AlreadyCompleted();
        if (reply.workflowRunId != workflowRunId || reply.prevTaskHashes[0] != summaryTaskHash) {
            revert InvalidReply();
        }

        summaryReplyHash = _anchorProvidedReply(reply, passThroughVerifier, true);
        _acceptRoundSummary(summaryReplyHash, workflowRunId);
    }

    function onAgentProve(bytes32[] calldata replyHashes, bytes calldata proof) external {
        if (replyHashes.length != 1) revert InvalidProofBatch();

        bytes32 replyHash = replyHashes[0];
        if (_replies[replyHash].replier == address(0)) revert UnknownRecord();
        if (_replyProven[replyHash]) revert AlreadyCompleted();

        if (!_verifyReply(replyHash, proof)) return;
        EvaluationCandidate storage candidate = _evaluationCandidates[replyHash];
        if (candidate.exists) {
            if (candidate.kind == EvaluationKind.Initial) {
                _acceptInitialEvaluation(replyHash, candidate);
            } else {
                _acceptAppealEvaluation(replyHash, candidate);
            }
            return;
        }
    }

    function onAgentReply(AgentReply calldata reply) external override {
        if (reply.replier != msg.sender) revert Unauthorized();
        if (reply.outputHash != keccak256(reply.output)) revert InvalidReply();
        if (reply.timestamp > block.timestamp) revert InvalidReply();
        if (reply.prevTaskHashes.length != 1) revert InvalidReply();
        AgentTask storage previousTask = _tasks[reply.prevTaskHashes[0]];
        if (
            previousTask.workflowRunId == bytes32(0) || previousTask.workflowRunId != reply.workflowRunId
                || reply.timestamp <= previousTask.timestamp || block.timestamp > previousTask.expiresAt
        ) revert InvalidReply();

        ActionKind action = abi.decode(reply.output, (ActionKind));
        AgentReply memory replyCopy = reply;
        if (action == ActionKind.SubmitContribution) {
            (
                ,
                ContributionType contributionType,
                uint256 attributedAgentId,
                bytes32 sourceKey,
                DataRef memory source,
                bytes32 reviewedContributionId
            ) = abi.decode(reply.output, (ActionKind, ContributionType, uint256, bytes32, DataRef, bytes32));
            _requireDataRef(source);
            uint256 recorderAgentId = _validateContribution(
                reply.workflowRunId, contributionType, attributedAgentId, sourceKey, reviewedContributionId, msg.sender
            );
            if (reply.prevTaskHashes[0] != _rounds[reply.workflowRunId].collectTaskHash) revert InvalidReply();
            bytes32 contributionId = _anchorProvidedReply(replyCopy, passThroughVerifier, true);
            _storeContribution(
                contributionId,
                reply.workflowRunId,
                contributionType,
                attributedAgentId,
                recorderAgentId,
                sourceKey,
                source,
                reviewedContributionId
            );
            return;
        }
        if (action == ActionKind.AppendSupportingMaterial) {
            (, bytes32 contributionId, DataRef memory material) =
                abi.decode(reply.output, (ActionKind, bytes32, DataRef));
            _appendSupportingMaterial(replyCopy, contributionId, material);
            return;
        }
        if (action == ActionKind.SubmitInitialEvaluation) {
            (, bytes32 contributionId, uint8 score, DataRef memory evaluation) =
                abi.decode(reply.output, (ActionKind, bytes32, uint8, DataRef));
            _submitInitialEvaluation(replyCopy, contributionId, score, evaluation);
            return;
        }
        if (action == ActionKind.SubmitAppeal) {
            (, bytes32 contributionId, DataRef memory appeal) = abi.decode(reply.output, (ActionKind, bytes32, DataRef));
            _submitAppeal(replyCopy, contributionId, appeal);
            return;
        }
        if (action == ActionKind.SubmitAppealEvaluation) {
            (, bytes32 contributionId, uint8 score, DataRef memory evaluation) =
                abi.decode(reply.output, (ActionKind, bytes32, uint8, DataRef));
            _submitAppealEvaluation(replyCopy, contributionId, score, evaluation);
            return;
        }
        if (action == ActionKind.SubmitRoundSummary) {
            (, bytes32 workflowRunId, DataRef memory summary) = abi.decode(reply.output, (ActionKind, bytes32, DataRef));
            _submitRoundSummary(replyCopy, workflowRunId, summary);
            return;
        }
        if (action == ActionKind.CloseCollection) {
            (, bytes32 workflowRunId) = abi.decode(reply.output, (ActionKind, bytes32));
            _submitCloseCollection(replyCopy, workflowRunId);
            return;
        }
        if (action == ActionKind.OpenAppealPhase) {
            (, bytes32 workflowRunId) = abi.decode(reply.output, (ActionKind, bytes32));
            _submitOpenAppealPhase(replyCopy, workflowRunId);
            return;
        }
        if (action == ActionKind.SettleRound) {
            (, bytes32 workflowRunId) = abi.decode(reply.output, (ActionKind, bytes32));
            _submitSettlement(replyCopy, workflowRunId);
            return;
        }
        revert InvalidReply();
    }

    function _submitSettlement(AgentReply memory reply, bytes32 workflowRunId)
        private
        returns (bytes32 settlementReplyHash)
    {
        RoundView storage round = _rounds[workflowRunId];
        if (round.phase != RoundPhase.Appealing) revert InvalidState();
        bytes32 settlementTaskHash = _settlementTaskHashes[workflowRunId];
        if (settlementTaskHash == bytes32(0)) revert Incomplete();
        if (!_replyProven[_summaryReplyHashes[workflowRunId]]) revert Incomplete();
        if (reply.workflowRunId != workflowRunId || reply.prevTaskHashes[0] != settlementTaskHash) {
            revert InvalidReply();
        }

        settlementReplyHash = _anchorProvidedReply(reply, passThroughVerifier, true);
        _settle(workflowRunId, settlementReplyHash);
    }

    function _settle(bytes32 workflowRunId, bytes32 settlementReplyHash) private {
        RoundView storage round = _rounds[workflowRunId];

        round.phase = RoundPhase.Settled;
        round.settledAt = uint64(block.timestamp);

        _spent[workflowRunId] = round.totalFinalScore;
        IBoundedAgentAction.Envelope storage envelope = _envelopes[workflowRunId];
        bytes32 previousCursor = envelope.cursorRoot;
        bytes32 newCursor = keccak256(abi.encode(round.totalFinalScore));
        envelope.cursorRoot = newCursor;
        envelope.status = IBoundedAgentAction.Status.Completed;
        emit EnvelopeAdvanced(workflowRunId, previousCursor, newCursor);
        emit EnvelopeStatusChanged(
            workflowRunId, IBoundedAgentAction.Status.Active, IBoundedAgentAction.Status.Completed
        );

        uint256[] storage rewardedAgents = _rewardedAgentIds[workflowRunId];
        IIdentityRegistryRead registry = IIdentityRegistryRead(ITAWGProfileRead(profile).identityRegistry());
        for (uint256 i; i < rewardedAgents.length; ++i) {
            uint256 agentId = rewardedAgents[i];
            uint256 amount = _agentRoundScores[workflowRunId][agentId];
            if (amount == 0) continue;
            address recipient = registry.getAgentWallet(agentId);
            if (recipient == address(0)) revert ZeroPointsRecipient();
            IERC20(pointsToken).safeTransfer(recipient, amount);
        }

        bytes32 finalTaskHash = _createTerminalTask(workflowRunId, settlementReplyHash);
        _results[workflowRunId] =
            RunResult({status: RunStatus.Success, finalTaskHash: finalTaskHash, completedAt: block.timestamp});
        emit WorkflowCompleted(workflowRunId, RunStatus.Success, finalTaskHash, block.timestamp);
    }

    function agentRoundScore(bytes32 workflowRunId, uint256 agentId) external view returns (uint256) {
        return _agentRoundScores[workflowRunId][agentId];
    }

    function result(bytes32 workflowRunId)
        external
        view
        override
        returns (RunStatus status, bytes32 finalTaskHash, uint256 completedAt)
    {
        RunResult storage runResult = _results[workflowRunId];
        return (runResult.status, runResult.finalTaskHash, runResult.completedAt);
    }

    function getAgentTask(bytes32 taskHash) external view override returns (AgentTask memory task, bool proven) {
        if (_tasks[taskHash].workflowRunId == bytes32(0)) revert UnknownRecord();
        return (_tasks[taskHash], _taskProven[taskHash]);
    }

    function getAgentReply(bytes32 replyHash)
        external
        view
        override
        returns (AgentReply memory reply, address verifier, bool proven, bytes32 verificationDigest)
    {
        if (_replies[replyHash].replier == address(0)) revert UnknownRecord();
        return (
            _replies[replyHash], _replyVerifier[replyHash], _replyProven[replyHash], _replyVerificationDigest[replyHash]
        );
    }

    function getEnvelope(bytes32 id) external view override returns (IBoundedAgentAction.Envelope memory envelope) {
        _requireEnvelope(id);
        return _envelopes[id];
    }

    function getCursor(bytes32 id) external view override returns (bytes32) {
        _requireEnvelope(id);
        return _envelopes[id].cursorRoot;
    }

    function bound(bytes32 id) external view override returns (uint256 cap, address asset) {
        _requireEnvelope(id);
        return (roundPointCap, pointsToken);
    }

    function spent(bytes32 id) external view override returns (uint256) {
        _requireEnvelope(id);
        return _spent[id];
    }

    function remaining(bytes32 id) external view override returns (uint256) {
        _requireEnvelope(id);
        IBoundedAgentAction.Envelope storage envelope = _envelopes[id];
        if (envelope.status != IBoundedAgentAction.Status.Active) return 0;
        return roundPointCap - _spent[id];
    }

    function registerEnvelope(address, bytes32, uint64, bytes calldata) external pure override returns (bytes32) {
        revert RestrictedBudgetMutation();
    }

    function advanceCursor(bytes32, bytes calldata) external pure override returns (bytes32) {
        revert RestrictedBudgetMutation();
    }

    function setStatus(bytes32, IBoundedAgentAction.Status) external pure override {
        revert RestrictedBudgetMutation();
    }

    function getStatus(bytes32 id) external view override returns (IBoundedAgentAction.Status) {
        return _envelopes[id].status;
    }

    function isActive(bytes32 id) external view override returns (bool) {
        return _envelopes[id].status == IBoundedAgentAction.Status.Active;
    }

    function supportsInterface(bytes4 interfaceId) external pure override returns (bool) {
        return interfaceId == type(IAgentWorkflow).interfaceId || interfaceId == type(IBoundedAgentAction).interfaceId
            || interfaceId == type(IBudgetSubstrate).interfaceId || interfaceId == 0x01ffc9a7;
    }

    function _hashTask(AgentTask memory task) private pure returns (bytes32) {
        return keccak256(
            abi.encode(
                task.stage,
                task.taskSeq,
                task.inputHash,
                task.timestamp,
                task.expiresAt,
                keccak256(abi.encodePacked(task.prevReplyHashes)),
                task.workflowRunId
            )
        );
    }

    function _requireDataRef(DataRef memory data) private pure {
        if (bytes(data.locator).length == 0 || data.digest == bytes32(0)) {
            revert InvalidDataReference();
        }
    }

    function _requireEnvelope(bytes32 id) private view {
        if (_envelopes[id].status == IBoundedAgentAction.Status.None) {
            revert UnknownRecord();
        }
    }

    function _hashReply(AgentReply memory reply) private pure returns (bytes32) {
        return keccak256(
            abi.encode(
                reply.outputHash,
                reply.timestamp,
                reply.replier,
                keccak256(abi.encodePacked(reply.prevTaskHashes)),
                reply.workflowRunId
            )
        );
    }

    function _anchorProvidedReply(AgentReply memory reply, address verifier, bool autoProve)
        private
        returns (bytes32 replyHash)
    {
        replyHash = _hashReply(reply);
        if (_replies[replyHash].replier != address(0)) revert AlreadyCompleted();
        _replies[replyHash] = reply;
        _replyVerifier[replyHash] = verifier;
        emit AgentReplyAnchored(reply.workflowRunId, replyHash, reply.replier);
        if (autoProve && !_verifyReply(replyHash, "")) revert InvalidReply();
    }

    function _verifyReply(bytes32 replyHash, bytes memory proof) private returns (bool valid) {
        AgentReply storage reply = _replies[replyHash];
        bytes32 taskHash = reply.prevTaskHashes[0];
        AgentTask storage task = _tasks[taskHash];
        bytes32 agentId = _replyVerifier[replyHash] == passThroughVerifier
            ? bytes32(uint256(uint160(reply.replier)))
            : bytes32(evaluatorAgentId);
        bytes32 verificationDigest;
        if (_verifierCallActive != 0) revert InvalidState();
        _verifierCallActive = 1;
        (valid, verificationDigest) =
            IAgentVerifier(_replyVerifier[replyHash]).verify(taskHash, agentId, task.inputHash, reply.outputHash, proof);
        _verifierCallActive = 0;
        _replyVerificationDigest[replyHash] = verificationDigest;
        if (valid) _replyProven[replyHash] = true;
    }

    function _createTask(bytes32 workflowRunId, Stage stage, bytes memory input, bytes32 previousReplyHash)
        private
        returns (bytes32 taskHash)
    {
        bytes32[] memory previousReplies = new bytes32[](1);
        previousReplies[0] = previousReplyHash;
        return _storeTask(workflowRunId, uint8(stage), keccak256(input), input, previousReplies, true);
    }

    function _createTaskWithPreviousReplies(
        bytes32 workflowRunId,
        Stage stage,
        bytes memory input,
        bytes32[] memory previousReplies
    ) private returns (bytes32 taskHash) {
        return _storeTask(workflowRunId, uint8(stage), keccak256(input), input, previousReplies, true);
    }

    function _createTerminalTask(bytes32 workflowRunId, bytes32 previousReplyHash) private returns (bytes32 taskHash) {
        bytes32[] memory previousReplies = new bytes32[](1);
        previousReplies[0] = previousReplyHash;
        return _storeTask(workflowRunId, type(uint8).max, EMPTY_INPUT_HASH, "", previousReplies, false);
    }

    function _storeTask(
        bytes32 workflowRunId,
        uint8 stage,
        bytes32 inputHash,
        bytes memory input,
        bytes32[] memory previousReplies,
        bool announce
    ) private returns (bytes32 taskHash) {
        AgentTask memory task = AgentTask({
            stage: stage,
            taskSeq: _nextTaskSequence[workflowRunId]++,
            inputHash: inputHash,
            input: input,
            timestamp: block.timestamp,
            expiresAt: type(uint256).max,
            prevReplyHashes: previousReplies,
            workflowRunId: workflowRunId
        });
        taskHash = _hashTask(task);
        _tasks[taskHash] = task;
        bool proven = true;
        for (uint256 i; i < previousReplies.length; ++i) {
            if (!_replyProven[previousReplies[i]]) {
                proven = false;
                break;
            }
        }
        _taskProven[taskHash] = proven;
        if (announce) emit NewAgentTask(workflowRunId, stage, taskHash);
    }

    function _resolveContributionRecorder(uint256 attributedAgentId, address caller)
        private
        view
        returns (uint256 recorderAgentId)
    {
        (bool isMember,,) = ITAWGProfileRead(profile).getAgent(attributedAgentId);
        if (!isMember) revert Unauthorized();

        IIdentityRegistryRead registry = IIdentityRegistryRead(ITAWGProfileRead(profile).identityRegistry());
        if (registry.getAgentWallet(attributedAgentId) == caller) {
            return attributedAgentId;
        }
        if (registry.getAgentWallet(evaluatorAgentId) == caller) {
            return evaluatorAgentId;
        }
        revert Unauthorized();
    }

    function _validateContribution(
        bytes32 workflowRunId,
        ContributionType contributionType,
        uint256 attributedAgentId,
        bytes32 sourceKey,
        bytes32 reviewedContributionId,
        address caller
    ) private view returns (uint256 recorderAgentId) {
        recorderAgentId = _resolveContributionRecorder(attributedAgentId, caller);

        RoundView storage round = _rounds[workflowRunId];
        if (round.phase != RoundPhase.Open) revert InvalidState();
        if (round.contributionCount >= maxContributionsPerRound) {
            revert ContributionLimitReached();
        }
        bytes32 existingContributionId = contributionBySourceKey[sourceKey];
        if (existingContributionId != bytes32(0)) {
            revert DuplicateContributionSource();
        }
        if (contributionType == ContributionType.Work) {
            if (reviewedContributionId != bytes32(0)) {
                revert InvalidReviewTarget();
            }
        } else {
            ContributionView storage reviewedContribution = _contributions[reviewedContributionId];
            if (!reviewedContribution.exists || reviewedContribution.contributionType != ContributionType.Work) {
                revert InvalidReviewTarget();
            }
        }
    }

    function _storeContribution(
        bytes32 contributionId,
        bytes32 workflowRunId,
        ContributionType contributionType,
        uint256 attributedAgentId,
        uint256 recorderAgentId,
        bytes32 sourceKey,
        DataRef memory source,
        bytes32 reviewedContributionId
    ) private {
        bytes32 evaluateTaskHash =
            _createTask(workflowRunId, Stage.EvaluateContribution, abi.encode(contributionId), contributionId);

        _contributions[contributionId] = ContributionView({
            exists: true,
            workflowRunId: workflowRunId,
            contributionType: contributionType,
            attributedAgentId: attributedAgentId,
            recorderAgentId: recorderAgentId,
            sourceKey: sourceKey,
            source: source,
            reviewedContributionId: reviewedContributionId,
            evaluateTaskHash: evaluateTaskHash,
            supportingMaterialCount: 0,
            supportingMaterialRoot: bytes32(0),
            initialEvaluated: false,
            initialScore: 0,
            finalScore: 0,
            initialEvaluation: DataRef({locator: "", digest: bytes32(0), expiresAt: 0}),
            initialEvaluationReplyHash: bytes32(0),
            appealed: false,
            appeal: DataRef({locator: "", digest: bytes32(0), expiresAt: 0}),
            appealReplyHash: bytes32(0),
            reevaluateTaskHash: bytes32(0),
            appealEvaluated: false,
            appealScore: 0,
            appealEvaluation: DataRef({locator: "", digest: bytes32(0), expiresAt: 0}),
            appealEvaluationReplyHash: bytes32(0)
        });
        contributionBySourceKey[sourceKey] = contributionId;
        _roundContributionIds[workflowRunId].push(contributionId);
        ++_rounds[workflowRunId].contributionCount;
    }

    function _requireEvaluator(address caller) private view {
        IIdentityRegistryRead registry = IIdentityRegistryRead(ITAWGProfileRead(profile).identityRegistry());
        if (registry.getAgentWallet(evaluatorAgentId) != caller) revert Unauthorized();
    }

    function _evaluationVerifier() private view returns (address verifier) {
        (,, verifier) = ITAWGProfileRead(profile).getAgent(evaluatorAgentId);
        if (verifier == address(0) || verifier == passThroughVerifier || verifier.code.length == 0) {
            revert InvalidConfiguration();
        }
    }

    function _acceptInitialEvaluation(bytes32 replyHash, EvaluationCandidate storage candidate) private {
        ContributionView storage contribution = _contributions[candidate.contributionId];
        if (contribution.initialEvaluated) revert AlreadyCompleted();
        if (_replies[replyHash].prevTaskHashes[0] != contribution.evaluateTaskHash) {
            revert InvalidReply();
        }

        RoundView storage round = _rounds[contribution.workflowRunId];
        if (round.totalFinalScore + candidate.score > roundPointCap) {
            revert RoundBudgetExceeded();
        }

        contribution.initialEvaluated = true;
        contribution.initialScore = candidate.score;
        contribution.finalScore = candidate.score;
        contribution.initialEvaluation = candidate.evaluation;
        contribution.initialEvaluationReplyHash = replyHash;
        ++round.initialEvaluatedCount;
        round.totalFinalScore += candidate.score;
        _increaseAgentRoundScore(contribution.workflowRunId, contribution.attributedAgentId, candidate.score);
    }

    function _acceptAppealEvaluation(bytes32 replyHash, EvaluationCandidate storage candidate) private {
        ContributionView storage contribution = _contributions[candidate.contributionId];
        if (!contribution.appealed) revert Incomplete();
        if (contribution.appealEvaluated) revert AlreadyCompleted();

        RoundView storage round = _rounds[contribution.workflowRunId];
        uint256 adjustedTotal = round.totalFinalScore - contribution.finalScore + candidate.score;
        if (adjustedTotal > roundPointCap) revert RoundBudgetExceeded();

        contribution.appealEvaluated = true;
        contribution.appealScore = candidate.score;
        contribution.finalScore = candidate.score;
        contribution.appealEvaluation = candidate.evaluation;
        contribution.appealEvaluationReplyHash = replyHash;
        round.totalFinalScore = adjustedTotal;
        --round.pendingAppealEvaluationCount;

        uint256 agentScore = _agentRoundScores[contribution.workflowRunId][contribution.attributedAgentId];
        _agentRoundScores[contribution.workflowRunId][contribution.attributedAgentId] =
            agentScore - contribution.initialScore + candidate.score;
        if (candidate.score > 0 && !_rewardedAgentSeen[contribution.workflowRunId][contribution.attributedAgentId]) {
            _rewardedAgentSeen[contribution.workflowRunId][contribution.attributedAgentId] = true;
            _rewardedAgentIds[contribution.workflowRunId].push(contribution.attributedAgentId);
        }
    }

    function _acceptRoundSummary(bytes32 replyHash, bytes32 workflowRunId) private {
        RoundView storage round = _rounds[workflowRunId];
        if (_summaryReplyHashes[workflowRunId] != bytes32(0)) revert AlreadyCompleted();

        _summaryReplyHashes[workflowRunId] = replyHash;
        _settlementTaskHashes[workflowRunId] = _createTask(
            workflowRunId, Stage.SettleRound, abi.encode(workflowRunId, replyHash, round.totalFinalScore), replyHash
        );
    }

    function _increaseAgentRoundScore(bytes32 workflowRunId, uint256 agentId, uint256 amount) private {
        if (amount > 0 && !_rewardedAgentSeen[workflowRunId][agentId]) {
            _rewardedAgentSeen[workflowRunId][agentId] = true;
            _rewardedAgentIds[workflowRunId].push(agentId);
        }
        _agentRoundScores[workflowRunId][agentId] += amount;
    }
}
