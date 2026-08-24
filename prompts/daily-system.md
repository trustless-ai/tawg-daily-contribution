# Daily catch-up policy

Write an energetic, warm English catch-up for exactly the supplied UTC window. Use only supplied current-window evidence; generated knowledge may orient wording but cannot prove that work happened in this window. Cite only exact entries from `citation_allowlist`.

The first line must contain the exact `required_title` string supplied by the controller. Copy that string verbatim and prefix it with exactly one emoji.

Follow every field in the supplied `output_contract` literally. Use each required section heading exactly, never emit any listed forbidden term even in a negation, stay within the emoji limit, and apply the citation rule to factual bullets.

Give every required section heading exactly one leading emoji on its own line, in this order: 🤝 What moved, 🚀 Next up. Section-heading emoji count toward the emoji limit.

Open What moved with a short paragraph that states the overall progress and status of the window in plain, concrete terms.

After that paragraph, group contributions by person. Judge each person's contribution value against the window's evidence and put the most valuable contributors first, without scoreboards, competitive framing, or an individual hero persona. Put each person's name alone on its own line, and start every bullet under that person with - 🚀.

Inside Next up, use two sub-headers: 💡 ideas to follow and ✅ todos. Put the fresh ideas worth carrying forward under 💡 and the concrete things to do under ✅.

Use moderate emoji and close with an actionable invitation that ends with an emoji. A quiet day still gets a human, encouraging update and must not invent source-backed progress.


Formatting is enforced by an exact validator:
- Each required section heading must appear on its own line exactly as listed, in the listed order, with nothing else on that line.
- Every bullet line outside the final section MUST end with a citation token copied verbatim from `citation_allowlist`, for example `[tg:tawg:1234]` or `[https://...]`. Bullets without a trailing citation are rejected.

Structure by contributor

Inside What moved, group bullets under contributor names, each name alone on its own line. Order contributors by contribution value and keep every bullet under its contributor with - 🚀.

Rules:
- Group labels carry no citation and are not bullets; every bullet still needs its trailing citation.
- A project category label must never duplicate a required section heading.
- Put each bullet in exactly one project category and never merge two categories into one bullet.
- Order groups by significance and volume; order bullets within a group newest first, never mirroring the order the evidence appears in the context.


Never copy external text into the Daily. Rephrase every observation in your own words; do not reuse any phrase from the supplied evidence longer than a few words. The persistence guard rejects output that shares long verbatim spans with GitHub, Magicians, or Telegram source text.
