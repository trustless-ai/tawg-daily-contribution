# TAWG Daily Rich Digest Design

Date: 2026-08-28  
Status: Approved

## Goal

Make the Telegram Daily catch-up easy to scan on a phone without losing the
grounded contributor narrative or its evidence trail.

## Message structure

Every Daily uses Telegram Rich Markdown and follows this order:

1. A compact bold title and a separate italic UTC window.
2. `Highlights`: one to four direction-level outcomes rendered as Rich Markdown
   quotation blocks. Every active-day highlight ends with one allowlisted
   citation.
3. `What moved`: detailed direction groups with a section heading, an italic
   synthesis, and real Markdown list items containing contributor recognition
   and exactly one trailing citation.
4. `Next up`: `Ideas to follow` and `TODOs`, each rendered as a real Markdown
   list rather than pseudo-bullets.
5. `Trusty's take`: a final quotation block containing an event-centric
   observation and one playful line of encouragement from the bot.

The title, UTC window, section headings, and blank-line separation are part of
the rendering contract. The design uses only Telegram Rich Markdown constructs
supported by the existing `sendRichMessage` transport: bold, italic, section
headings, lists, links, and quotation blocks.

## Highlights contract

- Select an event or direction, not a winning contributor.
- Summarize a meaningful collaboration outcome, such as fast progress, a
  multi-person relay, a closed implementation loop, or a clarified blocker.
- Keep one to four highlights on active days.
- End every highlight with exactly one allowlisted citation.
- On quiet days, state that no source-backed highlight landed; do not invent
  activity.

## What moved contract

- Preserve the existing evidence-grounded contributor recognition.
- Use a level-three Markdown heading for each direction.
- Use one short italic direction synthesis with no citation or source-dependent
  details.
- Use `- ` Rich Markdown list items for concrete work.
- Start a mapped contributor bullet with the exact confirmed public label.
- End every concrete bullet with exactly one allowlisted citation.

## Next up contract

- Use `Ideas to follow` for discussion paths.
- Use `TODOs` for concrete actions; do not label this subsection `Act`.
- Use real Markdown list items so Telegram renders distinct rows.
- Keep one final actionable invitation before the closing bot commentary only
  if it does not duplicate the commentary.

## Trusty's take contract

- Speak from Trusty's observer perspective, not as an awards presenter.
- Pick one collaboration event from facts already established earlier in the
  same Daily; introduce no new source-dependent fact.
- Do not turn the section into an MVP, individual ranking, or personal
  shout-out.
- Do not mention Telegram handles, URLs, or citations in this section.
- Use one `Today's spark` observation and one short playful closing line.
- Humor may reference collaboration rhythm, technical metaphors, or the bot
  itself. It must not mock a contributor or create social pressure.

## Deterministic enforcement

The controller validates:

- exact title and fixed UTC window;
- required section presence and order;
- active and quiet Highlight shapes;
- Rich Markdown direction headings, summaries, and list markers;
- trailing citation binding for Highlights and What moved;
- `Ideas to follow` and `TODOs` list structure;
- a two-part `Trusty's take` quotation with no mentions, URLs, or citations;
- the existing language, privacy, ranking, mention, and citation allowlist
  gates.

## Compatibility

This is a Daily output-contract change only. It does not modify delivery,
Actions scheduling, Modal wrappers, polling, webhooks, or persistence
boundaries. Both Actions and Modal continue to call the same shared Daily
service and Telegram transport.
