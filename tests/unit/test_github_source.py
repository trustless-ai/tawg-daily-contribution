from tawg_bot.source_filters import GitHubPathPolicy


def test_source_policy_excludes_generated_dependencies_builds_and_binaries() -> None:
    policy = GitHubPathPolicy()

    assert policy.classify("README.md", 100).include_body
    assert policy.classify("src/agent.py", 100).include_body
    assert not policy.classify("node_modules/pkg/index.js", 100).include_record
    assert not policy.classify("dist/generated.js", 100).include_record
    assert not policy.classify("assets/logo.png", 100).include_record


def test_source_policy_retains_lockfile_metadata_without_body() -> None:
    decision = GitHubPathPolicy().classify("package-lock.json", 500)

    assert decision.include_record
    assert not decision.include_body
    assert decision.reason == "lockfile_body_excluded"
