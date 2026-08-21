// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {Workflow} from "../contracts/Workflow.sol";
import {PassThroughVerifier} from "../contracts/PassThroughVerifier.sol";
import {AgentTask, AgentReply, RunStatus, IAgentWorkflow} from "@agent-ercs/execution/ERC8301/IAgentWorkflow.sol";
import {IBoundedAgentAction} from "@agent-ercs/metering/ERC8312/IBoundedAgentAction.sol";
import {IBudgetSubstrate} from "@agent-ercs/metering/ERC8312/IBudgetSubstrate.sol";
import {IAgentVerifier} from "@agent-ercs/verify/ERC8274/IAgentVerifier.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract MockIdentityRegistry {
    mapping(uint256 agentId => address wallet) internal _wallets;

    function setAgentWallet(uint256 agentId, address wallet) external {
        _wallets[agentId] = wallet;
    }

    function getAgentWallet(uint256 agentId) external view returns (address) {
        return _wallets[agentId];
    }
}

contract MockProfile {
    struct Member {
        bool isMember;
        string data;
        address verifier;
    }

    address public immutable identityRegistry;
    mapping(uint256 agentId => Member member) internal _members;

    constructor(address identityRegistry_) {
        identityRegistry = identityRegistry_;
    }

    function setMember(uint256 agentId, address verifier) external {
        _members[agentId] = Member({isMember: true, data: "{}", verifier: verifier});
    }

    function getAgent(uint256 agentId) external view returns (bool isMember, string memory data, address verifier) {
        Member storage member = _members[agentId];
        return (member.isMember, member.data, member.verifier);
    }
}

contract MockPointsToken is ERC20 {
    constructor() ERC20("TAWG Points", "TAWG") {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

contract TestAgentVerifier is IAgentVerifier {
    bool public valid = true;

    function setValid(bool valid_) external {
        valid = valid_;
    }

    function verify(bytes32 taskId, bytes32 agentId, bytes32 inputHash, bytes32 outputHash, bytes calldata)
        external
        returns (bool, bytes32 verificationDigest)
    {
        verificationDigest = keccak256(abi.encode(taskId, agentId, inputHash, outputHash, valid, address(this)));
        emit VerificationCompleted(taskId, agentId, inputHash, outputHash, valid, verificationDigest);
        return (valid, verificationDigest);
    }
}

contract ReentrantPassThroughVerifier is IAgentVerifier {
    Workflow internal _target;
    bytes32 internal _workflowRunId;
    bytes32 internal _settlementTaskHash;
    bool public armed;
    bool public reentered;

    function arm(Workflow target, bytes32 workflowRunId, bytes32 settlementTaskHash) external {
        _target = target;
        _workflowRunId = workflowRunId;
        _settlementTaskHash = settlementTaskHash;
        armed = true;
    }

    function verify(bytes32 taskId, bytes32 agentId, bytes32 inputHash, bytes32 outputHash, bytes calldata)
        external
        returns (bool valid, bytes32 verificationDigest)
    {
        if (armed) {
            armed = false;
            bytes memory output = abi.encode(Workflow.ActionKind.SettleRound, _workflowRunId);
            bytes32[] memory previousTasks = new bytes32[](1);
            previousTasks[0] = _settlementTaskHash;
            AgentReply memory reply = AgentReply({
                outputHash: keccak256(output),
                output: output,
                timestamp: block.timestamp,
                replier: address(this),
                prevTaskHashes: previousTasks,
                workflowRunId: _workflowRunId
            });
            try _target.onAgentReply(reply) {
                reentered = true;
            } catch {}
        }

        valid = true;
        verificationDigest = keccak256(abi.encode(taskId, agentId, inputHash, outputHash, valid, address(this)));
        emit VerificationCompleted(taskId, agentId, inputHash, outputHash, valid, verificationDigest);
    }
}

contract WorkflowTest is Test {
    uint256 internal constant EVALUATOR_AGENT_ID = 8004000000000000000000000000000000000000000000000000000000000042;
    uint256 internal constant CONTRIBUTOR_AGENT_ID = 8004000000000000000000000000000000000000000000000000000000000101;
    uint256 internal constant OTHER_AGENT_ID = 8004000000000000000000000000000000000000000000000000000000000202;
    uint64 internal constant ROUND_DURATION = 1 days;
    uint64 internal constant APPEAL_DURATION = 1 days;
    uint256 internal constant ROUND_POINT_CAP = 10_000;
    uint32 internal constant MAX_CONTRIBUTIONS = 128;
    address internal constant CONTRIBUTOR_WALLET = address(0xC011AB);
    address internal constant ROTATED_CONTRIBUTOR_WALLET = address(0xC011AB02);
    address internal constant EVALUATOR_WALLET = address(0xB07);
    address internal constant OTHER_WALLET = address(0xA11CE);

    MockIdentityRegistry internal identityRegistry;
    MockProfile internal profile;
    MockPointsToken internal pointsToken;
    TestAgentVerifier internal evaluatorVerifier;
    PassThroughVerifier internal passThroughVerifier;
    Workflow internal workflow;

    function setUp() public {
        vm.warp(1_000_000);

        identityRegistry = new MockIdentityRegistry();
        profile = new MockProfile(address(identityRegistry));
        pointsToken = new MockPointsToken();
        evaluatorVerifier = new TestAgentVerifier();
        passThroughVerifier = new PassThroughVerifier();
        profile.setMember(EVALUATOR_AGENT_ID, address(evaluatorVerifier));
        profile.setMember(CONTRIBUTOR_AGENT_ID, address(0x8274));
        profile.setMember(OTHER_AGENT_ID, address(0x8274));
        identityRegistry.setAgentWallet(EVALUATOR_AGENT_ID, EVALUATOR_WALLET);
        identityRegistry.setAgentWallet(CONTRIBUTOR_AGENT_ID, CONTRIBUTOR_WALLET);
        identityRegistry.setAgentWallet(OTHER_AGENT_ID, OTHER_WALLET);

        workflow = new Workflow(
            address(profile),
            address(pointsToken),
            address(passThroughVerifier),
            EVALUATOR_AGENT_ID,
            ROUND_DURATION,
            APPEAL_DURATION,
            ROUND_POINT_CAP,
            MAX_CONTRIBUTIONS
        );
    }

    function testDeploymentRejectsUnregisteredEvaluator() public {
        MockProfile emptyProfile = new MockProfile(address(identityRegistry));

        vm.expectRevert();
        new Workflow(
            address(emptyProfile),
            address(pointsToken),
            address(passThroughVerifier),
            EVALUATOR_AGENT_ID,
            ROUND_DURATION,
            APPEAL_DURATION,
            ROUND_POINT_CAP,
            MAX_CONTRIBUTIONS
        );
    }

    function testDeploymentRejectsZeroRoundConfiguration() public {
        vm.expectRevert();
        new Workflow(
            address(profile),
            address(pointsToken),
            address(passThroughVerifier),
            EVALUATOR_AGENT_ID,
            0,
            APPEAL_DURATION,
            ROUND_POINT_CAP,
            MAX_CONTRIBUTIONS
        );
    }

    function testDeploymentRejectsPassThroughVerifierWithoutCode() public {
        vm.expectRevert(Workflow.InvalidConfiguration.selector);
        new Workflow(
            address(profile),
            address(pointsToken),
            address(0x1234),
            EVALUATOR_AGENT_ID,
            ROUND_DURATION,
            APPEAL_DURATION,
            ROUND_POINT_CAP,
            MAX_CONTRIBUTIONS
        );
    }

    function testRunCreatesOneOpenRoundAndItsBudgetEnvelope() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);

        assertEq(workflow.currentRoundId(), runId);

        Workflow.RoundView memory round = workflow.getRound(runId);
        assertEq(uint8(round.phase), uint8(Workflow.RoundPhase.Open));
        assertEq(round.openedAt, block.timestamp);
        assertEq(round.minRolloverAt, block.timestamp + ROUND_DURATION);
        assertEq(round.appealDeadline, 0);
        assertEq(round.settledAt, 0);
        assertEq(round.contributionCount, 0);
        assertTrue(round.collectTaskHash != bytes32(0));

        (RunStatus status, bytes32 finalTaskHash, uint256 completedAt) = workflow.result(runId);
        assertEq(uint8(status), uint8(RunStatus.Pending));
        assertEq(finalTaskHash, bytes32(0));
        assertEq(completedAt, 0);

        (AgentTask memory collectTask, bool proven) = workflow.getAgentTask(round.collectTaskHash);
        assertTrue(proven);
        assertEq(collectTask.workflowRunId, runId);
        assertEq(collectTask.stage, uint8(Workflow.Stage.Collect));
        assertEq(collectTask.taskSeq, 0);
        assertEq(collectTask.inputHash, keccak256(""));
        assertEq(collectTask.input, bytes(""));
        assertEq(collectTask.expiresAt, type(uint256).max);
        assertEq(collectTask.prevReplyHashes.length, 0);

        IBoundedAgentAction.Envelope memory envelope = workflow.getEnvelope(runId);
        assertEq(envelope.id, runId);
        assertEq(envelope.principal, address(workflow));
        assertEq(uint8(envelope.status), uint8(IBoundedAgentAction.Status.Active));

        (uint256 cap, address asset) = workflow.bound(runId);
        assertEq(cap, ROUND_POINT_CAP);
        assertEq(asset, address(pointsToken));
        assertEq(workflow.spent(runId), 0);
        assertEq(workflow.remaining(runId), ROUND_POINT_CAP);
        assertEq(workflow.getCursor(runId), keccak256(abi.encode(uint256(0))));
    }

    function testRunCannotRolloverBeforeMinimumDuration() public {
        workflow.run(keccak256(""), "", type(uint256).max);

        vm.expectRevert();
        workflow.run(keccak256(""), "", type(uint256).max);
    }

    function testCloseCollectionReplyMovesOldRoundToEvaluatingBeforeSuccessorRun() public {
        bytes32 oldRunId = workflow.run(keccak256(""), "", type(uint256).max);
        vm.warp(block.timestamp + ROUND_DURATION);

        bytes32 closeReplyHash = _submitActionAs(
            workflow,
            OTHER_WALLET,
            oldRunId,
            workflow.getRound(oldRunId).collectTaskHash,
            abi.encode(Workflow.ActionKind.CloseCollection, oldRunId)
        );

        bytes32 newRunId = workflow.run(keccak256(""), "", type(uint256).max);

        assertEq(uint8(workflow.getRound(oldRunId).phase), uint8(Workflow.RoundPhase.Evaluating));
        (, address verifier, bool proven,) = workflow.getAgentReply(closeReplyHash);
        assertEq(verifier, address(passThroughVerifier));
        assertTrue(proven);
        assertEq(uint8(workflow.getRound(newRunId).phase), uint8(Workflow.RoundPhase.Open));
        assertEq(workflow.currentRoundId(), newRunId);
        assertTrue(oldRunId != newRunId);
    }

    function testElapsedDurationDoesNotCloseRoundWithoutCloseReply() public {
        workflow.run(keccak256(""), "", type(uint256).max);
        vm.warp(block.timestamp + ROUND_DURATION);

        vm.expectRevert();
        workflow.run(keccak256(""), "", type(uint256).max);
    }

    function testRunRejectsCallerControlledRoundInput() public {
        vm.expectRevert();
        workflow.run(keccak256("malicious direction"), "malicious direction", type(uint256).max);
    }

    function testRunRejectsNonCanonicalInputHash() public {
        vm.expectRevert();
        workflow.run(bytes32(0), "", type(uint256).max);
    }

    function testRunRejectsCallerControlledExpiry() public {
        vm.expectRevert();
        workflow.run(keccak256(""), "", block.timestamp + 1 days);
    }

    function testReplyMustBeTimestampedAfterItsTaskWasAnchored() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        bytes32 collectTaskHash = workflow.getRound(runId).collectTaskHash;
        bytes32[] memory previousTasks = new bytes32[](1);
        previousTasks[0] = collectTaskHash;
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/timestamp.json@commit",
            digest: keccak256("timestamp evidence"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes memory output = abi.encode(
            Workflow.ActionKind.SubmitContribution,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("timestamp-source"),
            source,
            bytes32(0)
        );
        AgentReply memory reply = AgentReply({
            outputHash: keccak256(output),
            output: output,
            timestamp: block.timestamp,
            replier: CONTRIBUTOR_WALLET,
            prevTaskHashes: previousTasks,
            workflowRunId: runId
        });

        vm.expectRevert(Workflow.InvalidReply.selector);
        vm.prank(CONTRIBUTOR_WALLET);
        workflow.onAgentReply(reply);
    }

    function testContributorCanRecordOwnWorkContribution() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        bytes32 sourceKey = keccak256("telegram:group-42:message-7:work");
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:trustless-ai/tawg-daily-contribution:data/contributions/7.json@commit",
            digest: keccak256("immutable contribution snapshot"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        bytes32 contributionId = _submitContributionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            sourceKey,
            source,
            bytes32(0)
        );

        Workflow.ContributionView memory contribution = workflow.getContribution(contributionId);
        assertTrue(contribution.exists);
        assertEq(contribution.workflowRunId, runId);
        assertEq(uint8(contribution.contributionType), uint8(Workflow.ContributionType.Work));
        assertEq(contribution.attributedAgentId, CONTRIBUTOR_AGENT_ID);
        assertEq(contribution.recorderAgentId, CONTRIBUTOR_AGENT_ID);
        assertEq(contribution.sourceKey, sourceKey);
        assertEq(contribution.source.digest, source.digest);
        assertEq(contribution.reviewedContributionId, bytes32(0));
        assertTrue(contribution.evaluateTaskHash != bytes32(0));
        assertEq(workflow.contributionBySourceKey(sourceKey), contributionId);

        (,, bool replyProven,) = workflow.getAgentReply(contributionId);
        assertTrue(replyProven);

        (AgentTask memory evaluateTask, bool taskProven) = workflow.getAgentTask(contribution.evaluateTaskHash);
        assertTrue(taskProven);
        assertEq(evaluateTask.workflowRunId, runId);
        assertEq(evaluateTask.stage, uint8(Workflow.Stage.EvaluateContribution));
        assertEq(evaluateTask.prevReplyHashes.length, 1);
        assertEq(evaluateTask.prevReplyHashes[0], contributionId);
        assertEq(workflow.getRound(runId).contributionCount, 1);
    }

    function testEvaluatorCanRecordMissedContributionForItsAuthor() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/missed.json@commit",
            digest: keccak256("missed contribution"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        bytes32 contributionId = _submitContributionAs(
            workflow,
            EVALUATOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("discord:channel-9:message-44:work"),
            source,
            bytes32(0)
        );

        Workflow.ContributionView memory contribution = workflow.getContribution(contributionId);
        assertEq(contribution.attributedAgentId, CONTRIBUTOR_AGENT_ID);
        assertEq(contribution.recorderAgentId, EVALUATOR_AGENT_ID);
    }

    function testUnrelatedMemberCannotRecordAnotherAgentsContribution() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        bytes32 collectTaskHash = workflow.getRound(runId).collectTaskHash;
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/unauthorized.json@commit",
            digest: keccak256("unauthorized attribution"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        vm.expectRevert();
        _submitContributionAgainstTaskAs(
            workflow,
            OTHER_WALLET,
            runId,
            collectTaskHash,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("unauthorized-source"),
            source,
            bytes32(0)
        );
    }

    function testContributionSourceCannotBeRecordedTwice() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        bytes32 sourceKey = keccak256("telegram:group-1:message-1:work");
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/1.json@commit",
            digest: keccak256("one snapshot"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        _submitContributionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            sourceKey,
            source,
            bytes32(0)
        );

        bytes32 collectTaskHash = workflow.getRound(runId).collectTaskHash;
        vm.expectRevert();
        _submitContributionAgainstTaskAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            collectTaskHash,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            sourceKey,
            source,
            bytes32(0)
        );
    }

    function testContributionRequiresAnAnchoredDataReference() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source =
            Workflow.DataRef({locator: "", digest: keccak256("snapshot without locator"), expiresAt: 0});
        bytes32 collectTaskHash = workflow.getRound(runId).collectTaskHash;

        vm.expectRevert();
        _submitContributionAgainstTaskAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            collectTaskHash,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("invalid-data-ref"),
            source,
            bytes32(0)
        );
    }

    function testReviewMustTargetAnExistingWorkContribution() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/reviews/1.json@commit",
            digest: keccak256("review snapshot"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        bytes32 collectTaskHash = workflow.getRound(runId).collectTaskHash;
        vm.expectRevert();
        _submitContributionAgainstTaskAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            collectTaskHash,
            Workflow.ContributionType.Review,
            CONTRIBUTOR_AGENT_ID,
            keccak256("discord:channel-2:message-8:review"),
            source,
            keccak256("unknown work")
        );
    }

    function testRoundContributionLimitRejectsAnotherRecord() public {
        Workflow limitedWorkflow = new Workflow(
            address(profile),
            address(pointsToken),
            address(passThroughVerifier),
            EVALUATOR_AGENT_ID,
            ROUND_DURATION,
            APPEAL_DURATION,
            ROUND_POINT_CAP,
            1
        );
        bytes32 runId = limitedWorkflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/cap.json@commit",
            digest: keccak256("cap snapshot"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        _submitContributionAs(
            limitedWorkflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("source-one"),
            source,
            bytes32(0)
        );

        bytes32 collectTaskHash = limitedWorkflow.getRound(runId).collectTaskHash;
        vm.expectRevert();
        _submitContributionAgainstTaskAs(
            limitedWorkflow,
            CONTRIBUTOR_WALLET,
            runId,
            collectTaskHash,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("source-two"),
            source,
            bytes32(0)
        );
    }

    function testEvaluatorScoreBecomesEffectiveOnlyAfterERC8274Proof() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/scored.json@commit",
            digest: keccak256("scored contribution"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 contributionId = _submitContributionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("telegram:group-1:message-50:work"),
            source,
            bytes32(0)
        );

        Workflow.DataRef memory evaluation = Workflow.DataRef({
            locator: "github:data/evaluations/50.json@commit",
            digest: keccak256("evaluation reasoning"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 evaluationReplyHash = _submitActionAs(
            workflow,
            EVALUATOR_WALLET,
            runId,
            workflow.getContribution(contributionId).evaluateTaskHash,
            abi.encode(Workflow.ActionKind.SubmitInitialEvaluation, contributionId, uint8(80), evaluation)
        );

        (, address verifierBefore, bool provenBefore,) = workflow.getAgentReply(evaluationReplyHash);
        assertEq(verifierBefore, address(evaluatorVerifier));
        assertFalse(provenBefore);
        assertFalse(workflow.getContribution(contributionId).initialEvaluated);

        TestAgentVerifier replacementVerifier = new TestAgentVerifier();
        replacementVerifier.setValid(false);
        profile.setMember(EVALUATOR_AGENT_ID, address(replacementVerifier));

        bytes32[] memory replyHashes = new bytes32[](1);
        replyHashes[0] = evaluationReplyHash;
        workflow.onAgentProve(replyHashes, hex"a77e57");

        (, address verifierAfter, bool provenAfter, bytes32 verificationDigest) =
            workflow.getAgentReply(evaluationReplyHash);
        assertEq(verifierAfter, address(evaluatorVerifier));
        assertTrue(provenAfter);
        assertTrue(verificationDigest != bytes32(0));

        Workflow.ContributionView memory contribution = workflow.getContribution(contributionId);
        assertTrue(contribution.initialEvaluated);
        assertEq(contribution.initialScore, 80);
        assertEq(contribution.finalScore, 80);
        assertEq(contribution.initialEvaluationReplyHash, evaluationReplyHash);
        assertEq(workflow.getRound(runId).initialEvaluatedCount, 1);
        assertEq(workflow.getRound(runId).totalFinalScore, 80);
    }

    function testOnlyEvaluatorCanSubmitAnEvaluation() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/authz.json@commit",
            digest: keccak256("evaluation authorization"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 contributionId = _submitContributionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("evaluation-authz-source"),
            source,
            bytes32(0)
        );
        bytes32 evaluateTaskHash = workflow.getContribution(contributionId).evaluateTaskHash;
        Workflow.DataRef memory evaluation = Workflow.DataRef({
            locator: "github:data/evaluations/unauthorized.json@commit",
            digest: keccak256("unauthorized evaluation"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        vm.expectRevert();
        _submitActionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            evaluateTaskHash,
            abi.encode(Workflow.ActionKind.SubmitInitialEvaluation, contributionId, uint8(80), evaluation)
        );
    }

    function testEvaluationRejectsPassThroughOrMissingVerifier() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/verifier-policy.json@commit",
            digest: keccak256("verifier policy contribution"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 contributionId = _submitContributionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("verifier-policy-source"),
            source,
            bytes32(0)
        );
        Workflow.DataRef memory evaluation = Workflow.DataRef({
            locator: "github:data/evaluations/verifier-policy.json@commit",
            digest: keccak256("verifier policy evaluation"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes memory output =
            abi.encode(Workflow.ActionKind.SubmitInitialEvaluation, contributionId, uint8(80), evaluation);
        bytes32 evaluateTaskHash = workflow.getContribution(contributionId).evaluateTaskHash;

        profile.setMember(EVALUATOR_AGENT_ID, address(passThroughVerifier));
        vm.expectRevert(Workflow.InvalidConfiguration.selector);
        _submitActionAs(workflow, EVALUATOR_WALLET, runId, evaluateTaskHash, output);

        profile.setMember(EVALUATOR_AGENT_ID, address(0));
        vm.expectRevert(Workflow.InvalidConfiguration.selector);
        _submitActionAs(workflow, EVALUATOR_WALLET, runId, evaluateTaskHash, output);

        profile.setMember(EVALUATOR_AGENT_ID, address(0x1234));
        vm.expectRevert(Workflow.InvalidConfiguration.selector);
        _submitActionAs(workflow, EVALUATOR_WALLET, runId, evaluateTaskHash, output);
    }

    function testProvenEvaluationKeepsItsOwnDAReferenceWhenAnotherCandidateExists() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/candidate-race.json@commit",
            digest: keccak256("candidate race contribution"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 contributionId = _submitContributionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("candidate-race-source"),
            source,
            bytes32(0)
        );

        Workflow.DataRef memory firstEvaluation = Workflow.DataRef({
            locator: "github:data/evaluations/first.json@commit",
            digest: keccak256("first evaluation"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        Workflow.DataRef memory secondEvaluation = Workflow.DataRef({
            locator: "github:data/evaluations/second.json@commit",
            digest: keccak256("second evaluation"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        bytes32 evaluateTaskHash = workflow.getContribution(contributionId).evaluateTaskHash;
        bytes32 firstReply = _submitActionAs(
            workflow,
            EVALUATOR_WALLET,
            runId,
            evaluateTaskHash,
            abi.encode(Workflow.ActionKind.SubmitInitialEvaluation, contributionId, uint8(80), firstEvaluation)
        );
        _submitActionAs(
            workflow,
            EVALUATOR_WALLET,
            runId,
            evaluateTaskHash,
            abi.encode(Workflow.ActionKind.SubmitInitialEvaluation, contributionId, uint8(70), secondEvaluation)
        );

        bytes32[] memory replyHashes = new bytes32[](1);
        replyHashes[0] = firstReply;
        workflow.onAgentProve(replyHashes, hex"a77e57");

        assertEq(workflow.getContribution(contributionId).initialEvaluation.digest, firstEvaluation.digest);
    }

    function testOneAppealReplacesTheSettlementScoreAfterProof() public {
        (bytes32 runId, bytes32 contributionId) = _createScoredContribution(80);
        _closeCollectionAndOpenSuccessor(runId);
        bytes32 openAppealReplyHash = _openAppealPhase(runId);
        Workflow.DataRef memory appeal = Workflow.DataRef({
            locator: "github:data/appeals/one.json@commit",
            digest: keccak256("appeal explanation"),
            expiresAt: uint64(block.timestamp + 365 days)
        });

        Workflow.RoundView memory beforeAppealRound = workflow.getRound(runId);
        assertTrue(beforeAppealRound.appealTaskHash != bytes32(0));
        (AgentTask memory appealTask, bool appealTaskProven) = workflow.getAgentTask(beforeAppealRound.appealTaskHash);
        assertTrue(appealTaskProven);
        assertEq(appealTask.prevReplyHashes.length, 1);
        assertEq(appealTask.prevReplyHashes[0], openAppealReplyHash);

        bytes32 appealReplyHash = _submitActionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            beforeAppealRound.appealTaskHash,
            abi.encode(Workflow.ActionKind.SubmitAppeal, contributionId, appeal)
        );

        Workflow.ContributionView memory afterAppeal = workflow.getContribution(contributionId);
        assertTrue(afterAppeal.appealed);
        assertEq(afterAppeal.appealReplyHash, appealReplyHash);
        assertTrue(afterAppeal.reevaluateTaskHash != bytes32(0));
        assertEq(workflow.getRound(runId).pendingAppealEvaluationCount, 1);

        Workflow.DataRef memory reevaluation = Workflow.DataRef({
            locator: "github:data/evaluations/appeal-one.json@commit",
            digest: keccak256("appeal reevaluation"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 reevaluationReplyHash = _submitActionAs(
            workflow,
            EVALUATOR_WALLET,
            runId,
            afterAppeal.reevaluateTaskHash,
            abi.encode(Workflow.ActionKind.SubmitAppealEvaluation, contributionId, uint8(60), reevaluation)
        );

        bytes32[] memory replyHashes = new bytes32[](1);
        replyHashes[0] = reevaluationReplyHash;
        workflow.onAgentProve(replyHashes, hex"a77e57");

        Workflow.ContributionView memory settledContribution = workflow.getContribution(contributionId);
        assertEq(settledContribution.initialScore, 80);
        assertTrue(settledContribution.appealEvaluated);
        assertEq(settledContribution.appealScore, 60);
        assertEq(settledContribution.finalScore, 60);
        assertEq(settledContribution.appealEvaluationReplyHash, reevaluationReplyHash);
        assertEq(workflow.getRound(runId).pendingAppealEvaluationCount, 0);
        assertEq(workflow.getRound(runId).totalFinalScore, 60);
    }

    function testRoundSummaryUsesAutomaticProofBeforeSettlement() public {
        (bytes32 runId,) = _createScoredContribution(80);
        _closeCollectionAndOpenSuccessor(runId);
        _openAppealPhase(runId);
        Workflow.RoundView memory appealingRound = workflow.getRound(runId);
        assertEq(uint8(appealingRound.phase), uint8(Workflow.RoundPhase.Appealing));
        assertEq(appealingRound.appealDeadline, block.timestamp + APPEAL_DURATION);

        vm.warp(appealingRound.appealDeadline + 1);
        bytes32 summaryTaskHash = workflow.prepareRoundSummary(runId);
        assertTrue(summaryTaskHash != bytes32(0));

        Workflow.DataRef memory summary = Workflow.DataRef({
            locator: "github:data/summaries/round-1.json@commit",
            digest: keccak256("round progress summary"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 summaryReplyHash = _submitActionAs(
            workflow,
            EVALUATOR_WALLET,
            runId,
            summaryTaskHash,
            abi.encode(Workflow.ActionKind.SubmitRoundSummary, runId, summary)
        );
        {
            (, address summaryVerifier, bool summaryProven, bytes32 summaryVerificationDigest) =
                workflow.getAgentReply(summaryReplyHash);
            assertEq(summaryVerifier, address(passThroughVerifier));
            assertTrue(summaryProven);
            assertTrue(summaryVerificationDigest != bytes32(0));
            (,, bytes32 settlementTaskHash) = workflow.getRoundEvidence(runId);
            assertTrue(settlementTaskHash != bytes32(0));
        }

        pointsToken.mint(address(workflow), ROUND_POINT_CAP);
        identityRegistry.setAgentWallet(CONTRIBUTOR_AGENT_ID, ROTATED_CONTRIBUTOR_WALLET);
        vm.recordLogs();
        (,, bytes32 settlementTaskHash) = workflow.getRoundEvidence(runId);
        bytes32 settlementReplyHash = _submitActionAs(
            workflow, OTHER_WALLET, runId, settlementTaskHash, abi.encode(Workflow.ActionKind.SettleRound, runId)
        );
        Vm.Log[] memory settlementLogs = vm.getRecordedLogs();

        Workflow.RoundView memory settledRound = workflow.getRound(runId);
        assertEq(uint8(settledRound.phase), uint8(Workflow.RoundPhase.Settled));
        (, bytes32 storedSummaryReplyHash,) = workflow.getRoundEvidence(runId);
        assertEq(storedSummaryReplyHash, summaryReplyHash);
        assertEq(pointsToken.balanceOf(CONTRIBUTOR_WALLET), 0);
        assertEq(pointsToken.balanceOf(ROTATED_CONTRIBUTOR_WALLET), 80);
        assertEq(workflow.agentRoundScore(runId, CONTRIBUTOR_AGENT_ID), 80);

        _assertTerminalEvidence(runId, settlementTaskHash, settlementReplyHash);

        assertEq(workflow.spent(runId), 80);
        assertEq(workflow.remaining(runId), 0);
        (uint256 settledCap,) = workflow.bound(runId);
        assertEq(settledCap - workflow.spent(runId), ROUND_POINT_CAP - 80);
        assertEq(workflow.getCursor(runId), keccak256(abi.encode(uint256(80))));
        assertEq(uint8(workflow.getEnvelope(runId).status), uint8(IBoundedAgentAction.Status.Completed));

        bytes32 advancedTopic = keccak256("EnvelopeAdvanced(bytes32,bytes32,bytes32)");
        bytes32 statusTopic = keccak256("EnvelopeStatusChanged(bytes32,uint8,uint8)");
        bytes32 completedTopic = keccak256("WorkflowCompleted(bytes32,uint8,bytes32,uint256)");
        bool sawAdvanced;
        bool sawStatus;
        bool sawCompleted;
        for (uint256 i; i < settlementLogs.length; ++i) {
            if (settlementLogs[i].emitter != address(workflow) || settlementLogs[i].topics.length == 0) continue;
            if (settlementLogs[i].topics[0] == advancedTopic) sawAdvanced = true;
            if (settlementLogs[i].topics[0] == statusTopic) sawStatus = true;
            if (settlementLogs[i].topics[0] == completedTopic) sawCompleted = true;
        }
        assertTrue(sawAdvanced);
        assertTrue(sawStatus);
        assertTrue(sawCompleted);
    }

    function testPassThroughVerifierCannotReenterSettlement() public {
        ReentrantPassThroughVerifier reentrantVerifier = new ReentrantPassThroughVerifier();
        Workflow guardedWorkflow = new Workflow(
            address(profile),
            address(pointsToken),
            address(reentrantVerifier),
            EVALUATOR_AGENT_ID,
            ROUND_DURATION,
            APPEAL_DURATION,
            ROUND_POINT_CAP,
            MAX_CONTRIBUTIONS
        );

        bytes32 runId = guardedWorkflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/reentrancy.json@commit",
            digest: keccak256("reentrancy contribution"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 contributionId = _submitContributionAs(
            guardedWorkflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("reentrancy-source"),
            source,
            bytes32(0)
        );
        Workflow.DataRef memory evaluation = Workflow.DataRef({
            locator: "github:data/evaluations/reentrancy.json@commit",
            digest: keccak256("reentrancy evaluation"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 evaluationReplyHash = _submitActionAs(
            guardedWorkflow,
            EVALUATOR_WALLET,
            runId,
            guardedWorkflow.getContribution(contributionId).evaluateTaskHash,
            abi.encode(Workflow.ActionKind.SubmitInitialEvaluation, contributionId, uint8(80), evaluation)
        );
        bytes32[] memory replyHashes = new bytes32[](1);
        replyHashes[0] = evaluationReplyHash;
        guardedWorkflow.onAgentProve(replyHashes, hex"a77e57");

        vm.warp(block.timestamp + ROUND_DURATION);
        _submitActionAs(
            guardedWorkflow,
            OTHER_WALLET,
            runId,
            guardedWorkflow.getRound(runId).collectTaskHash,
            abi.encode(Workflow.ActionKind.CloseCollection, runId)
        );
        guardedWorkflow.run(keccak256(""), "", type(uint256).max);
        bytes32 openAppealTaskHash = guardedWorkflow.prepareOpenAppeal(runId);
        _submitActionAs(
            guardedWorkflow,
            OTHER_WALLET,
            runId,
            openAppealTaskHash,
            abi.encode(Workflow.ActionKind.OpenAppealPhase, runId)
        );
        vm.warp(guardedWorkflow.getRound(runId).appealDeadline + 1);
        bytes32 summaryTaskHash = guardedWorkflow.prepareRoundSummary(runId);
        Workflow.DataRef memory summary = Workflow.DataRef({
            locator: "github:data/summaries/reentrancy.json@commit",
            digest: keccak256("reentrancy summary"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        _submitActionAs(
            guardedWorkflow,
            EVALUATOR_WALLET,
            runId,
            summaryTaskHash,
            abi.encode(Workflow.ActionKind.SubmitRoundSummary, runId, summary)
        );

        (,, bytes32 settlementTaskHash) = guardedWorkflow.getRoundEvidence(runId);
        pointsToken.mint(address(guardedWorkflow), 160);
        reentrantVerifier.arm(guardedWorkflow, runId, settlementTaskHash);
        _submitActionAs(
            guardedWorkflow, OTHER_WALLET, runId, settlementTaskHash, abi.encode(Workflow.ActionKind.SettleRound, runId)
        );

        assertFalse(reentrantVerifier.reentered());
        assertEq(pointsToken.balanceOf(CONTRIBUTOR_WALLET), 80);
        assertEq(guardedWorkflow.spent(runId), 80);
    }

    function testAdvertisesERC8301AndRestrictedERC8312Interfaces() public view {
        assertTrue(workflow.supportsInterface(type(IAgentWorkflow).interfaceId));
        assertTrue(workflow.supportsInterface(type(IBoundedAgentAction).interfaceId));
        assertTrue(workflow.supportsInterface(type(IBudgetSubstrate).interfaceId));
        assertTrue(workflow.supportsInterface(0x01ffc9a7));
    }

    function testExternalCallerCannotMutateRoundBudgetEnvelope() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);

        vm.expectRevert();
        workflow.registerEnvelope(address(this), keccak256("cap"), type(uint64).max, "");

        vm.expectRevert();
        workflow.advanceCursor(runId, abi.encode(uint256(1)));

        vm.expectRevert();
        workflow.setStatus(runId, IBoundedAgentAction.Status.Revoked);
    }

    function testBudgetReadsRejectAnUnknownEnvelope() public {
        bytes32 unknownId = keccak256("unknown-envelope");

        vm.expectRevert();
        workflow.getEnvelope(unknownId);
        vm.expectRevert();
        workflow.getCursor(unknownId);
        vm.expectRevert();
        workflow.bound(unknownId);
        vm.expectRevert();
        workflow.spent(unknownId);
        vm.expectRevert();
        workflow.remaining(unknownId);
    }

    function testStandardOnAgentReplyAcceptsCanonicalContributionAction() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        bytes32 collectTaskHash = workflow.getRound(runId).collectTaskHash;
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/standard-reply.json@commit",
            digest: keccak256("standard reply contribution"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes memory output = abi.encode(
            Workflow.ActionKind.SubmitContribution,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("standard-reply-source"),
            source,
            bytes32(0)
        );
        bytes32[] memory previousTasks = new bytes32[](1);
        previousTasks[0] = collectTaskHash;
        vm.warp(block.timestamp + 1);
        AgentReply memory reply = AgentReply({
            outputHash: keccak256(output),
            output: output,
            timestamp: block.timestamp,
            replier: CONTRIBUTOR_WALLET,
            prevTaskHashes: previousTasks,
            workflowRunId: runId
        });
        bytes32 replyHash = keccak256(
            abi.encode(
                reply.outputHash,
                reply.timestamp,
                reply.replier,
                keccak256(abi.encodePacked(reply.prevTaskHashes)),
                reply.workflowRunId
            )
        );

        vm.prank(CONTRIBUTOR_WALLET);
        workflow.onAgentReply(reply);

        Workflow.ContributionView memory contribution = workflow.getContribution(replyHash);
        assertTrue(contribution.exists);
        assertEq(contribution.attributedAgentId, CONTRIBUTOR_AGENT_ID);
        (, address verifier, bool proven,) = workflow.getAgentReply(replyHash);
        assertEq(verifier, address(passThroughVerifier));
        assertTrue(proven);
    }

    function testSupportingMaterialExtendsEvidenceChainAndReplacesEvaluationTask() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/material.json@commit",
            digest: keccak256("material contribution"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 contributionId = _submitContributionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256("material-source"),
            source,
            bytes32(0)
        );
        bytes32 oldEvaluateTaskHash = workflow.getContribution(contributionId).evaluateTaskHash;

        Workflow.DataRef memory material = Workflow.DataRef({
            locator: "github:data/materials/commit-diff.json@commit",
            digest: keccak256("supporting commit diff"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 materialReplyHash = _submitActionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            oldEvaluateTaskHash,
            abi.encode(Workflow.ActionKind.AppendSupportingMaterial, contributionId, material)
        );

        Workflow.ContributionView memory contribution = workflow.getContribution(contributionId);
        assertEq(contribution.supportingMaterialCount, 1);
        assertTrue(contribution.supportingMaterialRoot != bytes32(0));
        assertTrue(contribution.evaluateTaskHash != oldEvaluateTaskHash);

        (AgentReply memory storedMaterialReply, address materialVerifier, bool materialProven,) =
            workflow.getAgentReply(materialReplyHash);
        assertEq(storedMaterialReply.outputHash, keccak256(storedMaterialReply.output));
        assertEq(materialVerifier, address(passThroughVerifier));
        assertTrue(materialProven);

        (AgentTask memory newEvaluateTask,) = workflow.getAgentTask(contribution.evaluateTaskHash);
        assertEq(newEvaluateTask.prevReplyHashes.length, 1);
        assertEq(newEvaluateTask.prevReplyHashes[0], materialReplyHash);
    }

    function testRunEmitsStandardTaskAndBudgetEnvelopeEvents() public {
        vm.recordLogs();
        workflow.run(keccak256(""), "", type(uint256).max);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bytes32 taskTopic = keccak256("NewAgentTask(bytes32,uint8,bytes32)");
        bytes32 envelopeTopic = keccak256("EnvelopeRegistered(bytes32,address,bytes32)");
        bool sawTask;
        bool sawEnvelope;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter != address(workflow) || logs[i].topics.length == 0) continue;
            if (logs[i].topics[0] == taskTopic) sawTask = true;
            if (logs[i].topics[0] == envelopeTopic) sawEnvelope = true;
        }

        assertTrue(sawTask);
        assertTrue(sawEnvelope);
    }

    function testSettlementGasAtConfiguredContributionLimit() public {
        bytes32 runId = workflow.run(keccak256(""), "", type(uint256).max);

        for (uint256 i; i < MAX_CONTRIBUTIONS; ++i) {
            uint256 agentId = 8004000000000000000000000000000000000000000000000000000000010000 + i;
            address wallet = address(uint160(0x100000 + i));
            profile.setMember(agentId, address(0x8274));
            identityRegistry.setAgentWallet(agentId, wallet);

            Workflow.DataRef memory source = Workflow.DataRef({
                locator: "github:data/contributions/gas.json@commit",
                digest: keccak256(abi.encode("gas contribution", i)),
                expiresAt: uint64(block.timestamp + 365 days)
            });
            bytes32 contributionId = _submitContributionAs(
                workflow,
                EVALUATOR_WALLET,
                runId,
                Workflow.ContributionType.Work,
                agentId,
                keccak256(abi.encode("gas source", i)),
                source,
                bytes32(0)
            );
            Workflow.DataRef memory evaluation = Workflow.DataRef({
                locator: "github:data/evaluations/gas.json@commit",
                digest: keccak256(abi.encode("gas evaluation", i)),
                expiresAt: uint64(block.timestamp + 365 days)
            });
            bytes32 evaluationReplyHash = _submitActionAs(
                workflow,
                EVALUATOR_WALLET,
                runId,
                workflow.getContribution(contributionId).evaluateTaskHash,
                abi.encode(Workflow.ActionKind.SubmitInitialEvaluation, contributionId, uint8(1), evaluation)
            );
            bytes32[] memory replyHashes = new bytes32[](1);
            replyHashes[0] = evaluationReplyHash;
            workflow.onAgentProve(replyHashes, hex"a77e57");
        }

        _closeCollectionAndOpenSuccessor(runId);
        _openAppealPhase(runId);
        vm.warp(workflow.getRound(runId).appealDeadline + 1);
        uint256 summaryGasBefore = gasleft();
        bytes32 summaryTaskHash = workflow.prepareRoundSummary(runId);
        uint256 summaryPreparationGas = summaryGasBefore - gasleft();
        Workflow.DataRef memory summary = Workflow.DataRef({
            locator: "github:data/summaries/gas.json@commit",
            digest: keccak256("gas summary"),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 summaryReplyHash = _submitActionAs(
            workflow,
            EVALUATOR_WALLET,
            runId,
            summaryTaskHash,
            abi.encode(Workflow.ActionKind.SubmitRoundSummary, runId, summary)
        );
        (,, bool summaryProven,) = workflow.getAgentReply(summaryReplyHash);
        assertTrue(summaryProven);
        pointsToken.mint(address(workflow), MAX_CONTRIBUTIONS);

        uint256 gasBefore = gasleft();
        (,, bytes32 settlementTaskHash) = workflow.getRoundEvidence(runId);
        _submitActionAs(
            workflow, OTHER_WALLET, runId, settlementTaskHash, abi.encode(Workflow.ActionKind.SettleRound, runId)
        );
        uint256 settlementGas = gasBefore - gasleft();

        emit log_named_uint("summary preparation gas for 128 contributions", summaryPreparationGas);
        emit log_named_uint("settlement gas for 128 distinct recipients", settlementGas);
        assertEq(workflow.spent(runId), MAX_CONTRIBUTIONS);
    }

    function _submitContributionAs(
        Workflow target,
        address caller,
        bytes32 runId,
        Workflow.ContributionType contributionType,
        uint256 attributedAgentId,
        bytes32 sourceKey,
        Workflow.DataRef memory source,
        bytes32 reviewedContributionId
    ) private returns (bytes32 replyHash) {
        return _submitContributionAgainstTaskAs(
            target,
            caller,
            runId,
            target.getRound(runId).collectTaskHash,
            contributionType,
            attributedAgentId,
            sourceKey,
            source,
            reviewedContributionId
        );
    }

    function _submitContributionAgainstTaskAs(
        Workflow target,
        address caller,
        bytes32 runId,
        bytes32 collectTaskHash,
        Workflow.ContributionType contributionType,
        uint256 attributedAgentId,
        bytes32 sourceKey,
        Workflow.DataRef memory source,
        bytes32 reviewedContributionId
    ) private returns (bytes32 replyHash) {
        bytes memory output = abi.encode(
            Workflow.ActionKind.SubmitContribution,
            contributionType,
            attributedAgentId,
            sourceKey,
            source,
            reviewedContributionId
        );
        return _submitActionAs(target, caller, runId, collectTaskHash, output);
    }

    function _submitActionAs(
        Workflow target,
        address caller,
        bytes32 runId,
        bytes32 previousTaskHash,
        bytes memory output
    ) private returns (bytes32 replyHash) {
        vm.warp(block.timestamp + 1);
        bytes32[] memory previousTasks = new bytes32[](1);
        previousTasks[0] = previousTaskHash;
        AgentReply memory reply = AgentReply({
            outputHash: keccak256(output),
            output: output,
            timestamp: block.timestamp,
            replier: caller,
            prevTaskHashes: previousTasks,
            workflowRunId: runId
        });
        replyHash = keccak256(
            abi.encode(
                reply.outputHash,
                reply.timestamp,
                reply.replier,
                keccak256(abi.encodePacked(reply.prevTaskHashes)),
                reply.workflowRunId
            )
        );
        vm.prank(caller);
        target.onAgentReply(reply);
    }

    function _createScoredContribution(uint8 score) private returns (bytes32 runId, bytes32 contributionId) {
        runId = workflow.run(keccak256(""), "", type(uint256).max);
        Workflow.DataRef memory source = Workflow.DataRef({
            locator: "github:data/contributions/helper.json@commit",
            digest: keccak256(abi.encode("helper contribution", score)),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        contributionId = _submitContributionAs(
            workflow,
            CONTRIBUTOR_WALLET,
            runId,
            Workflow.ContributionType.Work,
            CONTRIBUTOR_AGENT_ID,
            keccak256(abi.encode("helper source", score)),
            source,
            bytes32(0)
        );

        Workflow.DataRef memory evaluation = Workflow.DataRef({
            locator: "github:data/evaluations/helper.json@commit",
            digest: keccak256(abi.encode("helper evaluation", score)),
            expiresAt: uint64(block.timestamp + 365 days)
        });
        bytes32 evaluationReplyHash = _submitActionAs(
            workflow,
            EVALUATOR_WALLET,
            runId,
            workflow.getContribution(contributionId).evaluateTaskHash,
            abi.encode(Workflow.ActionKind.SubmitInitialEvaluation, contributionId, score, evaluation)
        );
        bytes32[] memory replyHashes = new bytes32[](1);
        replyHashes[0] = evaluationReplyHash;
        workflow.onAgentProve(replyHashes, hex"a77e57");
    }

    function _closeCollectionAndOpenSuccessor(bytes32 runId) private {
        vm.warp(block.timestamp + ROUND_DURATION);
        _submitActionAs(
            workflow,
            OTHER_WALLET,
            runId,
            workflow.getRound(runId).collectTaskHash,
            abi.encode(Workflow.ActionKind.CloseCollection, runId)
        );
        workflow.run(keccak256(""), "", type(uint256).max);
    }

    function _openAppealPhase(bytes32 runId) private returns (bytes32 openAppealReplyHash) {
        bytes32 openAppealTaskHash = workflow.prepareOpenAppeal(runId);
        openAppealReplyHash = _submitActionAs(
            workflow, OTHER_WALLET, runId, openAppealTaskHash, abi.encode(Workflow.ActionKind.OpenAppealPhase, runId)
        );
    }

    function _assertTerminalEvidence(bytes32 runId, bytes32 settlementTaskHash, bytes32 settlementReplyHash)
        private
        view
    {
        (RunStatus status, bytes32 finalTaskHash, uint256 completedAt) = workflow.result(runId);
        assertEq(uint8(status), uint8(RunStatus.Success));
        assertTrue(finalTaskHash != settlementTaskHash);
        assertEq(completedAt, block.timestamp);

        (AgentTask memory finalTask, bool finalTaskProven) = workflow.getAgentTask(finalTaskHash);
        assertTrue(finalTaskProven);
        assertEq(finalTask.stage, type(uint8).max);
        assertEq(finalTask.inputHash, keccak256(""));
        assertEq(finalTask.input, bytes(""));
        assertEq(finalTask.prevReplyHashes.length, 1);
        assertEq(finalTask.prevReplyHashes[0], settlementReplyHash);

        (, address settlementVerifier, bool settlementProven,) = workflow.getAgentReply(settlementReplyHash);
        assertEq(settlementVerifier, address(passThroughVerifier));
        assertTrue(settlementProven);
    }
}
