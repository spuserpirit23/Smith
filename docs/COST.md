# Cost model (prototype scale: 1–5 devices)

Prices are list prices checked on 1 Nov 2026 (USD). They change and some vary by region:
re-check in the AWS Pricing Calculator for **ap-south-1** before committing a budget.

## Two ways to pay

The default stack runs the LLM, vision model and TTS **on the server** (Ollama + Piper, see
[LOCAL_MODELS.md](LOCAL_MODELS.md)), so there is no per-use AI bill. The trade-off: the server
has to be much bigger. Hosted APIs are still a config switch away.

| | Local models (default) | Hosted AI APIs |
|---|---|---|
| Server | 4 vCPU / 16 GB, e.g. m7g.xlarge ≈ **$85/mo** (c7g.xlarge 8 GB, vision off ≈ $72/mo) | t4g.small ≈ $8/mo |
| Per conversation turn | **$0** (STT: free Google Web Speech) | ≈ $0.005 (Transcribe + Haiku + Polly neural) |
| 100 turns/day | ≈ $85 + $6 fixed | ≈ $14 + $15 |
| Break-even | ≈ 15,000 turns/month (≈ 500/day) on AWS | |
| Data leaves the box | Only audio to Google STT (replaceable by local Whisper) | Audio, text, camera frames |

On AWS at prototype scale (1–5 devices), hosted APIs are cheaper in dollars. Local models win
when the server is **hardware you already own** (an office mini PC / NUC with 16 GB: the
recurring cost is electricity), at high volume, or when no data may leave the premises.
m7g/c7g prices are third-party listings for ap-south-1 (Nov 2026): verify in the AWS Pricing
Calculator before budgeting.

## Fixed monthly (one EC2 box, hosted-API setup)

| Item | Basis | ≈ USD / month |
|---|---|---|
| EC2 t4g.small, on-demand, ap-south-1 | ~$0.0112/h × 730 h | ~8.2 |
| EBS gp3 20 GB (root, DB lives here) | per GB-month | ~2 |
| Public IPv4 (Elastic IP) | $0.005/h for any public IPv4 | ~3.7 |
| EBS snapshots, 7 nightly (incremental) | small deltas | <1 |
| SSM Parameter Store (standard), Budgets (first 2) | free | 0 |
| **Total fixed** | | **≈ $14–15** |

Levers: a 1-year Savings Plan / RI cuts the instance roughly by a third; stopping the box outside
demo hours (EBS + IP still bill) halves it; a Spot instance is cheaper still but can be reclaimed —
fine for a lab, not for a client demo.

Not used (and why the bill stays small): ALB (~$16+/mo on its own), NAT gateway, Fargate,
IoT Core, RDS, DynamoDB, CloudWatch Logs ingestion.

## Per conversation turn (hosted-API setup)

Assumptions: ~5 s question; ~1,100 input tokens (system prompt + 10-turn context) and ~60 output
tokens; ~150 characters spoken back.

| Stage | Hosted provider | Unit price | ≈ per turn |
|---|---|---|---|
| STT | Amazon Transcribe streaming | $0.01–0.024 / min (AWS's own example shows $0.01 for us-east-1; billed per second) | $0.001–0.002 |
| LLM | Claude Haiku 4.5 via Bedrock global profile | $1 / M input, $5 / M output | ~$0.0014 |
| TTS | Polly **neural** | $16 / M chars (1 M chars/mo free for 12 months) | ~$0.0024 |
| TTS alt | Polly **standard** | $4 / M chars | ~$0.0006 |
| Vision (only on "what do you see") | Haiku 4.5 multimodal | image ≈ 0.5–1 k tokens | ~$0.001 |
| Cloud wake word | openWakeWord on the box | CPU only | $0 |

**≈ $0.005 per turn** with neural voice (≈ ₹0.4). 100 turns/day ≈ $15/month on top of the fixed
cost; a 4-hour demo with a turn every 30 s (~480 turns) ≈ $2.40.

Data residency: the India-only Bedrock profile (`in.anthropic.claude-haiku-4-5-20251001-v1:0`)
keeps inference in Mumbai/Hyderabad at ~10 % higher token prices.

## Cheaper or non-AWS swaps (config only)

| Change | Effect |
|---|---|
| `POLLY_ENGINE=standard` | TTS cost ÷4, more robotic voice |
| `MITRA_STT_PROVIDER=openai_compat` + Groq / OpenAI Whisper | Often cheaper per minute than Transcribe; batch (not streaming) adds ~0.2–0.5 s |
| `MITRA_STT_PROVIDER=google` (`MITRA_EXTRAS=aws,speech`), `MITRA_TTS_PROVIDERS=piper_local` (`,local`) | Free Google Web Speech STT (unofficial, rate-limited, no SLA); Piper TTS on CPU |
| `MITRA_LLM_PROVIDER=openai_compat` → any hosted or self-hosted model | Swap vendors without code changes |

## Guard rails in place

* AWS Budgets: email at 80 % actual and 100 % forecast of `monthly_budget_usd` (Terraform).
* `persona.max_reply_sentences` caps spoken length (and TTS spend).
* `max_tokens: 300` on every LLM call; `conversation.context_turns: 10`.
* Prometheus counters `mitra_tts_characters_total`, `mitra_stt_seconds_total`, `mitra_turns_total`
  give the inputs for a per-device cost report.
