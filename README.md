# Vera Challenge Bot

This submission implements the challenge contract from `challenge-brief.md` and `challenge-testing-brief.md`.

## Approach

The bot is a deterministic FastAPI service rather than an external-LLM wrapper. It stores pushed category, merchant, customer, and trigger contexts in memory with version checks, then composes messages through a trigger router. Each route retrieves only facts present in the input context: category digest items, peer benchmarks, merchant performance, active offers, customer consent, slots, and trigger payload fields.

The composer optimizes for the judge rubric:

- specificity through numbers, dates, source names, prices, and exact payload fields
- category fit through category-aware salutations, offers, and clinical/operator tone
- merchant fit through performance snapshots, locality, active offers, signals, and language hints
- trigger relevance through one route per major trigger family
- engagement through single CTAs, curiosity, loss aversion, and "I will draft/do it" effort externalization

## Multi-turn Handling

`/v1/reply` detects canned WhatsApp auto-replies, opt-outs/hostility, and explicit commitments such as "ok let's do it" or "I want to join". Commitment routes immediately to action instead of asking more qualification questions.

## Running Locally

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then run the provided simulator with `BOT_URL=http://localhost:8080`.

## Tradeoffs

The deterministic composer is less linguistically varied than a strong LLM, but it is fast, replayable, safe against hallucinated claims, and adapts cleanly to post-submission context injection because it reads the latest pushed contexts at send time.

