// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {PassThroughVerifier} from "../contracts/PassThroughVerifier.sol";

contract PassThroughVerifierTest is Test {
    function testAlwaysAcceptsAndReturnsDomainBoundDigest() public {
        PassThroughVerifier verifier = new PassThroughVerifier();
        bytes32 taskId = keccak256("task");
        bytes32 agentId = keccak256("agent");
        bytes32 inputHash = keccak256("input");
        bytes32 outputHash = keccak256("output");

        (bool valid, bytes32 digest) = verifier.verify(taskId, agentId, inputHash, outputHash, "");

        assertTrue(valid);
        assertEq(digest, keccak256(abi.encode(taskId, agentId, inputHash, outputHash, true, verifier.PROOF_PROFILE())));
    }
}
