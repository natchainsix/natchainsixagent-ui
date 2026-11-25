# ReguLens 4.0 | NatWest hackathon | NatChain Six

## Quick start in VS Code (Windows PowerShell)
Open the extracted `regulens-autonomous` folder (the folder containing app.py).

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
.\start.ps1
```
If script execution is blocked, use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` or run the direct command below. No activation is necessary when using the virtual environment executable.

### Start / stop / restart
Start: ` .\start.ps1 ` or ` .\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1 `.
Open http://127.0.0.1:8000. Keep the terminal open.
Stop: press Ctrl+C in the running server terminal and wait for shutdown. `stop.ps1` prints these instructions; it deliberately does not kill processes. Restart by running the start command again. Restart after .env edits. Use one worker and avoid reload mode during the demo.

### macOS/Linux
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```
Stop with Ctrl+C.

### Environment configuration
Demo: retain MODE=demo and POLL_SECONDS=15; no keys required. Expect three sources and seven synthetic risk signals after initial review.
Live: edit .env using the following template (replace placeholders; leave unused providers blank).
```dotenv
MODE=github
POLL_SECONDS=15
GITHUB_ORG=your-org
GITHUB_TOKEN=
GITLAB_GROUP=your-group
GITLAB_TOKEN=
DOCS_DIR=C:/Projects/compliance-documents
DOCS_OWNER=Compliance Engineering
GITHUB_WEBHOOK_SECRET=
AGENTS_JSON={"supervisor":{"url":"http://127.0.0.1:8001/review/invoke","mode":"langserve","token_env":"AGENT_TOKEN"}}
AGENT_TOKEN=
ALERT_WEBHOOK_URL=
ALERT_TOKEN=
```
Start your own agent service separately before starting live reviews. The package does not contain that service. For plain REST change mode to direct and use its actual URL. Keep secrets out of Git. Configure an approved notification relay to send emails; relay acceptance is not delivery confirmation.

### Verify and troubleshoot
- Open http://127.0.0.1:8000/api/status for worker state and findings. API documentation routes are disabled.
- Select a source and open the assistant; enter `rescan`, `verify fix`, or `status`.
- For missing modules, reinstall requirements using the virtual environment Python.
- For port conflicts, stop the other server or use --port 8080 in the direct start command.
- For provider 401/403 errors, verify approved token permissions and group/organisation names.
- For agent errors, verify service availability and request/response contract below.
- To reset demo history only, stop the server, back up state.sqlite3, then delete it. This deletes saved findings and alerts; never do this casually in live mode.

Local autonomous regulatory assurance prototype. Python 3.10+.

## Run
`python -m venv .venv`, activate it, then `pip install -r requirements.txt`.
Copy `.env.example` to `.env`. Run `uvicorn app:app --host 127.0.0.1 --port 8000` with one worker. Open http://127.0.0.1:8000. Demo starts automatically, scans the two bundled repositories and lending documentation, and detects edits on the next poll. No keys needed. Demo rules are synthetic and not a regulatory engine. No findings is not a pass certification.

## GitHub discovery
Set MODE=github, GITHUB_ORG=your-org and GITHUB_TOKEN to an approved read-only token with access to the intended repositories. Discovery enumerates visible non-archived organisation repositories; reviews default-branch commits only. Polling detects new repos and changed commits. Optional signed GitHub webhook POST /webhooks/github wakes the worker; configure GITHUB_WEBHOOK_SECRET and a trusted HTTPS gateway. Pull request events wake discovery but do not scan unmerged PR heads. No code is executed. Review scope is eligible text files, up to 40 files/200 KB, 40 KB per file; skipped eligible files are shown. Other file types are outside scope. No automatic GitHub writes.

## Agent integration
Set AGENTS_JSON in .env, for example:
```
AGENTS_JSON='{"regulatory":{"url":"http://127.0.0.1:8001/review/invoke","mode":"langserve","token_env":"AGENT_TOKEN"},"privacy":{"url":"http://127.0.0.1:8002/review","mode":"direct"}}'
```
Each agent receives repository, commit, files [{path,content}], instructions and specialty. LangServe wraps the payload in {input: ...} and unwraps output. Direct mode sends/receives plain JSON. Agents run concurrently per repository; any failure leaves previous results intact and retries next cycle. Example response:
```
{"findings":[{"status":"NON_COMPLIANT","severity":"HIGH","title":"Excessive storage","file":"customer_service.py","line_start":3,"regulation":"UK GDPR Art. 5(1)(c)","evidence":"db.append(dict(customer))","remediation":"Store application ID and status only."}]}
```
Allowed status: NON_COMPLIANT, COMPLIANT_WITHIN_SCOPE, NEEDS_CONTEXT. Severity: CRITICAL, HIGH, MEDIUM, LOW. The adapter checks schema and line locations, not legal accuracy. Use your LangGraph supervisor endpoint or separate specialist endpoints. Configure actual regulatory retrieval, citations and LangSmith on the agent side. Changing agent configuration does not rescan unchanged commits; stop app and remove state.sqlite3 to reset demo (deletes history).

## Alerts
Set ALERT_WEBHOOK_URL to an approved internal notification relay (HTTPS recommended) and ALERT_TOKEN if needed. Every NON_COMPLIANT finding queues a persistent alert with repository, commit, finding, regulation and suggested fix. Raw evidence field is omitted, but agent-generated text may still contain sensitive information: use an approved internal relay and DLP. Relay should map repo to authorised recipients and deliver email/Teams/Slack. No direct email delivery is implemented. HTTP failures retry next cycle; receiver must deduplicate Idempotency-Key. New commit creates new alerts; dashboard shows latest successful scan, not a verified remediation lifecycle. Pending count includes historical undelivered alerts. Dashboard polls every second.

## Security and limitations
Local single-user prototype only: bind to loopback; dashboard has no authentication or tenant isolation. Before deployment add SSO/RBAC, TLS gateway, secrets vault, approved source-code egress/DLP, dependency lock and vulnerability scanning, durable distributed queue, retention policy, monitoring, rate limits, and independent security testing. Basic secret screening is incomplete. Do not connect real bank repositories without approval. Agent output is untrusted and displayed as text. No claim of vulnerability-free code or legal certification. Default branch scanning is near-real-time, not instantaneous; sequential repository processing and API limits affect latency. Demo repos intentionally include unsafe patterns and synthetic data. SQLite stores latest scans and alert outbox; not a full audit history. No actual security scanner or regulatory corpus included.

## 2.1 Command centre upgrade
The UI polls `/api/status` every second for actual worker phases, repository progress, specialist endpoint states and a bounded in-memory activity stream. Animations stop when idle or disconnected. Fast demo checks may complete between polls; activity history still records their transitions. Reduced-motion preferences are supported. Existing agent and alert environment settings are unchanged. Run a single Uvicorn worker. API docs are disabled in this package. The activity stream resets on restart; findings persist. No production authentication or independent vulnerability audit is included.

## Version 3 — multi-source command centre
Run the same local Uvicorn command (single worker, loopback only). Demo inventory labels SafeLend as GitHub, QuickLend as GitLab, and lending-docs as Documentation; these are local fixtures, not live hosted repositories. Seven expected synthetic risk signals. The supplied NatWest logo is used for the hackathon prototype.

Live mode: set MODE=github, configure GITHUB_ORG/GITHUB_TOKEN and/or GITLAB_GROUP/GITLAB_TOKEN (gitlab.com, read_api/read_repository as appropriate), and/or DOCS_DIR (approved local folder of text documents) and DOCS_OWNER. Configure AGENTS_JSON as above. GitLab and documents use scheduled discovery; GitHub webhook wakes the worker. Default branches only; no PDF/DOCX extraction. Owners are provider namespaces, not verified individual maintainers. Live email addresses are not inferred; configure routing in your approved relay. Relay acceptance is not proof of email delivery.

Select a source, expand findings, or open AI assistant and enter `rescan`, `verify fix`, or `status`. The assistant is a command interface, not an LLM chat service. Rescan forces review even if revision unchanged, and reports actual job results. Previous signals not redetected are not certified fixed. Jobs are in-memory; findings and alerts persist. Live integration and browser visual testing not performed. Do not expose this unauthenticated local prototype publicly; use approved authentication, access control, DLP and security review before production. No guarantee of vulnerability-free operation.

## V4 — AI Judge / scan evaluation
Open the selected source and click **Show evaluation for selected source**. Scores are saved with the scan revision. If upgrading an existing database, force a rescan using the assistant. The benchmark labels six lending control files in each demo repo and one policy file in the docs repo. Accuracy measures correct file-level risk classification, precision measures correct flagged files, recall measures detected expected risky files, and F1 is their harmonic mean. Undefined denominators display N/A. Unexpected flagged files count as false positives; multiple findings in a file count once. The literal evidence match checks whether the supplied evidence appears on the stated source line. No legal citation or remediation correctness is inferred. Modified snapshots, skipped files and unknown live repos have no benchmark score. Demo results are not independent AI validation: the fixture scanner is designed for these examples. For genuine testing configure external agents and use a separate held-out, human-labelled dataset in LangSmith. See https://docs.langchain.com/langsmith/evaluation-types.

### macOS daily start / stop
In VS Code open the folder containing app.py. Run `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements.txt`, then `sh start.sh`. Visit http://127.0.0.1:8000. Stop with Ctrl+C in the server terminal and restart with `sh start.sh`. No virtual environment activation required. Windows scripts remain unchanged. Do not copy a virtual environment between operating systems.
