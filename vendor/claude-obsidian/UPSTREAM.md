# Reviewed upstream guidance

- Repository: https://github.com/AgriciDaniel/claude-obsidian
- Revision: `1c1bc49c03a685ee8f5d09c99efe52b42d6673f5`
- Reviewed: 2026-08-23
- Security verdict: CLEAN
- License: MIT; preserved as `LICENSE` in this directory.

The review covered all 34 files under the upstream `skills/` tree. This project vendors only the following text guidance:

- `skills/obsidian-markdown/SKILL.md`
- `skills/wiki-ingest/SKILL.md`
- `skills/wiki-query/SKILL.md`
- `skills/wiki-lint/SKILL.md`
- `skills/wiki-retrieve/SKILL.md`
- `skills/wiki/references/frontmatter.md`
- `skills/wiki/references/operation-transactions.md`
- `skills/wiki/references/provenance.md`

No upstream executable or runtime installer is vendored or invoked. The TAWG wrapper owns authorization, paths, validation, and unattended operation.
