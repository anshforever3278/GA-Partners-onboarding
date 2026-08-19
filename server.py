"""G&A Partners Onboarding POC — FastAPI backend."""
import os
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
import bcrypt
import jwt

from ai_service import (
    generate_progress_note,
    generate_status_report,
    draft_inquiry_response,
    csa_cross_validation_narrative,
)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGO = "HS256"

mongo_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = mongo_client[os.environ["DB_NAME"]]

app = FastAPI(title="G&A Onboarding POC")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ga-onboarding")

PHASES = ["Kickoff", "Data Collection", "Configuration", "Testing", "First Payroll"]
DOC_TYPES = ["Census", "Codes", "Payroll Report", "Reverse Wire", "Handbook", "WTPA"]

SAMPLE_CSA_EXTRACT = {
    "fee_structure": {"per_employee_per_month": 95.00, "setup_fee": 2500.00},
    "term": "24 months",
    "included_services": ["Payroll", "Benefits Admin", "HR Advisory", "Tax Filing"],
    "optional_services": ["Risk Management", "401k Admin"],
    "suta_coverage": {"state": "TX", "rate": 0.027, "type": "standard"},
    "billing_exceptions": ["Off-cycle payrolls billed separately at $250/run"],
    "tax_classification": "PEO Co-employment",
    "cyber_policy": {"carrier": "Hartford", "limit": 5_000_000},
    "ownership": "ABC Holdings LLC — 100%",
}


def iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------- Mailer Simulation ----------------
DEFAULT_SETTINGS = {
    "id": "global",
    "from_email": "alerts@gandapartners.com",
    "sender_name": "G&A Onboarding",
    "recipients": {
        "pm_manager": "pm.manager@gandapartners.com",
        "ba": "business.analyst@gandapartners.com",
        "dept_head": "department.head@gandapartners.com",
        "executive_sponsor": "executive.sponsor@gandapartners.com",
        "benefits": "benefits@gandapartners.com",
        "team_401k": "401k@gandapartners.com",
        "tax": "tax@gandapartners.com",
        "risk": "risk@gandapartners.com",
    },
    "updated_at": None,
}


async def _get_settings() -> dict:
    s = await db.email_settings.find_one({"id": "global"}, {"_id": 0})
    if not s:
        s = DEFAULT_SETTINGS.copy()
        s["updated_at"] = iso(datetime.now(timezone.utc))
        await db.email_settings.insert_one(s.copy())
    return s


async def _send_mail(client_id: Optional[str], to: List[str], subject: str,
                    body: str, event_type: str, level: int = 0):
    """Simulated email send — logs to email_logs collection only."""
    s = await _get_settings()
    log = {
        "id": str(uuid.uuid4()),
        "client_id": client_id,
        "from_email": s["from_email"],
        "sender_name": s["sender_name"],
        "to": [t for t in to if t],
        "subject": subject,
        "body": body,
        "event_type": event_type,
        "level": level,
        "status": "Sent (simulated)",
        "sent_at": iso(datetime.now(timezone.utc)),
    }
    await db.email_logs.insert_one(log.copy())
    return log


# ---------------- Auth ----------------
class LoginReq(BaseModel):
    username: str
    password: str


def make_token(user: dict) -> str:
    payload = {
        "sub": user["id"], "username": user["username"], "role": user["role"],
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


async def current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing auth")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(401, "User not found")
    return user


@api.post("/auth/login")
async def login(req: LoginReq):
    user = await db.users.find_one({"username": req.username})
    if not user or not bcrypt.checkpw(req.password.encode(), user["password_hash"].encode()):
        raise HTTPException(401, "Invalid credentials")
    token = make_token(user)
    return {"token": token, "user": {"id": user["id"], "username": user["username"],
                                      "role": user["role"], "name": user["name"]}}


@api.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    return user


# ---------------- Clients ----------------
@api.get("/clients")
async def list_clients(user: dict = Depends(current_user)):
    clients = await db.clients.find({}, {"_id": 0}).to_list(500)
    client_ids = [c["id"] for c in clients]
    all_docs = await db.documents.find(
        {"client_id": {"$in": client_ids}}, {"_id": 0}
    ).to_list(50000)
    docs_by_client: dict = {}
    for d in all_docs:
        docs_by_client.setdefault(d["client_id"], []).append(d)
    out = []
    for c in clients:
        docs = docs_by_client.get(c["id"], [])
        received = sum(1 for d in docs if d["status"] == "Received")
        total = len(docs) or 1
        max_esc = max(
            (d.get("escalation_level", 0) for d in docs if d["status"] == "Pending"),
            default=0,
        )
        c["doc_progress"] = round(received / total * 100)
        c["docs_received"] = received
        c["docs_total"] = total
        c["max_escalation"] = max_esc
        out.append(c)
    return out


@api.get("/clients/{client_id}")
async def get_client(client_id: str, user: dict = Depends(current_user)):
    c = await db.clients.find_one({"id": client_id}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Client not found")
    phases = await db.phases.find({"client_id": client_id}, {"_id": 0}).to_list(20)
    phases.sort(key=lambda p: PHASES.index(p["phase_name"]))
    docs = await db.documents.find({"client_id": client_id}, {"_id": 0}).to_list(50)
    cfg = await db.payroll_configs.find_one({"client_id": client_id}, {"_id": 0})
    csa = await db.csa_extractions.find_one({"client_id": client_id}, {"_id": 0})
    comms = await db.communications.find({"client_id": client_id}, {"_id": 0}).sort("created_at", -1).to_list(50)
    cs = await db.clientspace_records.find_one({"client_id": client_id}, {"_id": 0})
    phr = await db.prismhr_records.find_one({"client_id": client_id}, {"_id": 0})
    ws = await db.worksight_projects.find_one({"client_id": client_id}, {"_id": 0})
    notes = await db.notifications.find({"client_id": client_id}, {"_id": 0}).sort("created_at", -1).to_list(100)
    email_logs = await db.email_logs.find({"client_id": client_id}, {"_id": 0}).sort("sent_at", -1).to_list(200)
    return {
        "client": c, "phases": phases, "documents": docs, "payroll_config": cfg,
        "csa_extraction": csa, "communications": comms,
        "clientspace": cs, "prismhr": phr, "worksight": ws, "notifications": notes,
        "email_logs": email_logs,
    }


class ClientCreate(BaseModel):
    name: str


@api.post("/clients")
async def create_client(req: ClientCreate, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    cid = str(uuid.uuid4())
    client_doc = {
        "id": cid, "name": req.name, "status": "Kickoff", "current_phase": "Kickoff",
        "days_in_phase": 0, "csa_uploaded": False, "handoff_data": {},
        "clientspace_id": f"CS-{uuid.uuid4().hex[:8].upper()}",
        "prismhr_id": f"PHR-{uuid.uuid4().hex[:8].upper()}",
        "created_at": iso(now), "updated_at": iso(now),
        "phase_started_at": iso(now), "pm_id": None, "pa_id": None,
    }
    await db.clients.insert_one(client_doc.copy())
    for p in PHASES:
        await db.phases.insert_one({
            "id": str(uuid.uuid4()), "client_id": cid, "phase_name": p,
            "status": "In Progress" if p == "Kickoff" else "Not Started",
            "started_at": iso(now) if p == "Kickoff" else None,
            "completed_at": None, "progress_note": "",
        })
    for dt in DOC_TYPES:
        await db.documents.insert_one({
            "id": str(uuid.uuid4()), "client_id": cid,
            "name": f"{req.name}_{dt}.pdf", "type": dt, "status": "Pending",
            "uploaded_at": None, "escalation_level": 0,
        })
    await db.payroll_configs.insert_one({
        "id": str(uuid.uuid4()), "client_id": cid,
        "wc_codes": {}, "suta_rates": {}, "billing_rules": {},
        "pay_groups": {}, "deduction_codes": {},
        "validation_status": "Pending", "validation_report": "",
    })
    await db.clientspace_records.insert_one({
        "id": str(uuid.uuid4()), "client_id": cid,
        "case_id": client_doc["clientspace_id"], "status": "Kickoff",
        "last_sync": iso(now),
    })
    await db.prismhr_records.insert_one({
        "id": str(uuid.uuid4()), "client_id": cid,
        "phr_id": client_doc["prismhr_id"], "status": "Kickoff",
        "last_sync": iso(now),
    })
    client_doc.pop("_id", None)
    return client_doc


# ---------------- Documents & Escalation ----------------
@api.post("/clients/{client_id}/documents/{doc_id}/receive")
async def receive_document(client_id: str, doc_id: str, user: dict = Depends(current_user)):
    res = await db.documents.update_one(
        {"id": doc_id, "client_id": client_id},
        {"$set": {"status": "Received", "uploaded_at": iso(datetime.now(timezone.utc)),
                  "escalation_level": 0}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Document not found")
    return {"ok": True}


def _escalation_for_days(days: int) -> int:
    if days >= 10:
        return 2
    if days >= 5:
        return 1
    return 0


async def _push_notification(client_id: str, level: int, message: str):
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()), "client_id": client_id, "level": level,
        "message": message, "created_at": iso(datetime.now(timezone.utc)),
        "read": False,
    })


@api.post("/clients/{client_id}/simulate-day")
async def simulate_day(client_id: str, user: dict = Depends(current_user)):
    c = await db.clients.find_one({"id": client_id})
    if not c:
        raise HTTPException(404, "Client not found")
    new_days = c.get("days_in_phase", 0) + 1
    await db.clients.update_one(
        {"id": client_id},
        {"$set": {"days_in_phase": new_days, "updated_at": iso(datetime.now(timezone.utc))}},
    )
    pending_docs = await db.documents.find({"client_id": client_id, "status": "Pending"}, {"_id": 0}).to_list(50)
    new_esc = _escalation_for_days(new_days)
    settings = await _get_settings()
    r = settings["recipients"]
    client_contact = c.get("primary_contact_email") or f"contact@{c['name'].lower().replace(' ', '').replace('.','').replace(',','')}.com"
    msgs = []
    for d in pending_docs:
        old = d.get("escalation_level", 0)
        await db.documents.update_one({"id": d["id"]}, {"$set": {"escalation_level": new_esc}})
        if new_days in (1, 3) and new_esc == 0:
            msgs.append(f"Reminder email sent to client for {d['type']} (Day {new_days}).")
            await _push_notification(client_id, 0, f"Reminder sent: {d['type']} (Day {new_days})")
            await _send_mail(
                client_id, [client_contact],
                f"Reminder · {d['type']} required for {c['name']} onboarding",
                f"Hi,\n\nWe are still missing the following document for your onboarding: {d['type']}.\n"
                f"Please upload it via WorkSight to avoid escalation. This is reminder #{1 if new_days == 1 else 2} (Day {new_days}).\n\n"
                f"— {settings['sender_name']}",
                event_type="reminder", level=0,
            )
        if new_days == 5 and old < 1:
            msgs.append(f"ESCALATED to PM Manager + BA: {d['type']} overdue (Day 5).")
            await _push_notification(client_id, 1, f"Day 5 escalation: {d['type']} overdue")
            await _send_mail(
                client_id, [r["pm_manager"], r["ba"]],
                f"[ESCALATION · Day 5] {c['name']} — {d['type']} overdue",
                f"Day 5 escalation triggered for {c['name']}.\n\n"
                f"Document overdue: {d['type']}\nClient contact: {client_contact}\nPhase: {c.get('current_phase')}\n\n"
                f"Please intervene. Auto-escalation will move to Dept Head + Executive Sponsor on Day 10.\n\n"
                f"— {settings['sender_name']}",
                event_type="escalation_d5", level=1,
            )
        if new_days == 10 and old < 2:
            msgs.append(f"ESCALATED to Department Head + Executive Sponsor: {d['type']} (Day 10).")
            await _push_notification(client_id, 2, f"Day 10 escalation: {d['type']} overdue")
            await _send_mail(
                client_id, [r["dept_head"], r["executive_sponsor"]],
                f"[ESCALATION · Day 10] {c['name']} — {d['type']} critical",
                f"Day 10 critical escalation for {c['name']}.\n\n"
                f"Document: {d['type']}\nClient contact: {client_contact}\nPhase: {c.get('current_phase')}\n\n"
                f"Executive intervention requested.\n\n— {settings['sender_name']}",
                event_type="escalation_d10", level=2,
            )
    return {"days_in_phase": new_days, "escalation_level": new_esc, "messages": msgs}


@api.get("/clients/{client_id}/notifications")
async def list_notifications(client_id: str, user: dict = Depends(current_user)):
    return await db.notifications.find({"client_id": client_id}, {"_id": 0}).sort("created_at", -1).to_list(100)


# ---------------- Phases ----------------
@api.post("/clients/{client_id}/phases/{phase_name}/complete")
async def complete_phase(client_id: str, phase_name: str, user: dict = Depends(current_user)):
    if phase_name not in PHASES:
        raise HTTPException(400, "Invalid phase")
    c = await db.clients.find_one({"id": client_id})
    if not c:
        raise HTTPException(404, "Client not found")
    if phase_name == "Data Collection":
        pending = await db.documents.find({"client_id": client_id, "status": "Pending"}).to_list(1)
        if pending:
            raise HTTPException(400, "Cannot complete Data Collection — required documents still Pending.")

    now = datetime.now(timezone.utc)
    docs = await db.documents.find({"client_id": client_id}, {"_id": 0}).to_list(50)
    doc_summary = ", ".join([f"{d['type']}:{d['status']}" for d in docs])
    note = await generate_progress_note(c["name"], phase_name, doc_summary)

    await db.phases.update_one(
        {"client_id": client_id, "phase_name": phase_name},
        {"$set": {"status": "Complete", "completed_at": iso(now), "progress_note": note}},
    )

    idx = PHASES.index(phase_name)
    next_phase = PHASES[idx + 1] if idx + 1 < len(PHASES) else None
    update_fields = {"updated_at": iso(now), "days_in_phase": 0, "phase_started_at": iso(now)}
    if next_phase:
        update_fields["current_phase"] = next_phase
        update_fields["status"] = next_phase
        await db.phases.update_one(
            {"client_id": client_id, "phase_name": next_phase},
            {"$set": {"status": "In Progress", "started_at": iso(now)}},
        )
    else:
        update_fields["status"] = "Transitioned"
    await db.clients.update_one({"id": client_id}, {"$set": update_fields})

    await db.clientspace_records.update_one(
        {"client_id": client_id},
        {"$set": {"status": update_fields.get("status"), "last_sync": iso(now)}},
    )
    await db.prismhr_records.update_one(
        {"client_id": client_id},
        {"$set": {"status": update_fields.get("status"), "last_sync": iso(now)}},
    )

    if next_phase == "First Payroll":
        specialist = await db.team_members.find_one({"role": "Payroll Specialist"}, {"_id": 0})
        if specialist:
            await _push_notification(client_id, 0, f"Auto-assigned Payroll Specialist: {specialist['name']}")
            await _send_mail(
                client_id,
                [f"{specialist['name'].lower().replace(' ', '.')}@gandapartners.com"],
                f"[Assignment] You've been assigned to {c['name']} for First Payroll",
                f"Hi {specialist['name']},\n\nYou've been auto-assigned as Payroll Specialist for {c['name']}. "
                f"The client has just transitioned into the First Payroll phase. Please review the pre-flight report and reach out to the client contact.\n\n"
                f"— G&A Onboarding",
                event_type="auto_assignment",
            )

    if next_phase == "Testing":
        settings = await _get_settings()
        r = settings["recipients"]
        dept_map = {"Benefits": r["benefits"], "401k": r["team_401k"], "Tax": r["tax"], "Risk": r["risk"]}
        for dept, addr in dept_map.items():
            await _push_notification(client_id, 0, f"Departmental follow-up triggered: {dept}")
            await _send_mail(
                client_id, [addr],
                f"[Follow-up] {c['name']} entering Testing phase — {dept} action needed",
                f"Hi {dept} Team,\n\n{c['name']} has just transitioned into Testing. Please review and confirm your "
                f"departmental tasks are complete before First Payroll.\n\n— G&A Onboarding",
                event_type="dept_followup",
            )

    if next_phase == "Data Collection":
        exists = await db.worksight_projects.find_one({"client_id": client_id})
        if not exists:
            await db.worksight_projects.insert_one({
                "id": str(uuid.uuid4()), "client_id": client_id,
                "name": f"DataCollection - {c['name']}", "status": "Active",
                "created_at": iso(now), "provisioned_admin": True,
            })
            await _push_notification(client_id, 0, "WorkSight Data Collection project auto-created.")

    return {"phase": phase_name, "next_phase": next_phase, "progress_note": note}


class PhaseNoteUpdate(BaseModel):
    progress_note: str


@api.patch("/clients/{client_id}/phases/{phase_name}/note")
async def update_phase_note(client_id: str, phase_name: str, req: PhaseNoteUpdate,
                            user: dict = Depends(current_user)):
    await db.phases.update_one(
        {"client_id": client_id, "phase_name": phase_name},
        {"$set": {"progress_note": req.progress_note}},
    )
    return {"ok": True}


# ---------------- Sync ----------------
@api.post("/clients/{client_id}/sync-clientspace")
async def sync_clientspace(client_id: str, user: dict = Depends(current_user)):
    c = await db.clients.find_one({"id": client_id})
    if not c:
        raise HTTPException(404, "Client not found")
    now = iso(datetime.now(timezone.utc))
    await db.clientspace_records.update_one(
        {"client_id": client_id},
        {"$set": {"status": c["status"], "last_sync": now}},
    )
    return {"synced_at": now, "status": c["status"]}


@api.post("/clients/{client_id}/sync-prismhr")
async def sync_prismhr(client_id: str, user: dict = Depends(current_user)):
    c = await db.clients.find_one({"id": client_id})
    if not c:
        raise HTTPException(404, "Client not found")
    now = iso(datetime.now(timezone.utc))
    await db.prismhr_records.update_one(
        {"client_id": client_id},
        {"$set": {"status": c["status"], "last_sync": now}},
    )
    return {"synced_at": now, "status": c["status"]}


# ---------------- CSA Extraction ----------------
@api.post("/clients/{client_id}/csa/simulate-upload")
async def simulate_csa_upload(client_id: str, user: dict = Depends(current_user)):
    c = await db.clients.find_one({"id": client_id})
    if not c:
        raise HTTPException(404, "Client not found")
    handoff = c.get("handoff_data") or {}
    extracted = SAMPLE_CSA_EXTRACT
    discrepancies = []
    for k, v in extracted.items():
        if k not in handoff:
            continue
        if handoff[k] != v:
            discrepancies.append({"field": k, "csa_value": v, "handoff_value": handoff[k]})
    narrative = await csa_cross_validation_narrative(discrepancies)
    status = "Pass" if not discrepancies else "Fail"

    payload = {
        "id": str(uuid.uuid4()), "client_id": client_id,
        "extracted_data": extracted, "discrepancies": discrepancies,
        "validation_status": status, "reviewed_by": [user["id"]],
        "narrative": narrative,
        "created_at": iso(datetime.now(timezone.utc)),
    }
    await db.csa_extractions.delete_many({"client_id": client_id})
    await db.csa_extractions.insert_one(payload.copy())
    await db.clients.update_one({"id": client_id}, {"$set": {"csa_uploaded": True}})
    payload.pop("_id", None)
    return payload


# ---------------- Pre-Flight Validation ----------------
@api.post("/clients/{client_id}/preflight")
async def run_preflight(client_id: str, user: dict = Depends(current_user)):
    cfg = await db.payroll_configs.find_one({"client_id": client_id}, {"_id": 0})
    if not cfg:
        raise HTTPException(404, "No payroll config")
    issues: List[str] = []

    missing_wc = [emp for emp, code in (cfg.get("wc_codes") or {}).items() if not code]
    if missing_wc:
        issues.append(f"Missing WC Codes for: {', '.join(missing_wc)}. Assign valid code (e.g., 8810).")

    states_in_use = set()
    for group, states in (cfg.get("pay_groups") or {}).items():
        for s in states:
            states_in_use.add(s)
    suta = cfg.get("suta_rates") or {}
    for s in states_in_use:
        if s not in suta:
            issues.append(f"Missing SUTA rate for state {s}. Pre-approval required for non-standard rates.")

    br = cfg.get("billing_rules") or {}
    if not br.get("mapped") or not br.get("fee_match"):
        issues.append("Billing rules incomplete or fees not mapped to services.")

    if not cfg.get("pay_groups"):
        issues.append("Pay groups not defined.")

    for code, status in (cfg.get("deduction_codes") or {}).items():
        if status != "valid":
            issues.append(f"Deduction code {code} not mapped to a valid account.")

    status = "Pass" if not issues else "Fail"
    lines = ["PRE-FLIGHT VALIDATION REPORT", f"Status: {status}", ""]
    if issues:
        lines.append("Remediation Steps:")
        for i, issue in enumerate(issues, 1):
            lines.append(f"  {i}. {issue}")
    else:
        lines.append("All checks passed. Mock payroll can be executed.")
    suta_non_standard = [s for s, r in suta.items() if r > 0.05]
    if suta_non_standard:
        lines.append(f"\nFlagged for executive review: non-standard SUTA in {', '.join(suta_non_standard)}.")
    else:
        lines.append("\nSUTA rates standard — auto-approved (pre-approval logic).")
    report = "\n".join(lines)
    await db.payroll_configs.update_one(
        {"client_id": client_id},
        {"$set": {"validation_status": status, "validation_report": report}},
    )
    return {"status": status, "issues": issues, "report": report}


# ---------------- Communications ----------------
def _classify_inquiry(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["payroll", "paycheck", "pay run", "tax filing", "first payroll"]):
        return "Payroll"
    if any(k in t for k in ["benefit", "401k", "insurance", "medical", "dental"]):
        return "Benefits"
    if any(k in t for k in ["handbook", "policy", "hr advisory", "compliance"]):
        return "HR Advisory"
    if any(k in t for k in ["risk", "workers comp", "safety", " wc "]):
        return "Risk"
    return "General"


_ROUTING = {
    "Payroll": "Payroll Specialist",
    "Benefits": "Benefits Team",
    "HR Advisory": "HR Advisory Team",
    "Risk": "Risk Management Team",
    "General": "Onboarding PM",
}


class InquiryReq(BaseModel):
    subject: str
    body: str


@api.post("/clients/{client_id}/communications")
async def create_inquiry(client_id: str, req: InquiryReq, user: dict = Depends(current_user)):
    c = await db.clients.find_one({"id": client_id}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Client not found")
    category = _classify_inquiry(req.subject + " " + req.body)
    routed_to = _ROUTING[category]
    docs = await db.documents.find({"client_id": client_id}, {"_id": 0}).to_list(50)
    received = sum(1 for d in docs if d["status"] == "Received")
    total = len(docs) or 1
    status_data = {
        "phase": c["current_phase"],
        "completion_pct": round(received / total * 100),
        "docs_received": received, "docs_total": total,
    }
    draft = await draft_inquiry_response(c["name"], req.body, category, status_data)
    payload = {
        "id": str(uuid.uuid4()), "client_id": client_id, "type": "Inquiry",
        "category": category, "subject": req.subject, "body": req.body,
        "draft_response": draft, "routed_to": routed_to,
        "created_at": iso(datetime.now(timezone.utc)),
    }
    await db.communications.insert_one(payload.copy())
    payload.pop("_id", None)
    return payload


@api.post("/clients/{client_id}/status-report")
async def status_report(client_id: str, user: dict = Depends(current_user)):
    c = await db.clients.find_one({"id": client_id}, {"_id": 0})
    if not c:
        raise HTTPException(404, "Client not found")
    docs = await db.documents.find({"client_id": client_id}, {"_id": 0}).to_list(50)
    phases = await db.phases.find({"client_id": client_id}, {"_id": 0}).to_list(20)
    completed = [p["phase_name"] for p in phases if p["status"] == "Complete"]
    pending = [p["phase_name"] for p in phases if p["status"] != "Complete"]
    received = sum(1 for d in docs if d["status"] == "Received")
    total = len(docs) or 1
    completion_pct = round(received / total * 100)
    estimated = (datetime.now(timezone.utc) + timedelta(days=max(1, 14 - completion_pct // 8))).strftime("%b %d, %Y")
    data = {"completion_pct": completion_pct, "completed": completed, "pending": pending,
            "current_phase": c["current_phase"], "go_live": estimated}
    body = await generate_status_report(c["name"], data)
    payload = {
        "id": str(uuid.uuid4()), "client_id": client_id, "type": "Status Report",
        "category": "General", "subject": f"Weekly Status — {c['name']}",
        "body": body, "draft_response": "", "routed_to": "Client",
        "created_at": iso(datetime.now(timezone.utc)),
    }
    await db.communications.insert_one(payload.copy())
    payload.pop("_id", None)
    # Simulated client email send
    client_contact = c.get("primary_contact_email") or f"contact@{c['name'].lower().replace(' ', '').replace('.','').replace(',','')}.com"
    await _send_mail(
        client_id, [client_contact],
        f"Weekly Status — {c['name']}",
        body, event_type="status_report",
    )
    return payload


# ---------------- Team / WorkSight ----------------
@api.get("/team")
async def list_team(user: dict = Depends(current_user)):
    return await db.team_members.find({}, {"_id": 0}).to_list(50)


@api.post("/clients/{client_id}/worksight/provision")
async def provision_worksight(client_id: str, user: dict = Depends(current_user)):
    c = await db.clients.find_one({"id": client_id})
    if not c:
        raise HTTPException(404, "Client not found")
    now = iso(datetime.now(timezone.utc))
    exists = await db.worksight_projects.find_one({"client_id": client_id})
    payload = {"name": f"DataCollection - {c['name']}", "status": "Active",
               "created_at": now, "provisioned_admin": True}
    if exists:
        await db.worksight_projects.update_one({"client_id": client_id}, {"$set": payload})
    else:
        payload.update({"id": str(uuid.uuid4()), "client_id": client_id})
        await db.worksight_projects.insert_one(payload.copy())
    payload.pop("_id", None)
    return {"ok": True, "worksight": payload}


# ---------------- Dashboard summary ----------------
@api.get("/dashboard/summary")
async def dashboard_summary(user: dict = Depends(current_user)):
    clients = await db.clients.find({}, {"_id": 0}).to_list(500)
    active = [c for c in clients if c["status"] != "Transitioned"]
    client_ids = [c["id"] for c in clients]
    all_pending = await db.documents.find(
        {"client_id": {"$in": client_ids}, "status": "Pending"}, {"_id": 0}
    ).to_list(25000)
    escalations_d10 = sum(1 for d in all_pending if d.get("escalation_level") == 2)
    escalations_d5 = sum(1 for d in all_pending if d.get("escalation_level") == 1)
    overdue_docs = len(all_pending)
    avg_days = sum(c.get("days_in_phase", 0) for c in active) / max(1, len(active))
    baseline_cycle = 50
    projected = max(36, baseline_cycle - 12)
    return {
        "active_clients": len(active), "total_clients": len(clients),
        "escalations_d5": escalations_d5, "escalations_d10": escalations_d10,
        "overdue_docs": overdue_docs, "avg_days_in_phase": round(avg_days, 1),
        "baseline_cycle_days": baseline_cycle, "projected_cycle_days": projected,
        "days_saved": baseline_cycle - projected,
    }


# ---------------- Email Settings & Logs ----------------
class EmailSettingsUpdate(BaseModel):
    from_email: Optional[str] = None
    sender_name: Optional[str] = None
    recipients: Optional[dict] = None


@api.get("/settings/email")
async def get_email_settings(user: dict = Depends(current_user)):
    return await _get_settings()


@api.put("/settings/email")
async def update_email_settings(req: EmailSettingsUpdate, user: dict = Depends(current_user)):
    if user.get("role") != "PM Manager":
        raise HTTPException(403, "Admin only")
    s = await _get_settings()
    if req.from_email is not None:
        s["from_email"] = req.from_email
    if req.sender_name is not None:
        s["sender_name"] = req.sender_name
    if req.recipients is not None:
        s["recipients"] = {**s.get("recipients", {}), **req.recipients}
    s["updated_at"] = iso(datetime.now(timezone.utc))
    await db.email_settings.update_one({"id": "global"}, {"$set": s}, upsert=True)
    s.pop("_id", None)
    return s


@api.get("/email-logs")
async def list_email_logs(client_id: Optional[str] = None, limit: int = 200,
                         user: dict = Depends(current_user)):
    q = {}
    if client_id:
        q["client_id"] = client_id
    logs = await db.email_logs.find(q, {"_id": 0}).sort("sent_at", -1).to_list(limit)
    return logs


class ClientContactUpdate(BaseModel):
    primary_contact_email: str


@api.patch("/clients/{client_id}/contact")
async def update_client_contact(client_id: str, req: ClientContactUpdate,
                                user: dict = Depends(current_user)):
    res = await db.clients.update_one(
        {"id": client_id},
        {"$set": {"primary_contact_email": req.primary_contact_email}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Client not found")
    return {"ok": True, "primary_contact_email": req.primary_contact_email}


@api.get("/")
async def root():
    return {"status": "ok", "app": "G&A Onboarding POC"}


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    if await db.users.count_documents({}) == 0:
        logger.info("DB empty — running seed.")
        try:
            from seed import seed as run_seed
            await run_seed()
        except Exception as e:
            logger.error(f"Seed failed: {e}")


@app.on_event("shutdown")
async def shutdown():
    mongo_client.close()
