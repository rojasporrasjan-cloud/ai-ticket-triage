# AI Ticket Triage for the Service Desk

Reads incoming support tickets and, for each one, decides the **category**, the **priority (P1–P4)**,
the **team that should own it**, a **first reply to the user** and the **first troubleshooting steps** for the analyst.

Triage is the first thing every Service Desk analyst does dozens of times a day. Doing it consistently is what keeps
SLAs: a P1 outage or a phishing click must never sit in the queue behind a password reset.

## Two engines

| Engine | When it runs | Notes |
|---|---|---|
| **Claude (AI)** | When the Anthropic SDK is installed and credentials are set | Understands free text in English or Spanish, writes a personalized reply, uses structured outputs so the result is always valid JSON |
| **Offline rules** | `--offline`, or automatically if the AI is not available | Keyword scoring + an impact × urgency matrix. No account, no internet, no cost |

If an AI call fails, that ticket falls back to the rule engine, so the queue keeps moving.

## Priority rules (ITIL impact × urgency)

| Priority | Meaning | Example |
|---|---|---|
| P1 - Critical | Many users or a critical service down, or a security incident | "Nobody in the office has internet", "I entered my password on a fake page" |
| P2 - High | One user fully blocked, or a hard deadline | "Locked out and I have a client meeting in 30 minutes" |
| P3 - Medium | User can work with a workaround | "Outlook keeps asking for my password, web works" |
| P4 - Low | Request or question | "How do I request a second monitor?" |

## Run it

```bash
# Offline mode: nothing to install
python triage.py --offline
python triage.py --offline --text "Mi impresora no imprime y tengo que entregar un reporte urgente"

# AI mode (needs an Anthropic API key, usage is billed by Anthropic)
pip install -r requirements.txt
set ANTHROPIC_API_KEY=your-key        # PowerShell: $env:ANTHROPIC_API_KEY="your-key"
python triage.py
python triage.py --file my_tickets.json --json results.json
```

## Sample output (offline mode, `sample_tickets.json`)

```
INC100232  [P1 - Critical]  Network / Wi-Fi  ->  L2 Network   (offline rules)
  Why: Security risk or many users affected.
  First steps:
    1. Check whether other users on the same site are affected
    2. Run Test-NetworkHealth.ps1 or ipconfig /all, look for an APIPA address
    ...

INC100234  [P1 - Critical]  Security incident  ->  Security Operations   (offline rules)
  First steps:
    1. Ask the user to disconnect from the network if malware is suspected
    2. Do not delete the email; report it with the Report Phishing button
    3. Reset the user's password and revoke sessions if credentials were entered
    ...

INC100235  [P3 - Medium]  Access request  ->  L2 Identity & Access   (offline rules)
  Reply to user: Gracias por contactar al Service Desk. Registramos tu caso como Access request (P3) ...

Summary: P1 - Critical: 2, P2 - High: 1, P3 - Medium: 4, P4 - Low: 1
```

All 8 sample tickets (English and Spanish) are classified correctly by the offline engine.

## How the AI part works

- One request per ticket to the Claude API (official `anthropic` Python SDK), with a system prompt that encodes the
  triage policy: priority matrix, routing rules, "security always goes to SecOps", "never ask for a password".
- **Structured outputs** (`output_config.format` with a JSON schema): category, priority and team are `enum` fields,
  so the model can only answer with values the ticketing system accepts.
- Low reasoning effort, because triage is a short classification task.

## Skills shown

ITIL incident management (impact × urgency, routing, escalation) · Python · prompt design for a real business process ·
LLM structured outputs · fail-safe design (fallback engine) · bilingual EN/ES support.

## How I built it

Built with the help of AI (Claude) for the code. The triage policy, categories, routing and troubleshooting steps come
from my 3.5 years doing Service Desk and L2 end-user support at Kyndryl and Blue Mantis.

## License

MIT
