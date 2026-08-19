"""Seed mock users, clients, team members, documents and CSA fixtures."""
import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import bcrypt

load_dotenv(Path(__file__).parent / ".env")

mongo = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = mongo[os.environ["DB_NAME"]]


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def iso(dt: datetime) -> str:
    return dt.isoformat()


PHASES = ["Kickoff", "Data Collection", "Configuration", "Testing", "First Payroll"]
DOC_TYPES = ["Census", "Codes", "Payroll Report", "Reverse Wire", "Handbook", "WTPA"]

USERS = [
    {"username": "admin", "password": "admin123", "role": "PM Manager", "name": "Alex Rivera"},
    {"username": "pm.jane", "password": "pm123", "role": "PM", "name": "Jane Hill"},
    {"username": "pa.mike", "password": "pa123", "role": "PA", "name": "Mike Chen"},
]

TEAM = [
    {"name": "Jane Hill", "role": "PM"},
    {"name": "Mike Chen", "role": "PA"},
    {"name": "Alex Rivera", "role": "PM Manager"},
    {"name": "Dana Park", "role": "PA Manager"},
    {"name": "Sam Cole", "role": "Benefits"},
    {"name": "Riley Stone", "role": "401k"},
    {"name": "Pat Lee", "role": "Tax"},
    {"name": "Jordan Vega", "role": "Risk"},
    {"name": "Taylor Brooks", "role": "Payroll Specialist"},
    {"name": "Casey Morgan", "role": "Payroll Specialist"},
]

# Sample CSA extraction payload — would normally come from PDF parsing
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

# Handoff data that will mismatch CSA in 2 places for discrepancy demo
SAMPLE_HANDOFF = {
    "fee_structure": {"per_employee_per_month": 95.00, "setup_fee": 2500.00},
    "term": "24 months",
    "included_services": ["Payroll", "Benefits Admin", "HR Advisory", "Tax Filing"],
    "optional_services": ["Risk Management"],  # missing 401k
    "suta_coverage": {"state": "TX", "rate": 0.031, "type": "standard"},  # rate mismatch
    "billing_exceptions": ["Off-cycle payrolls billed separately at $250/run"],
    "tax_classification": "PEO Co-employment",
    "cyber_policy": {"carrier": "Hartford", "limit": 5_000_000},
    "ownership": "ABC Holdings LLC — 100%",
}


def make_payroll_config(employee_count=10, missing_wc=False, missing_suta_state=None):
    wc_codes = {}
    for i in range(1, employee_count + 1):
        if missing_wc and i == 7:
            wc_codes[f"EMP-{i:03d}"] = None  # missing → preflight fail
        else:
            wc_codes[f"EMP-{i:03d}"] = "8810"
    suta_rates = {"TX": 0.027, "CA": 0.034, "NY": 0.029}
    if missing_suta_state:
        suta_rates.pop(missing_suta_state, None)
    return {
        "wc_codes": wc_codes,
        "suta_rates": suta_rates,
        "billing_rules": {"mapped": True, "fee_match": True},
        "pay_groups": {"weekly": ["TX", "CA"], "biweekly": ["NY"]},
        "deduction_codes": {"401K": "valid", "MEDICAL": "valid", "DENTAL": "valid"},
    }


CLIENTS_SEED = [
    {
        "name": "Acme Industrial Co.",
        "status": "Data Collection",
        "current_phase": "Data Collection",
        "days_in_phase": 6,  # triggers Day 5 escalation
        "csa_uploaded": False,
        "missing_docs": ["WTPA"],
        "payroll_cfg_args": {},
    },
    {
        "name": "Beacon Health Services",
        "status": "Configuration",
        "current_phase": "Configuration",
        "days_in_phase": 2,
        "csa_uploaded": True,
        "missing_docs": [],
        "payroll_cfg_args": {"missing_wc": True},
    },
    {
        "name": "Crestline Logistics",
        "status": "Testing",
        "current_phase": "Testing",
        "days_in_phase": 3,
        "csa_uploaded": True,
        "missing_docs": [],
        "payroll_cfg_args": {"missing_suta_state": "NY"},
    },
    {
        "name": "Delta Manufacturing",
        "status": "First Payroll",
        "current_phase": "First Payroll",
        "days_in_phase": 1,
        "csa_uploaded": True,
        "missing_docs": [],
        "payroll_cfg_args": {},
    },
    {
        "name": "Evergreen Retail Group",
        "status": "Data Collection",
        "current_phase": "Data Collection",
        "days_in_phase": 11,  # Day 10 escalation
        "csa_uploaded": False,
        "missing_docs": ["Census", "Codes", "Payroll Report"],
        "payroll_cfg_args": {},
    },
]


async def reset_collections():
    for coll in ["users", "clients", "team_members", "documents", "phases",
                 "payroll_configs", "csa_extractions", "communications",
                 "notifications", "clientspace_records", "prismhr_records",
                 "worksight_projects", "email_settings", "email_logs"]:
        await db[coll].delete_many({})


async def seed():
    await reset_collections()

    # users
    for u in USERS:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "username": u["username"],
            "password_hash": hash_pw(u["password"]),
            "role": u["role"],
            "name": u["name"],
        })

    # team
    team_ids = {}
    for t in TEAM:
        tid = str(uuid.uuid4())
        team_ids[t["name"]] = tid
        await db.team_members.insert_one({
            "id": tid, "name": t["name"], "role": t["role"], "assigned_clients": [],
        })

    now = datetime.now(timezone.utc)

    for c in CLIENTS_SEED:
        cid = str(uuid.uuid4())
        phase_order = PHASES.index(c["current_phase"])
        slug = c["name"].lower().replace(" ", "").replace(".", "").replace(",", "")
        client_doc = {
            "id": cid,
            "name": c["name"],
            "status": c["status"],
            "current_phase": c["current_phase"],
            "days_in_phase": c["days_in_phase"],
            "csa_uploaded": c["csa_uploaded"],
            "handoff_data": SAMPLE_HANDOFF,
            "clientspace_id": f"CS-{uuid.uuid4().hex[:8].upper()}",
            "prismhr_id": f"PHR-{uuid.uuid4().hex[:8].upper()}",
            "created_at": iso(now - timedelta(days=15)),
            "updated_at": iso(now),
            "pm_id": team_ids["Jane Hill"],
            "pa_id": team_ids["Mike Chen"],
            "phase_started_at": iso(now - timedelta(days=c["days_in_phase"])),
            "primary_contact_email": f"hr.lead@{slug}.com",
        }
        await db.clients.insert_one(client_doc)

        # phases
        for i, p in enumerate(PHASES):
            if i < phase_order:
                status, started, completed, note = "Complete", iso(now - timedelta(days=20-i*3)), iso(now - timedelta(days=18-i*3)), f"{p} completed on schedule. Hand-off verified."
            elif i == phase_order:
                status, started, completed, note = "In Progress", iso(now - timedelta(days=c["days_in_phase"])), None, ""
            else:
                status, started, completed, note = "Not Started", None, None, ""
            await db.phases.insert_one({
                "id": str(uuid.uuid4()), "client_id": cid, "phase_name": p,
                "status": status, "started_at": started, "completed_at": completed,
                "progress_note": note,
            })

        # documents
        for dt in DOC_TYPES:
            doc_status = "Received" if dt not in c["missing_docs"] else "Pending"
            escalation = 0
            if doc_status == "Pending":
                if c["days_in_phase"] >= 10:
                    escalation = 2
                elif c["days_in_phase"] >= 5:
                    escalation = 1
            await db.documents.insert_one({
                "id": str(uuid.uuid4()), "client_id": cid, "name": f"{c['name']}_{dt}.pdf",
                "type": dt, "status": doc_status,
                "uploaded_at": iso(now - timedelta(days=2)) if doc_status == "Received" else None,
                "escalation_level": escalation,
            })

        # payroll config
        cfg = make_payroll_config(**c["payroll_cfg_args"])
        await db.payroll_configs.insert_one({
            "id": str(uuid.uuid4()), "client_id": cid, **cfg,
            "validation_status": "Pending", "validation_report": "",
        })

        # CSA extraction (only if uploaded)
        if c["csa_uploaded"]:
            await db.csa_extractions.insert_one({
                "id": str(uuid.uuid4()), "client_id": cid,
                "extracted_data": SAMPLE_CSA_EXTRACT, "discrepancies": [],
                "validation_status": "Pending", "reviewed_by": [], "narrative": "",
                "created_at": iso(now - timedelta(days=4)),
            })

        # clientspace & prismhr records
        await db.clientspace_records.insert_one({
            "id": str(uuid.uuid4()), "client_id": cid,
            "case_id": client_doc["clientspace_id"], "status": c["status"],
            "last_sync": iso(now - timedelta(hours=6)),
        })
        await db.prismhr_records.insert_one({
            "id": str(uuid.uuid4()), "client_id": cid,
            "phr_id": client_doc["prismhr_id"], "status": c["status"],
            "last_sync": iso(now - timedelta(hours=6)),
        })

        # worksight project (for first 3 clients past kickoff)
        if phase_order >= 1:
            await db.worksight_projects.insert_one({
                "id": str(uuid.uuid4()), "client_id": cid,
                "name": f"DataCollection - {c['name']}",
                "status": "Active" if phase_order == 1 else "Complete",
                "created_at": iso(now - timedelta(days=10)),
                "provisioned_admin": True,
            })

    print(f"Seeded {len(USERS)} users, {len(TEAM)} team, {len(CLIENTS_SEED)} clients.")


if __name__ == "__main__":
    asyncio.run(seed())
