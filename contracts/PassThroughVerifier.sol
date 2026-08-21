// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import {IAgentVerifier} from "@agent-ercs/verify/ERC8274/IAgentVerifier.sol";

/// @notice ERC-8274 verifier for Workflow stages whose acceptance is fully
///         determined by the Workflow contract and requires no external proof.
contract PassThroughVerifier is IAgentVerifier {
    bytes32 public constant PROOF_PROFILE = keccak256("transparent/workflow-gate-v1");

    function verify(bytes32 taskId, bytes32 agentId, bytes32 inputHash, bytes32 outputHash, bytes calldata)
        external
        returns (bool valid, bytes32 verificationDigest)
    {
        valid = true;
        verificationDigest = keccak256(abi.encode(taskId, agentId, inputHash, outputHash, valid, PROOF_PROFILE));
        emit VerificationCompleted(taskId, agentId, inputHash, outputHash, valid, verificationDigest);
    }
}
