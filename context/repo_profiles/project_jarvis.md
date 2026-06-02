# Project Jarvis Repo Profile

Project Jarvis is the FastAPI backend for Jarvis server-side logic, APIs, data handling, integrations, and orchestration.

Current architecture direction:

- AI logic should live in `app/services_refactor`.
- `ChatService` owns orchestration.
- Jarvis Brain, routing, memory, prompt handling, and model execution should follow service_refactor ownership.
- Routers should stay thin and focus on transport concerns.
- Review changes for service ownership, canonical backend contracts, safety, and roadmap alignment.
