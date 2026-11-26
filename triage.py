"""AI ticket triage for a Service Desk.

Reads incoming support tickets and returns, for each one:
  - category and priority (P1-P4, using an impact x urgency matrix)
  - which team should own it (L1 Service Desk or an L2 resolver group)
  - a short first-response draft for the user
  - the first troubleshooting steps for the analyst

Two modes:
  * AI mode     - uses Claude through the official Anthropic SDK when credentials
                  are available (ANTHROPIC_API_KEY or `ant auth login`).
  * Offline mode - a keyword rule engine, so the project runs with no account,
                   no internet and no cost (`--offline`, or automatic fallback).

Usage:
    python triage.py                          # triage sample_tickets.json
    python triage.py --file my_tickets.json   # your own tickets
    python triage.py --text "Outlook keeps asking for my password"
    python triage.py --offline                # force the rule engine
    python triage.py --json results.json      # also save the results
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

MODEL = "claude-opus-5-5"

CATEGORIES = [
    "Password / Account lockout",
    "Microsoft 365 / Outlook",
    "VPN / Remote access",
    "Hardware",
    "Software install / license",
    "Network / Wi-Fi",
    "Intune / Device enrollment",
    "Printer",
    "Access request",
    "Security incident",
    "Other",
]
PRIORITIES = ["P1 - Critical", "P2 - High", "P3 - Medium", "P4 - Low"]
TEAMS = [
    "L1 Service Desk",
    "L2 Desktop Support",
    "L2 Network",
    "L2 Identity & Access",
    "L2 Messaging (M365)",
    "Security Operations",
]

SYSTEM_PROMPT = """You are an experienced IT Service Desk analyst triaging incoming tickets.

For each ticket decide:
- category: the single best category from the allowed list.
- priority: use ITIL impact x urgency.
  P1 = many users or a business-critical service down, or a security incident in progress.
  P2 = one user fully blocked from working, or a VIP / time-critical deadline.
  P3 = the user can work with a workaround.
  P4 = a request, question or minor issue.
- team: "L1 Service Desk" when it can be solved on the first contact (password resets,
  how-to questions, simple Outlook issues); otherwise the L2 group that owns it.
  Suspected phishing, malware or a compromised account always goes to "Security Operations".
- reply_to_user: 2-3 friendly sentences in the same language the user wrote in.
  Acknowledge the problem, say what happens next, and give one safe thing they can try now.
  Never ask the user for their password.
- first_steps: 3-5 short, concrete troubleshooting steps for the analyst, in English.
- reasoning: one sentence explaining the priority.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "priority": {"type": "string", "enum": PRIORITIES},
        "team": {"type": "string", "enum": TEAMS},
        "reply_to_user": {"type": "string"},
        "first_steps": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["category", "priority", "team", "reply_to_user", "first_steps", "reasoning"],
    "additionalProperties": False,
}


@dataclass
class Triage:
    ticket_id: str
    category: str
    priority: str
    team: str
    reply_to_user: str
    first_steps: list[str]
    reasoning: str
    engine: str


# --------------------------------------------------------------------------- AI mode
def triage_with_claude(client, ticket: dict) -> Triage:
    """Classify one ticket with Claude, using structured outputs so the JSON is always valid."""
    import anthropic

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Ticket {ticket['id']}:\n\n{ticket['text']}"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        # If the model declines a request, the API retries it on a fallback model automatically.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise anthropic.APIError("The request was declined by the model.", request=None, body=None)

    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return Triage(ticket_id=ticket["id"], engine=f"claude ({response.model})", **data)


def make_client():
    """Return an Anthropic client if the SDK and credentials are available, else None."""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        # Cheap check that credentials resolve (env var or `ant auth login` profile).
        client.models.retrieve(MODEL)
        return client
    except Exception:
        return None


# ---------------------------------------------------------------------- offline mode
RULES = [
    # (category, team, keywords)
    ("Security incident", "Security Operations",
     ["phishing", "suspicious email", "virus", "malware", "ransomware", "hacked", "clicked a link", "compromised"]),
    ("Password / Account lockout", "L1 Service Desk",
     ["password", "locked out", "lockout", "account locked", "reset my", "mfa", "authenticator", "contraseña", "bloquead"]),
    ("VPN / Remote access", "L2 Network", ["vpn", "remote access", "globalprotect", "anyconnect", "forticlient"]),
    ("Microsoft 365 / Outlook", "L2 Messaging (M365)",
     ["outlook", "teams", "onedrive", "sharepoint", "mailbox", "emails", "syncing", "microsoft 365", "office 365", "correo"]),
    ("Intune / Device enrollment", "L2 Desktop Support",
     ["intune", "company portal", "enroll", "compliance", "compliant"]),
    ("Printer", "L2 Desktop Support", ["printer", "print", "impresora", "scanner"]),
    ("Network / Wi-Fi", "L2 Network", ["wifi", "wi-fi", "internet", "network", "no connection", "ethernet", "red "]),
    ("Hardware", "L2 Desktop Support",
     ["laptop", "screen", "monitor", "keyboard", "mouse", "battery", "won't turn on", "blue screen", "bsod", "pantalla"]),
    ("Software install / license", "L2 Desktop Support",
     ["install", "license", "licence", "software", "application", "update", "instalar", "licencia"]),
    ("Access request", "L2 Identity & Access",
     ["access to", "permission", "shared folder", "distribution list", "need access", "acceso", "permiso"]),
]
P1_WORDS = ["everyone", "whole team", "all users", "entire office", "down for", "outage", "ransomware", "todos"]
P2_WORDS = ["urgent", "asap", "can't work", "cannot work", "blocked", "deadline", "ceo", "vp ", "client meeting", "urgente"]
P4_WORDS = ["question", "how do i", "request", "when you can", "no rush", "pregunta", "solicito"]

STEPS = {
    "Security incident": ["Ask the user to disconnect from the network if malware is suspected",
                          "Do not delete the email; report it with the Report Phishing button",
                          "Reset the user's password and revoke sessions if credentials were entered",
                          "Escalate to Security Operations with the message headers"],
    "Password / Account lockout": ["Verify the user's identity following the ID policy",
                                   "Check lockout status and source in Active Directory / Entra ID",
                                   "Unlock and reset the password, require change at next sign-in",
                                   "Check for a stale password on phone or mapped drives if lockouts repeat"],
    "VPN / Remote access": ["Confirm the user has internet without VPN",
                            "Check the VPN client version and the error message",
                            "Verify the account is in the VPN access group",
                            "Restart the VPN client and re-enter credentials"],
    "Microsoft 365 / Outlook": ["Check Microsoft 365 service health",
                                "Test Outlook on the web to isolate the desktop client",
                                "Start Outlook in safe mode / repair the Office install",
                                "Recreate the Outlook profile if the issue persists"],
    "Intune / Device enrollment": ["Open Company Portal and check the device compliance status",
                                   "Sync the device from Settings > Accounts > Access work or school",
                                   "Review the non-compliant policy in the Intune admin center",
                                   "Re-enroll the device if the record is stale"],
    "Printer": ["Confirm the printer is online and has no error on its panel",
                "Check the print queue and clear stuck jobs",
                "Remove and re-add the printer / reinstall the driver"],
    "Network / Wi-Fi": ["Check whether other users on the same site are affected",
                        "Run Test-NetworkHealth.ps1 or ipconfig /all, look for an APIPA address",
                        "Forget and reconnect to the Wi-Fi network",
                        "Escalate to L2 Network with the results if the gateway is unreachable"],
    "Hardware": ["Get the asset tag and warranty status",
                 "Try basic checks: power cycle, external monitor, different charger",
                 "Run the manufacturer's hardware diagnostics",
                 "Arrange a replacement or repair if the device fails diagnostics"],
    "Software install / license": ["Confirm the software is approved in the catalog",
                                   "Check license availability and manager approval",
                                   "Deploy from Company Portal / software center"],
    "Access request": ["Confirm the resource and access level needed",
                       "Get approval from the resource owner or manager",
                       "Add the user to the right security group and ask them to sign out and in"],
    "Other": ["Call the user to gather details and reproduce the issue",
              "Search the knowledge base for similar tickets",
              "Escalate with full notes if not resolved on first contact"],
}


SPANISH_HINTS = ["necesito", "buenos", "hola", "por favor", "gracias", "mi ", "no puedo", "tengo"]


def triage_offline(ticket: dict) -> Triage:
    text = ticket["text"].lower()

    # Score every category by how many of its keywords appear; security always wins,
    # ties go to the rule listed first.
    category, team, best = "Other", "L1 Service Desk", 0
    for cat, owner, words in RULES:
        score = sum(1 for w in words if w in text)
        if cat == "Security incident" and score:
            category, team = cat, owner
            break
        if score > best:
            category, team, best = cat, owner, score

    if category == "Security incident" or any(w in text for w in P1_WORDS):
        priority, why = "P1 - Critical", "Security risk or many users affected."
    elif any(w in text for w in P2_WORDS):
        priority, why = "P2 - High", "The user is blocked or the request is time-critical."
    elif any(w in text for w in P4_WORDS):
        priority, why = "P4 - Low", "Request or question with no work stoppage."
    else:
        priority, why = "P3 - Medium", "Single user affected, work can continue."

    p = priority.split(" - ")[0]
    if any(h in text for h in SPANISH_HINTS):
        reply = (
            f"Gracias por contactar al Service Desk. Registramos tu caso como {category} ({p}) "
            f"y el equipo {team} ya lo está atendiendo. Te contactaremos pronto; "
            "por favor mantén tu equipo encendido y conectado."
        )
    else:
        reply = (
            f"Thanks for contacting the Service Desk. We have logged your issue as {category} ({p}) "
            f"and the {team} team is on it. We will contact you shortly; "
            "please keep your device on and connected."
        )
    return Triage(ticket["id"], category, priority, team, reply, STEPS[category], why, "offline rules")


# ----------------------------------------------------------------------------- main
def load_tickets(args) -> list[dict]:
    if args.text:
        return [{"id": "CLI-1", "text": args.text}]
    path = Path(args.file)
    return json.loads(path.read_text(encoding="utf-8"))


def print_triage(t: Triage) -> None:
    print(f"\n{t.ticket_id}  [{t.priority}]  {t.category}  ->  {t.team}   ({t.engine})")
    print(f"  Why: {t.reasoning}")
    print(f"  Reply to user: {t.reply_to_user}")
    print("  First steps:")
    for i, step in enumerate(t.first_steps, 1):
        print(f"    {i}. {step}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage Service Desk tickets with AI or offline rules.")
    parser.add_argument("--file", default=str(Path(__file__).parent / "sample_tickets.json"))
    parser.add_argument("--text", help="triage a single ticket text")
    parser.add_argument("--offline", action="store_true", help="use the rule engine only")
    parser.add_argument("--json", help="save the results to this JSON file")
    args = parser.parse_args()

    tickets = load_tickets(args)
    client = None if args.offline else make_client()
    if client is None and not args.offline:
        print("Claude not available (no SDK or credentials) - using offline rules.", file=sys.stderr)

    results = []
    for ticket in tickets:
        if client is not None:
            try:
                result = triage_with_claude(client, ticket)
            except Exception as exc:  # keep the queue moving if one call fails
                print(f"{ticket['id']}: AI triage failed ({exc}); using offline rules.", file=sys.stderr)
                result = triage_offline(ticket)
        else:
            result = triage_offline(ticket)
        results.append(result)
        print_triage(result)

    counts: dict[str, int] = {}
    for r in results:
        counts[r.priority] = counts.get(r.priority, 0) + 1
    print("\nSummary: " + ", ".join(f"{p}: {counts[p]}" for p in PRIORITIES if p in counts))

    if args.json:
        Path(args.json).write_text(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"Saved {len(results)} results to {args.json}")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
