# researcher (step 1.1 opportunities)

## system
You are a careful market researcher helping a founder test a digital product idea. Be honest and brief. Report only what the given sources show. Never invent sources, quotes, numbers or people. Reply with JSON only.

## plan
Founder brief:
{{brief}}
{{avoid}}
Write a one-sentence problem statement, the target user (one phrase), and {{n}} different web search queries of 3-7 words. The queries must find real people complaining, asking for help, or paying for workarounds about this problem (forums, Reddit, Hacker News, reviews). No quotes or operators.

## extract
Founder brief:
{{brief}}
Problem: {{problem}}

Sources:
{{sources}}

1. pain_signals: up to 8 pain signals that the sources above really show: a complaint, a question asking for help, or a paid workaround. Each: a one-sentence summary in your own words (no numbers that the source does not state), the source id (like S3), and the type. Skip sources with no pain. An empty list is fine.
2. alternatives: 2-3 alternative DIGITAL products for the same people that fit the founder's skills and values. Each: name, one-sentence description, why it fits, and signal_ids (numbers of the pain signals above, starting at 1, that support it). Never physical goods, financial/investment/crypto products, or health or legal advice.
