# G&A Partners Onboarding POC — PRD

## Original Problem Statement
Full-stack POC for G&A Partners' Onboarding division demonstrating 6 core capabilities that compress onboarding cycle by 10–14 days. Simulates ClientSpace, PrismHR, and WorkSight integrations. Target users: Onboarding Project Managers (OBPMs) and Onboarding Payroll Analysts (OBPAs).

## User Choices Locked
- Stack: React + FastAPI + MongoDB (equivalent to Replit Node/Postgres spec)
- AI: Hybrid — Claude Sonnet 4.5 (Emergent LLM key) for CSA extraction + status reports + auto-drafts; templates for routing
- Auth: Simple username/password with seeded mock users
- Seed Data: 3-5 sample clients across phases
- CSA: "Simulate CSA Upload" button with sample JSON

## Architecture
- backend/server.py — FastAPI routes (auth, clients, phases, docs, CSA, preflight, comms)
- backend/ai_service.py — Claude Sonnet 4.5 via emergentintegrations
- backend/seed.py — sample clients/team/docs
- frontend: React + Shadcn UI + Tailwind, Chivo + IBM Plex Sans + JetBrains Mono

## What's Implemented (v1 — Feb 2026)
- All 6 capabilities (data collection escalation, PM workflows w/ auto-propagation, AI communications, CSA extraction, ClientSpace-WorkSight sim, preflight)
- Master Dashboard, Client Detail (tabs), Login
- Simulate Day, Sync to ClientSpace, Run Pre-Flight, Generate Status Report buttons
- Seeded users (admin/admin), 4 sample clients across phases

## Personas
- OBPM: oversees onboarding lifecycle, validates docs, marks phase complete
- OBPA: runs preflight, mock payroll validation
- PM Manager / Dept Heads: receive escalations

## Backlog (P1/P2)
- P1: Real PDF parsing for CSA (currently sample JSON)
- P1: Multi-user role-based dashboards
- P2: Email integration for real escalation sends
- P2: GuideCX comparison/migration history view
