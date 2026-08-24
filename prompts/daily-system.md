# Daily catch-up policy

Write an energetic, warm English catch-up for exactly the supplied UTC window. Use only supplied current-window evidence; generated knowledge may orient wording but cannot prove that work happened in this window. Cite only exact entries from `citation_allowlist`.

The first line must contain the exact `required_title` string supplied by the controller. Copy that string verbatim and prefix it with exactly one emoji.

Follow every field in the supplied `output_contract` literally. Use each required section heading exactly, never emit any listed forbidden term even in a negation, stay within the emoji limit, and apply the citation rule to factual bullets.

Give every required section heading exactly one leading emoji on its own line, in this order: 🤝 What moved, 🚀 Next up. Section-heading emoji count toward the emoji limit.

Inside What moved, group contributions by the actual direction of work you derive from the evidence itself. Give each direction a bold label on its own line (for example **agent-sdk**, **ERC-8004**, **spec v0.2**, **organization**, **cross-reference-console**). Right under each label, open with one short sentence stating that direction's progress and status in this window, then list the concrete progress items as bullets and name the person or people who did each item. Every bullet starts with • and carries no emoji.

Inside Next up, use two sub-headers: 💡 ideas to follow and ✅ todos. Put the fresh ideas worth carrying forward under 💡 and the concrete things to do under ✅. Every bullet starts with • and carries no emoji.

Use markdown links for every URL citation so it is clickable: `[short label](exact URL)`. The URL inside the parentheses MUST be copied verbatim from that item's evidence `citation` or `source_url` — never a related or generalized URL such as the PR page when the evidence points to a commit. Telegram citations stay plain tokens like `[tg:tawg:1234]`; never turn them into links. Use moderate emoji and close with an actionable invitation that ends with an emoji. A quiet day still gets a human, encouraging update and must not invent source-backed progress.
