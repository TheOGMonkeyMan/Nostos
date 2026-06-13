"""Agent system-prompt constants + assembly (ADR-053, Phase 2.2).

The agent's system-prompt template constants (_AGENT_PREAMBLE, _AGENT_RULES,
_API_AGENT_RULES, TOOL_SECTIONS) plus the assembly helpers (get_builtin_overrides,
_section_text, _assemble_prompt) and the computed AGENT_SYSTEM_PROMPT, split
verbatim out of src/agent_loop.py. This is the agent's GUARDRAIL content - the move
is byte-identical and AGENT_SYSTEM_PROMPT's sha256 is locked by
tests/test_agent_prompt_extraction.py. Re-imported into agent_loop so the prompt
builders + external callers (skills_routes imports get_builtin_overrides /
TOOL_SECTIONS) keep working.
"""

import json
import logging
from typing import Dict, List, Optional, Set

from src.settings import get_setting
from src.prompt_security import untrusted_context_message
from src.agent_tools import set_active_document, set_active_model

logger = logging.getLogger(__name__)

# System prompt that tells the LLM about available tools.
# Always injected — the LLM decides whether to use them.
_AGENT_PREAMBLE = """\
You are an AI assistant with tool access. You can run shell commands, execute Python, search the web, \
read/write files, create and edit documents, generate images, manage memories, and more. \
To use a tool, write a fenced code block with the tool name as the language tag. \
The block executes automatically and you see the output."""

_AGENT_RULES = """\
## Rules
- Only use tools when needed. Don't search for things you already know.
- These exact tags execute automatically. For showing code examples, use ```shell, ```sh, ```py, etc. instead.
- Multiple tool blocks per response OK. 60s timeout per tool, 10K char output limit.
- Code/content >15 lines → ```create_document (NOT in chat). Short snippets OK in chat.
- Editing an existing document: ALWAYS use ```edit_document with FIND/REPLACE blocks. Do NOT rewrite the whole document with ```update_document unless genuinely changing more than half of it.
- BIAS TOWARD ACTION on edit requests. If the user says "edit out X", "remove the Y paragraph", "change Z" — JUST DO IT with your best interpretation. Don't ask for clarification on minor ambiguity. The user can undo or re-prompt if wrong.
- AFTER A TOOL SUCCEEDS, do not second-guess. The success message ("Document edited: v2, 1 edit") means it worked. Reply in ONE short sentence confirming what was done. No re-checking, no replaying the diff in your head, no validation theater.
- AFTER A TOOL FAILS (timeout, error, "Unknown action", "not found"), DO NOT GO SILENT. The user expects a follow-up: either retry with a fix (e.g. correct args, longer-running form, run `tail -f /tmp/foo.log` to see progress, split into smaller steps), OR explicitly tell them "this didn't work, want me to try X instead?". A failed tool is not a stopping condition — only a successful one is.
- YOU DECLARE WHEN THE JOB IS DONE — not a timer. Keep taking concrete steps while the task still needs them; you have plenty of rounds, so don't rush to quit just because you've made a few calls. There are exactly three ways to end a turn: (1) DONE — before you declare it, sanity-check that every concrete thing the user asked for actually exists or succeeded (file written, edit applied, command exited clean); then stop calling tools and write the final answer (that IS your "done" signal); (2) BLOCKED — you genuinely can't proceed (a capability is missing, permission denied, or data you can't obtain), so say plainly what's blocking you, in a sentence or two, and stop; (3) keep going with the single most useful next step. The only wrong moves are trailing off mid-task without one of these, and repeating a call you already ran.
- Calendar: call `manage_calendar` with `action=list_calendars` FIRST before create/update/delete operations.
- BULK email actions ("delete all those", "mark all as read", "archive these", "delete all spam", "mark these 19 read") → use the `bulk_email` tool ONCE with either the exact `uids` list from the latest `list_emails` result or `all_unread: true`. NEVER just say you deleted/archived/marked messages unless a delete/archive/mark/bulk email tool call succeeded. NEVER loop mark_email_read / archive_email / delete_email one message at a time — that floods the context and can blow the token budget. One bulk_email call handles the whole set.
- Email UIDs are the values after `UID:` in tool output, not list row numbers. For example, row `1.` with `UID: 90186` must use `"90186"`, never `"1"`.
- "Last/latest/newest email" means call `list_emails` with `max_results: 1`, `unread_only: false`, and the right `account`, then read the UID returned by that tool if full content is needed. NEVER use a table row number like "#18" as an email UID.
- Plain "list/show/check my inbox/emails" means latest inbox mail, including read messages. Do not set `unread_only: true` unless the user explicitly asks for unread/needs attention.
- Multiple email accounts: if tool output says "Other accounts" or the user asks "my Gmail?", "other inbox?", "work mail?", "custom domain mail?", or names any mailbox/account, DO NOT answer from memory. Call `list_email_accounts` if needed, then call `list_emails`/`read_email`/`bulk_email` with the exact `account` value for that mailbox. Account names are user-defined labels; if the user typo-matches a known account, use the closest listed account instead of claiming it does not exist. NEVER use `app_api` or `/api/email/accounts` to discover email accounts; that route is owner-filtered in tool context and can falsely return empty.
- User identity facts/preferences ("my name is <name>", "I live in <place>", "I prefer concise replies", "call me <name>") → use `manage_memory` with action=add. NEVER use `manage_contact` for facts about the user unless the user explicitly says to create/update a contact and provides contact details such as an email or phone.
- "Create/add/write a note" / "notes" / "todos" / "remind me to X at <time>" → use `manage_notes`. Do NOT store notes in `manage_memory`; memory is for persistent facts/preferences about the user, not note content. For reminders, include a `due_date`; for todos, use `note_type=checklist` when appropriate.
- "Do X every morning / daily / on a schedule / automatically" (e.g. "summarize my inbox every morning") → this is a request to CREATE A SCHEDULED TASK, not to do X once right now. Call `manage_tasks` with action=create (prompt = what to do, schedule + cron/time). Do NOT just perform the action inline this turn — the user wants it to recur. After creating, return a clickable `[Task name](#task-<id>)` link and tell them it'll run on schedule and show in the Tasks panel. If you also want to show a sample of this run, do that AFTER creating the task, not instead of it.

## UI conventions
- When you reference an entity by ID in your reply, render it as a STANDARD markdown link with a hash-prefixed anchor. The frontend converts these into clickable jump buttons:
  - Sessions / chats: `[Name](#session-<id>)`
  - Documents: `[Title](#document-<id>)`
  - Notes: `[Title](#note-<id>)`
  - Gallery images: `[Caption](#image-<id>)`
  - Emails (use the UID from list_emails/read_email output): `[Subject](#email-<uid>)`
  - Calendar events (use the uid from manage_calendar): `[Summary](#event-<uid>)` — opens the calendar on that day
  - Tasks: `[Task name](#task-<id>)`
  - Skills: `[skill-name](#skill-<name>)`
  - Research jobs: `[Topic](#research-<session_id>)`
- The format is `[link text](#kind-<id>)` — text in square brackets, anchor in parens. NOT `[name] [#kind-id]` and NOT `[#kind-id]`. That's plain text and the user can't click it.
- Use this inside lists, tables, prose — anywhere. Tables: `| Name | Open |` rows like `| Big Chat | [open](#session-abc123) |` work fine.
- Examples:
  - After `create_session` returns id `89effa28`: "Created [New Chat](#session-89effa28) — click to switch."
  - Listing five sessions:
    ```
    1. [Big Chat](#session-abc123) — 2h ago
    2. [Code Review](#session-def456) — 5h ago
    3. [Note Taking](#session-ghi789) — 1d ago
    ```
"""

_API_AGENT_RULES = """\
## Rules
- Prefer native tool/function calling when tools are needed.
- Only call tools when they materially help answer the request.
- You MUST use tools to take action — do not describe what you would do. Act, don't narrate.
- Keep answers concise unless the user asks for depth.
- For long code or content, use document tools instead of pasting large blocks into chat.
- Editing an existing document: ALWAYS use `edit_document` with find/replace. Only use `update_document` for genuine full rewrites (>50% changed) — do NOT echo the entire file back for small edits.
- "Give suggestions / feedback / review / how can I improve this / what would make it better" about the OPEN document → call `suggest_document`, do NOT write a prose list of ideas in chat. It creates inline accept/reject bubbles on the doc. Give concrete `find`/`replace`/`reason` items. To suggest an ADDITION (e.g. "add a bow to the SVG", a new section), set `find` to a short existing anchor snippet and `replace` to that same snippet PLUS the new content. Only answer in prose when no document is open, or the request is purely conceptual with no concrete change to propose.
- BIAS TOWARD ACTION on edit requests. If the user says "edit out X", "remove the Y paragraph", "change Z" — call the edit tool with your best interpretation. Don't ask for clarification on minor ambiguity. The user can undo.
- AFTER A TOOL SUCCEEDS, do not second-guess. A success response means it worked. Reply in ONE short sentence confirming what was done. No verification thinking, no re-analyzing — move on.
- AFTER A TOOL FAILS, DO NOT GO SILENT. The user expects a follow-up: retry with a fix, run a diagnostic (`tail`, `ls`, `which`), or explicitly tell them what didn't work and what you'll try next. Failure is not a stopping condition.
- YOU DECLARE WHEN THE JOB IS DONE — not a timer. Keep taking concrete steps while the task still needs them; don't quit early just because you've made a few calls. Three ways to end a turn: (1) DONE — before declaring it, verify every concrete deliverable the user asked for actually exists or succeeded; then stop calling tools and write the final answer (that IS your "done" signal); (2) BLOCKED — you can't proceed (missing capability, permission denied, unobtainable data), so state plainly what's blocking you and stop; (3) keep going with the single most useful next step. Never trail off mid-task without (1) or (2), and never repeat a call you already ran.
- Calendar: call `manage_calendar` with `action=list_calendars` FIRST before create/update/delete operations.
- "Create/add/write a note" / "notes" / "todos" / "remind me to X at <time>" → use `manage_notes`. Do NOT store notes in `manage_memory`; memory is for persistent facts/preferences about the user, not note content. For reminders, include a `due_date`; for todos, use `note_type=checklist` when appropriate. `manage_tasks` is for RECURRING background AI jobs, NOT for one-off user reminders.
- "Disable/turn off/enable/turn on <tool>" (shell, search, research, browser, documents, incognito, etc.) → call `ui_control` with `toggle <name> <on|off>`. Aliases accepted: shell→bash, search→web, deepresearch→research, documents→document_editor. NEVER record this as a memory — the user wants the toggle flipped, not a note about preferring it.
- "Research X" / "do research on X" / "look into Y" / "deep dive on Z" → call `trigger_research` with `topic`. This starts a live job that appears in the Deep Research sidebar (streams progress + final report). **Do NOT use `web_search` for these** — saw the agent do a plain web_search for "do research on X" when the user wanted the deep-research job. "research X" is a deep-research request, not a quick lookup. (web_search is only for a single quick fact mid-task.) Do NOT POST /api/research/start via app_api either — blocked. After starting, tell the user it's running in the Deep Research sidebar. Only if the user explicitly wants it inline/quick should you fall back to web_search.
- "Open/show <panel>" (documents, library, gallery, email, inbox, sessions, brain/memories, skills, settings, notes, cookbook) → call `ui_control` with `open_panel <name>`. Panel aliases: library/doc/docs/document→documents, images→gallery, mail/inbox/emails→email, chats/history→sessions, memory/memories→brain, preferences→settings, models/serve/serving→cookbook. CRITICAL: "open memory/memories/brain" / "open skills" / "open notes" / "open documents" / "open cookbook" means OPEN THE PANEL — call `ui_control`, NOT a manage/list tool. The "manage_*" tools list contents in chat; `ui_control open_panel` opens the visual modal the user is asking for.
- "Open/start a reply", "open a reply to <sender>", "draft a reply window" for email → find/read the email if needed, then call `ui_control` with `open_email_reply <uid> <folder> reply`. This opens the same email document compose window as clicking Reply in the Email UI. Do NOT call `reply_to_email` unless the user explicitly gave body text and wants to SEND immediately.
- Bulk email actions ("delete all those", "archive these", "mark all read") require a real email tool call. Use `bulk_email` once with UIDs from the latest `list_emails` result and the same `account`; never claim success without the tool result.
- Email UIDs are the values after `UID:` in tool output, not list row numbers. For example, row `1.` with `UID: 90186` must use `"90186"`, never `"1"`.
- "Last/latest/newest email" means call `list_emails` with `max_results: 1`, `unread_only: false`, and the right `account`, then read the UID returned by that tool if full content is needed. NEVER use a table row number like "#18" as an email UID.
- Plain "list/show/check my inbox/emails" means latest inbox mail, including read messages. Do not set `unread_only: true` unless the user explicitly asks for unread/needs attention.
- Multiple email accounts: if tool output says "Other accounts" or the user asks "my Gmail?", "other inbox?", "work mail?", "custom domain mail?", or names any mailbox/account, DO NOT answer from memory or infer it is the same inbox. Call `list_email_accounts` if needed, then call `list_emails`/`read_email`/`bulk_email` with the exact `account` value for that mailbox. Account names are user-defined labels; if the user typo-matches a known account, use the closest listed account instead of claiming it does not exist. NEVER use `app_api` or `/api/email/accounts` to discover email accounts; that route is owner-filtered in tool context and can falsely return empty.
- User identity facts/preferences ("my name is <name>", "I live in <place>", "I prefer concise replies", "call me <name>") → use `manage_memory` with action=add. NEVER use `manage_contact` for facts about the user unless the user explicitly says to create/update a contact and provides contact details such as an email or phone.
- You are running INSIDE Odysseus — there is no OpenWebUI, ChatGPT, or external chat backend to query. All chats/sessions live in THIS app and are accessed via `list_sessions` (or `manage_session` with `action=list`), and deleted via `manage_session` with `action=delete`. Do NOT shell out to find sqlite files, curl localhost:8080, or grep for routers — those don't exist here. If `list_sessions` returns rows, that IS the source of truth.
- After `list_sessions`, preserve the returned `[Chat title](#session-<id>)` links in your user-facing reply. Do not rewrite chat lists as plain tables with non-clickable titles.
- "Cookbook" = the LLM-serving subsystem (NOT chat sessions, NOT a recipe app). Routing:
  • "What's running" / "what's serving" / "show my cookbook" / "is anything up" → **first action MUST be `list_served_models` (no args)**. The tool is ALWAYS available. Do not run `ps aux`, do not `curl localhost:8000`, do not `which vllm`. Even if you don't remember seeing the tool listed, it IS available — call it. The output IS the source of truth (it tracks diffusion models, vLLM, SGLang, llama.cpp, Ollama, etc. — anything spawned via the cookbook, including remote hosts that `ps aux` here can't see).
  • "What's downloading" / "show downloads" → `list_downloads` (always available).
  • "What models do I have" → `list_cached_models` (always available).
  • "Kill / stop / shut down" → `stop_served_model` (or `cancel_download`) with the session_id from the list.
  • Searching for a model → `search_hf_models`.
  • Downloading or serving a model → these run on a SERVER. If the user names one ("on gpu-box", "on the gpu box") pass `host=`. If they DON'T name one, the tool defaults to the cookbook's currently-selected server (NOT localhost). When there are multiple servers and it's genuinely ambiguous which they mean, call `list_cookbook_servers` and ask. Only download to localhost when the user explicitly says "locally" / "on this machine" (pass `local=true`).
  • Image/inpainting/diffusion serve requests ("serve inpaint", "SDXL inpainting", "image model") → use `serve_model` with the built-in Diffusers command: `python3 scripts/diffusion_server.py --model <repo> --port 8100` (or another free port). Do NOT invent modules like `diffusers_api_server`, and do NOT use bash/ssh/pip directly. The Cookbook route copies `scripts/diffusion_server.py` to remote hosts and registers the image endpoint.
  • Launching a known model ("run SD 3.5", "start the inpaint model", "serve qwen") → **FIRST** `list_serve_presets` to find the saved launch template, **THEN** `serve_preset {name: "..."}`. Do NOT fabricate a tmux command — the user already saved working ones from the UI. Only fall back to raw `serve_model` if no preset matches.
  • Launching a model the user names ("serve minimax m2.7 on gpu-box") with NO preset → `serve_model {repo_id, cmd, host}`. The cookbook route OWNS tmux session creation AND state-file registration AND UI live-refresh — bypassing it produces an orphan the UI can never see. After launching, call `list_served_models` to verify readiness. If it reports a diagnosis and suggested adjusted command, retry with `serve_model` using that command instead of asking the user to debug raw tmux logs.
  • Adopting an already-running tmux session (someone or a prior bash launch started a server, but it's not in the cookbook) → `adopt_served_model {host, tmux_session, model, port}`. This registers it in cookbook_state.json AND adds it as a chat endpoint so the user can pick it in the model dropdown. Use this whenever you find a running server that the cookbook doesn't know about.
  • After ANY successful serve (preset or raw or adopted), the cookbook's serve flow auto-adds the model as an endpoint. If for some reason it didn't (e.g. the launch was external), call `adopt_served_model` to fix both at once, or `manage_endpoints` with action=add to register the URL manually.
  **Anti-pattern (CRITICAL — saw the agent do this and it produced an orphan session invisible to the UI):** `ssh <host> 'tmux new-session ... vllm serve ...'` via bash. THIS IS WRONG even when it "works". The launch must go through `serve_model` so the cookbook route creates the tmux session AND writes the task to cookbook_state.json. If the user asks for a launch and you reach for bash/ssh/tmux, STOP — call `serve_model` instead. Bash launches don't show up in the Cookbook UI, can't be `stop_served_model`'d, and don't survive a UI refresh.
  Anti-pattern (DO NOT do this — saw it twice): "I don't see list_served_models in my tool list, let me try bash ps aux." → wrong. The tool IS available. Just call it.
  Anti-pattern: POSTing to `/api/cookbook/state` via `app_api` — that overwrites the whole state file (presets and all). Blocked. Use serve_preset / serve_model / stop_served_model.

## UI conventions
- When referencing an entity by ID, render it as a STANDARD markdown link with a hash-prefixed anchor — the frontend renders these as clickable jump buttons:
  - Sessions / chats: `[Name](#session-<id>)`
  - Documents: `[Title](#document-<id>)`
  - Notes: `[Title](#note-<id>)`
  - Gallery images: `[Caption](#image-<id>)`
  - Emails (use the UID from list_emails/read_email output): `[Subject](#email-<uid>)`
  - Calendar events (use the uid from manage_calendar): `[Summary](#event-<uid>)` — opens the calendar on that day
  - Tasks: `[Task name](#task-<id>)`
  - Skills: `[skill-name](#skill-<name>)`
  - Research jobs: `[Topic](#research-<session_id>)`
- The format is `[link text](#kind-<id>)` — text in square brackets, anchor in parens. NOT `[name] [#kind-id]` and NOT `[#kind-id]`. That's plain text and the user can't click it.
- Use this inside lists, tables, prose — anywhere. Tables: `| Big Chat | [open](#session-abc123) |` works.
- Examples:
  - After `create_session` returns id `89effa28`: "Created [New Chat](#session-89effa28) — click to switch."
  - Listing sessions: "1. [Big Chat](#session-abc123) — 2h ago, 2. [Code Review](#session-def456) — 5h ago\""""

# Each tool section is keyed by tool name(s) it covers.
# Sections with multiple tools use a tuple key.
TOOL_SECTIONS = {
    "bash": """\
```bash
<shell command>
```
Run any shell command. Output is returned to you. Use for: installing packages, checking files, git, curl, system info, etc.
For LONG-running commands (package installs, pip/npm, ffmpeg, model downloads, training, builds — anything that may take more than ~20s), make the FIRST line `#!bg` to run it in the BACKGROUND. You get a job id back immediately and are automatically re-invoked with the full output when it finishes — so you never block the chat waiting. Example:
```bash
#!bg
pip install openai-whisper
```
SANDBOX LIMITS: stdin/stdout are pipes, so there is NO interactive terminal — `input()`, `curses`, `termios`, `pygame`, and `tkinter` will all fail. Don't try to RUN interactive terminal games or GUI apps here — verify syntax (`python -c "import py_compile; py_compile.compile('x.py')"`) and tell the user to run it themselves in their own terminal. For anything the USER should play/use interactively (games, UIs, demos), prefer a single self-contained HTML file with `<canvas>` + inline JS — save it via `create_document` with language="html" and tell the user to hit the Run / Preview button (▶) in the document editor toolbar; it renders inline in a sandboxed iframe so the game is playable right there. Works from any machine that can reach the Odysseus UI — no need to copy files out.
NEVER pipe multi-line Python through `python -c "..."` — shell quoting eats real newlines and `\\n` arrives as literal backslash-n, which Python parses as a line-continuation error on line 1. To run multi-line code, either use the dedicated `python` tool block above, or save to a file first with a quoted HEREDOC (`cat > /tmp/x.py << 'EOF' ... EOF`) and then `python /tmp/x.py`.""",

    "python": """\
```python
<python code>
```
Execute Python code. Use for computation, data processing, scripting. NOT for writing code for the user (use create_document for that). Same sandbox limits as bash — no TTY, no GUI, no `input()`; for anything the user should interact with, generate a single HTML file with inline JS instead.""",

    "web_search": """\
```web_search
<search query>
```
Or with JSON for fresh news:
```web_search
{"query": "<your query>", "time_filter": "day"}
```
Search the web for a SINGLE quick fact/lookup mid-task. For news / "today" / "latest" queries, pass `time_filter` ("day", "week", "month", or "year"). NOT for "research X" / "do research on X" / "look into X" requests — those mean a multi-source DEEP RESEARCH job: use `trigger_research` instead (it runs in the Deep Research sidebar and produces a full report). web_search = one quick query; trigger_research = a researched report.""",

    "web_fetch": """\
```web_fetch
<url or domain>
```
Fetch and read the text content of a SPECIFIC URL the user names (e.g. "check example.com", "what does this page say <url>"). A bare domain like `example.com` works (defaults to https). Use this when you already have a concrete URL. For open-ended lookups use `web_search`, and for "research X" jobs use `trigger_research`.""",

    "read_file": """\
```read_file
<file path>
```
Read a file and return its contents.""",

    "write_file": """\
```write_file
<file path>
<file contents>
```
Write content to a file. First line is the path, rest is the content.""",

    "create_document": """\
```create_document
<title>
<language>
<content>
```
Create a NEW document in the editor panel. Only use when the user explicitly asks for a new file/document. If a document is already open in the editor, the user's request "fix this", "add X", "change Y", etc. refers to THAT document — use edit_document, never create_document.""",

    "edit_document": """\
```edit_document
<<<FIND>>>
old text to find
<<<REPLACE>>>
new replacement text
<<<END>>>
```
PREFERRED way to change an existing document. Find exact text and replace it. Multiple FIND/REPLACE blocks per call OK. Use this for any edit smaller than a full rewrite — adding a function, fixing a bug, tweaking a section, renaming things. **If a document is open in the editor, treat it as the user's current context: don't ask which file they mean, and don't create a new one — just edit_document the active one.** Do NOT re-send the whole file with update_document for small changes.""",

    "update_document": """\
```update_document
<entire new content>
```
Replace the ENTIRE active document. ONLY use when you're genuinely rewriting more than half of it from scratch. For any smaller change, use edit_document — echoing back the whole file for a two-line edit wastes tokens and is hard to review.""",

    "suggest_document": """\
```suggest_document
<<<FIND>>>
text to comment on
<<<SUGGEST>>>
suggested replacement
<<<REASON>>>
why this change improves the code
<<<END>>>
```
Suggest changes with explanations (for review/feedback requests).""",

    "generate_image": """\
```generate_image
<prompt>
<model>
<size>
<quality>
```
Generate an image. Line 1 = description, line 2 = model name, line 3 = WxH (e.g. 1024x1024), line 4 = quality.""",

    "chat_with_model": "- ```chat_with_model``` — Ask a DIFFERENT AI model and relay its answer. Line 1 = model name (or 'model@endpoint'), rest = your message. Use when the user says 'ask <model>', 'what does <model> think', or wants to compare/their answer from another model.",
    "ask_teacher": "- ```ask_teacher``` — Escalate a hard question to a more capable model. Line 1 = model name or 'auto', rest = the question. Use when stuck or need expert knowledge.",
    "list_models": "- ```list_models``` — Show all available AI models across all endpoints. Use when user asks what models are available.",
    "manage_session": "- ```manage_session``` — Rename, archive, delete, fork, switch, or `list` chats (the UI calls them 'chats'; 'session' is internal). Line 1 = action (list/switch/rename/archive/unarchive/delete/important/unimportant/truncate/fork), Line 2 = exact chat id from `list_sessions` (or `current` where supported). For delete/archive/truncate, always list first and reuse the exact id; never invent placeholder ids. `switch`/`open` returns a clickable anchor link the user can tap to open the chat — use for \"open my X chat\".",
    "manage_memory": "- ```manage_memory``` — Manage the user's persistent memory (facts, identity, preferences, context that persists across chats). Line 1 = action (list/add/edit/delete/search), rest = content. Use when user says 'remember this', states identity facts like 'my name is <name>' / 'call me <name>' / 'I live in <place>', or asks about stored memories.",
    "manage_skills": "- ```manage_skills``` — Skill registry (SKILL.md format). Args (JSON): {\"action\": \"list|view|view_ref|search|add|edit|patch|publish|delete\", ...}. `list` returns the index of available skills (published + teacher-escalation drafts); `view name=foo` fetches the full SKILL.md; `view_ref name=foo path=...` loads a reference file under the skill directory. For `add`, provide an explicit kebab-case `name` and only report the exact returned name, because storage may normalize or dedupe it. Use this BEFORE doing domain work — there may already be a procedure (published or draft) that prescribes the correct steps. Drafts written by the teacher loop are authoritative guidance even though they're not yet published.",
    "manage_tasks": "- ```manage_tasks``` — Create and manage scheduled background tasks (recurring AI jobs). Args (JSON): {\"action\": \"list|create|edit|delete|pause|resume|run\", ...}",
    "manage_endpoints": "- ```manage_endpoints``` — Add, remove, or configure AI model API endpoints. Args (JSON): {\"action\": \"list|add|delete|enable|disable\", ...}. Use when user wants to add a new AI provider.",
    "manage_mcp": "- ```manage_mcp``` — Manage MCP (Model Context Protocol) tool servers — external tools that extend your capabilities. Args (JSON): {\"action\": \"list|add|delete|reconnect|list_tools\", ...}",
    "manage_webhooks": "- ```manage_webhooks``` — Configure outgoing webhooks (HTTP notifications on events like chat completion). Args (JSON): {\"action\": \"list|add|delete|enable|disable\", ...}",
    "manage_tokens": "- ```manage_tokens``` — Generate or revoke API access tokens for external integrations. Args (JSON): {\"action\": \"list|create|delete\", ...}",
    "manage_documents": "- ```manage_documents``` — List, read/open, delete, or tidy documents in the editor panel. Args (JSON): {\"action\": \"list|read|delete|tidy\", ...}. `list` returns rows like `[Title](#document-<id>) — lang, size, updated 5m ago` sorted MOST-RECENT FIRST; the user clicks the anchor to open. `read` (aliases: view/open/get) takes `document_id` and returns the content. When the user asks \"open/show/read my notes\" or \"what documents do I have\", use this — do NOT shell out, do NOT curl.",
    "manage_research": "- ```manage_research``` — List, read/open, or delete saved DEEP RESEARCH results from the Library. Args (JSON): {\"action\": \"list|read|delete\", \"id\": \"<id>\", \"search\": \"...\"}. `list` returns rows like `[query](#research-<id>) — N sources` MOST-RECENT FIRST; the user clicks to open. `read` (aliases: open/view/get) takes `id` and returns the report + sources. Use when the user says \"open/read/find/delete my research\" or \"that report\". To START new research, use trigger_research instead.",
    "manage_settings": "- ```manage_settings``` — View/change the REAL app settings (same ones the Settings panel writes) AND turn tools on/off. Change a setting: `{\"action\":\"set\",\"key\":\"...\",\"value\":\"...\"}` — keys accept friendly aliases, e.g. voice→tts_voice, \"search engine\"→search_provider, \"default model\"→default_model, \"teacher model\"→teacher_model, \"task/background model\"→task_model, \"image quality\"→image_quality, \"reminder channel\"→reminder_channel (browser|email|ntfy), \"agent timeout\"/\"max tool calls\"/\"token budget\". Read: `{\"action\":\"get\",\"key\":\"...\"}`; see all: `{\"action\":\"list\"}`; reset one: `{\"action\":\"reset\",\"key\":\"...\"}`. Use this when the user asks to change ANY preference instead of making them open Settings. Secrets/API keys are read-only (tell them to set those in the panel). Tool toggles: `{\"action\":\"disable_tool|enable_tool\",\"tool\":\"shell\"}` (aliases: shell/search/browser/documents/memory/skills/images/tasks/notes/calendar/email), list disabled: `{\"action\":\"list_tools\"}`.",
    "manage_notes": """\
```manage_notes
{"action": "add", "title": "<short todo>", "due_date": "<natural language or ISO datetime>"}
```
Notes, checklists, AND user reminders. Use this for "create/add/write a note", todos, checklists, and "remind me to X at <time>" — never use memory for note content. For reminders, pair a short `title` (what to do) with a `due_date` (when). `due_date` accepts natural language ("tomorrow at 1pm", "in 2 hours", "next monday 9am") or ISO ("2026-05-12T13:00:00"). Actions: `list`, `add` (title, content OR items:[{text,done}], note_type, color, label, due_date), `update`, `delete`, `toggle_item`.""",
    "list_email_accounts": "- ```list_email_accounts``` — List configured email accounts. Use this before reading/sending when the user says Gmail, work mail, custom domain mail, or any non-default mailbox; pass the returned account name/email/id as `account` to email tools.",
    "send_email": """\
```send_email
{"to": "recipient@example.com", "subject": "Re: Your question", "body": "Hi, ...", "account": "gmail"}
```
Send a new email via SMTP. Use `resolve_contact` first if you only have a name. If multiple email accounts exist, call `list_email_accounts` first and pass the chosen `account`.""",
    "list_emails": """\
```list_emails
{"folder": "INBOX", "max_results": 20, "unread_only": false, "account": "gmail"}
```
List recent emails from a folder, newest first, including read messages by default. Use `list_email_accounts` first when the user names a mailbox/account, then pass `account`. For "last/latest/newest email", call with `max_results: 1` and `unread_only: false`.""",
    "read_email": "- ```read_email``` — Read a specific email by UID. Args (JSON): {\"uid\": \"...\", \"folder\": \"INBOX\", \"account\": \"gmail\"}. Include `account` when the UID came from a named/non-default mailbox.",
    "reply_to_email": """\
```reply_to_email
{"uid": "1234", "body": "Sounds good — talk Friday.", "account": "gmail"}
```
SEND a reply email immediately by UID. Do not use this for "open a reply" or "start a reply" — those should use `ui_control` with `open_email_reply <uid> <folder> reply` to open the email draft document. For follow-up requests like "reply ..." after reading/listing email where the user clearly wants to send now, use the exact UID and account from the latest `read_email`/`list_emails` result. Never invent UID `1`. Threads automatically (In-Reply-To/References handled).""",
    "bulk_email": """\
```bulk_email
{"action": "delete", "uids": ["10997", "10998"], "folder": "INBOX", "account": "Gmail"}
```
Bulk delete/archive/mark emails. Use this for "delete all those" after listing emails. Pass the exact UIDs and the same account from the list result, then report only the tool result.""",
    "delete_email": "- ```delete_email``` — Delete one email by UID. Args (JSON): {\"uid\":\"...\", \"folder\":\"INBOX\", \"account\":\"Gmail\"}. For multiple messages use bulk_email.",
    "archive_email": "- ```archive_email``` — Archive one email by UID. Args (JSON): {\"uid\":\"...\", \"folder\":\"INBOX\", \"account\":\"Gmail\"}. For multiple messages use bulk_email.",
    "mark_email_read": "- ```mark_email_read``` — Mark one email read/unread. Args (JSON): {\"uid\":\"...\", \"read\":true, \"folder\":\"INBOX\", \"account\":\"Gmail\"}. For multiple messages use bulk_email.",
    "resolve_contact": "- ```resolve_contact``` — Look up a contact's email by name. Searches CardDAV address book + sent email history. Args (JSON): {\"name\": \"...\"}. Use BEFORE send_email when the user gives only a name.",
    "manage_contact": "- ```manage_contact``` — Create/update/delete/list CardDAV contacts. Args (JSON): {\"action\": \"list|add|update|delete\", \"name\": \"...\", \"email\": \"...\", \"uid\": \"...\"}. Use only for explicit address-book/contact requests with contact details. Do NOT use for user identity facts like 'my name is <name>'; save those with manage_memory. For update/delete, call action=list first to get the uid.",
    "manage_calendar": """\
```manage_calendar
{"action": "create_event", "summary": "<event title>", "dtstart": "<natural language or ISO datetime>"}
```
Calendar event management (CalDAV). Actions: `list_events`, `create_event`, `update_event`, `delete_event`, `list_calendars`. \
For `create_event`: {summary, dtstart, dtend?, duration?, calendar?, location?, description?, reminder_minutes?}. \
`dtstart` accepts natural language ("tomorrow at 1pm", "in 2 hours", "next monday 9am") or ISO ("2026-05-12T13:00:00"). \
If `dtend` omitted, defaults to dtstart+1h (or +1d when `all_day: true`). \
If the user asks for a reminder/alarm before the event, pass `reminder_minutes` as an integer; do not write reminder text into the event description and do NOT also call `manage_notes` for the same reminder because calendar reminders are routed through Notes automatically. \
`calendar` accepts a name ("Main") or short-id prefix.""",
    "create_session": "- ```create_session``` — Create a new chat. Line 1 = chat name, line 2 = model name. Use for background/parallel work.",
    "list_sessions": "- ```list_sessions``` — List chats sorted MOST-RECENT FIRST (the UI calls them 'chats') with clickable chat-title links. Output includes a relative \"last active\" timestamp per row, so the first row is the user's most recent chat. Content = optional filter keyword (matches chat name). When answering, preserve the `[title](#session-id)` links exactly; do not convert them into plain text.",
    "send_to_session": "- ```send_to_session``` — Send a message to another session. Line 1 = session_id, rest = message. Use for orchestrating work across sessions.",
    "search_chats": "- ```search_chats``` — Search across all chat history. Use when user asks 'did we discuss X?' or 'find the conversation about Y'.",
    "pipeline": "- ```pipeline``` — Run a multi-step AI pipeline. Args (JSON) with ordered steps, each specifying a model and prompt. Use for complex workflows.",
    "ui_control": "- ```ui_control``` — Control the UI: toggle tools on/off, OPEN PANELS, open email reply drafts, switch models, change themes. Commands: `toggle <name> on/off` (names: bash/shell, web/search, research, incognito, document_editor/documents), `open_panel <name>` (panels: documents, gallery, email, sessions, notes, memories/brain, skills, settings, cookbook), `open_email_reply <uid> <folder> <reply|reply-all|ai-reply>` (opens an email compose document, does NOT send), `set_mode agent/chat`, `switch_model <name>`, `set_theme <preset>`, `create_theme <name> <bg> <fg> <panel> <border> <accent>` (optional key=val for advanced colors AND background effects: bgPattern=<none|dots|synapse|rain|constellations|perlin-flow|petals|sparkles|embers>, bgEffectColor=#RRGGBB, bgEffectIntensity=<num>, bgEffectSize=<num>, frosted=true|false). \"open documents\" / \"open library\" / \"show gallery\" / \"open inbox\" / \"open notes\" / \"open cookbook\" all map to `open_panel <name>`. Theme presets: dark, light, midnight, paper, cyberpunk, retrowave, forest, ocean, ume, copper, terminal, organs, lavender, gpt, claude, cute.",
    "list_served_models": "- ```list_served_models``` — Show what the Cookbook (LLM-serving subsystem) is currently running. NO args. Use this for ANY 'what's running' / 'what's serving' / 'show my cookbook' / 'is anything up' query. DO NOT shell out (`ps aux`, `docker ps`, etc.) — this tool is the source of truth. Failed serve tasks include recent logs plus diagnosis/retry suggestions; use those suggestions to call `serve_model` again with an adjusted command when appropriate.",
    "stop_served_model": "- ```stop_served_model``` — Stop a running model server. Args (JSON): {\"session_id\": \"<from list_served_models>\"}. Use for 'kill my cookbook' / 'stop the model' / 'shut down vLLM'.",
    "download_model": "- ```download_model``` — Download a HuggingFace model. Args (JSON): {\"repo_id\": \"Qwen/Qwen3-8B\", \"host\": \"user@gpu-box\"?, \"include\": \"*Q4_K_M*\"?}.",
    "serve_model": "- ```serve_model``` — Start serving a model with vLLM / SGLang / llama.cpp / Ollama / Diffusers. Args (JSON): {\"repo_id\": \"...\", \"cmd\": \"vllm serve ... --port 8000\" or \"python3 -m sglang.launch_server ... --port 30000\" or \"python3 scripts/diffusion_server.py --model diffusers/stable-diffusion-xl-1.0-inpainting-0.1 --port 8100\", \"host\": \"user@gpu-box\"?}. For image/inpaint/diffusion models, use the `scripts/diffusion_server.py` command exactly. After launch, call `list_served_models`; if it returns a diagnosis with an adjusted command, retry with that command.",
    "list_downloads": "- ```list_downloads``` — Show in-progress HuggingFace model downloads (filters Cookbook tasks/status to downloads only). NO args. Use for 'what's downloading' / 'show my downloads' / 'check download progress'.",
    "cancel_download": "- ```cancel_download``` — Cancel an in-progress download. Args (JSON): {\"session_id\": \"<from list_downloads>\"}. Use for 'cancel the download' / 'kill the download'.",
    "search_hf_models": "- ```search_hf_models``` — Search HuggingFace for models. Args (JSON): {\"query\": \"qwen 8b\", \"limit\": 10?}. Use for 'find a model for X' / 'search huggingface' / 'what models are there for Y'.",
    "list_cached_models": "- ```list_cached_models``` — List models already on disk. Args (JSON, all optional): {\"host\": \"ajax or user@gpu-box\"?, \"model_dir\": \"/data/models,/extra\"?}. Friendly Cookbook server names work. Use for 'what models do I have' / 'show cached models' / 'is X downloaded'.",
    "app_api": """\
```app_api
{"action": "call", "method": "GET", "path": "/api/cookbook/gpus"}
```
GENERIC LOOPBACK to ANY Odysseus internal endpoint. Use this whenever the user wants something the UI can do but there's NO named tool for it. Every UI button hits some /api/* endpoint — you can hit the same one. Auth is handled automatically.

**Discovery first.** If you're not sure of the path, call `{"action":"endpoints","filter":"<keyword>"}` (e.g. filter='calendar' or 'gallery' or 'theme') to list available endpoints with their methods + summaries. Then call with action='call'.

**Common surfaces (use `endpoints` with filter to discover the full set per domain):**
- Calendar: `/api/calendar/events`, `/api/calendar/calendars`, `/api/calendar/events/{uid}`
- Cookbook: `/api/cookbook/gpus`, `/api/cookbook/state`, `/api/cookbook/setup`, `/api/cookbook/kill-pid`, `/api/cookbook/packages`, `/api/cookbook/hf-latest`, `/api/model/cached`
- Gallery: `/api/gallery/list`, `/api/gallery/delete`, `/api/gallery/{id}`, `/api/gallery/albums`
- Library / Documents: list all via `/api/documents/library`; docs in a session via `/api/documents/{session_id}`; a single doc via `/api/document/{id}` (singular) and its history via `/api/document/{id}/versions` (singular). Note the plural `/api/documents/...` vs singular `/api/document/{id}` split.
- Memory: `/api/memory`, `/api/memory/{id}`, `/api/memory/search`
- Notes: `/api/notes`, `/api/notes/{id}`
- Tasks: `/api/tasks`, `/api/tasks/{id}/run`, `/api/tasks/notifications`
- Sessions: `/api/sessions`, `/api/session/{id}`, `/api/session/{id}/truncate`
- Themes: `/api/prefs/themes`, `/api/prefs/custom-themes`
- Settings: `/api/settings`, `/api/prefs/{key}`
- Research: `/api/research/start`, `/api/research/tasks`, `/api/research/report/{id}`
- Compare: `/api/compare/sessions`, `/api/compare/start`
- Email: use named email tools (`list_email_accounts`, `list_emails`, `read_email`, `send_email`, `reply_to_email`). Do NOT use `/api/email/accounts`; it is owner-filtered in tool context and may falsely return empty.
- Endpoints (model providers): `/api/endpoints`, `/api/endpoints/{id}`

Body for POST/PUT/PATCH goes in `body` (object). Query params in `query` (object). Returns the parsed JSON of the response.

**When to prefer named tools over app_api:** if a named wrapper exists (list_email_accounts, list_emails, read_email, manage_calendar, manage_notes, list_served_models, etc.) USE IT — it has nicer output formatting and clearer schema. Reach for `app_api` only when there's no wrapper for what you need.

Blocked paths (refused for safety): /api/auth/, /api/users/, /api/tokens/, /api/admin/, /api/backup/restore, /api/email/accounts.""",
}

def get_builtin_overrides() -> dict:
    """User overrides for built-in tool descriptions (TOOL_SECTIONS).
    Stored globally in settings.json so the user can preview + edit how
    the assistant is told to use a native tool, with a revert path."""
    try:
        from src.settings import get_setting
        ov = get_setting("builtin_tool_overrides", {})
        return ov if isinstance(ov, dict) else {}
    except Exception:
        return {}


def _section_text(name: str, default: str) -> str:
    """Effective TOOL_SECTIONS text for a tool — user override if set,
    else the shipped default."""
    ov = get_builtin_overrides()
    val = ov.get(name)
    return val if isinstance(val, str) and val.strip() else default


def _assemble_prompt(tool_names: set, disabled_tools: set = None, compact: bool = False) -> str:
    """Build the system prompt with only the specified tools included."""
    disabled = disabled_tools or set()
    included = tool_names - disabled

    if compact:
        tool_list = ", ".join(sorted(included)) if included else "none"
        parts = [
            "You are an AI assistant with tool access.",
            f"Available tools: {tool_list}.",
            _API_AGENT_RULES,
        ]
        return "\n\n".join(parts)

    parts = [_AGENT_PREAMBLE]

    # Collect full-block tool sections (with examples)
    full_blocks = []
    # Collect one-liner tool sections
    one_liners = []

    for name, _default_section in TOOL_SECTIONS.items():
        if name not in included:
            continue
        section = _section_text(name, _default_section)
        if section.startswith("```") or section.startswith("-"):
            if section.startswith("- "):
                one_liners.append(section)
            else:
                full_blocks.append(section)

    if full_blocks:
        parts.append("\n\n".join(full_blocks))

    if one_liners:
        parts.append("## Additional tools\n" + "\n".join(one_liners))

    # Mention tools that exist but weren't included
    all_known = set(TOOL_SECTIONS.keys())
    not_shown = all_known - included - disabled
    if not_shown:
        sample = sorted(not_shown)[:5]
        hint = ", ".join(sample)
        if len(not_shown) > 5:
            hint += f", ... ({len(not_shown) - 5} more)"
        parts.append(f"(Other tools available when needed: {hint})")

    parts.append(_AGENT_RULES)
    return "\n\n".join(parts)


# Legacy: full prompt with all tools (fallback when RAG unavailable)
AGENT_SYSTEM_PROMPT = _assemble_prompt(set(TOOL_SECTIONS.keys()))


# --- prompt + context BUILDERS (ADR-054, agent_loop slice 2) ---

_cached_base_prompt = None
_cached_base_prompt_key = None

# Constants — moved out of hot paths to avoid per-request/per-round allocation
# Hosts whose endpoints natively support OpenAI-style function calling.
# When the active endpoint is one of these, the agent sends FUNCTION_TOOL_SCHEMAS
# (so the model emits `tool_calls` directly) instead of relying on the model
# to copy fenced-block examples from prompt text. Smaller models — DeepSeek
# especially — often fail to follow the fenced-block convention and emit raw
# JSON, which the agent then can't parse as a tool call.
_API_HOSTS = frozenset([
    "api.openai.com", "api.anthropic.com",
    "openrouter.ai", "api.groq.com",
    "api.mistral.ai", "api.cohere.com",
    "api.deepseek.com", "deepseek.com",
    "api.together.xyz", "api.fireworks.ai",
    "api.perplexity.ai", "api.x.ai",
    "ollama.com",
])
_MCP_KEYWORDS = frozenset(["browse", "browser", "website", "calendar", "event", "email",
                           "gmail", "screenshot", "navigate", "click", "miniflux", "rss", "feed"])
_ADMIN_SCHEMA_NAMES = frozenset([
    "manage_session", "manage_skills", "manage_tasks",
    "manage_endpoints", "manage_mcp", "manage_webhooks", "manage_tokens",
    "create_session", "list_sessions", "send_to_session", "pipeline",
    "ask_teacher", "list_models", "search_chats",
])
_TOOL_SELECTION_TIMEOUT_SECONDS = 1.5

# Admin tool keywords — if the last user message contains any of these, include admin tools
_ADMIN_KEYWORDS = [
    "session", "sessions", "chat", "chats", "conversation", "conversations",
    "delete", "fork", "truncate",
    "archive", "rename", "endpoint", "endpoints", "api key",
    "webhook", "webhooks", "token", "tokens", "mcp", "server", "skill", "skills",
    "task", "tasks", "schedule", "cron", "setting", "settings", "preference",
    "configure", "config", "setup", "manage", "admin", "pipeline", "second opinion",
    "list models", "switch model", "change model", "theme", "create theme",
    # Documents — "show/list/read my docs", "open my notes file", etc.
    # Without these, manage_documents never reaches the prompt and the
    # agent flails (curl, bash) instead of using the right tool.
    "document", "documents", "doc", "docs", "library", "tidy",
    "note", "notes", "todo", "todos", "reminder", "reminders",
]

def _detect_admin_intent(messages: List[Dict]) -> bool:
    """Check if the last user message suggests admin/management tool usage."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            content_lower = content.lower()
            return any(kw in content_lower for kw in _ADMIN_KEYWORDS)
    return False


def _extract_last_user_message(messages: List[Dict]) -> str:
    """Return the most recent user message as plain text."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            return content
    return ""


def _recent_context_for_retrieval(messages: List[Dict], max_user: int = 3, max_chars: int = 600) -> str:
    """Build the tool-retrieval query from the last few USER turns, not just
    the latest one.

    A contextless follow-up ("yes", "and?", "do it in November") carries no
    tool signal on its own, so RAG/keyword retrieval drops the tools the
    conversation is actually about — the model then "forgets" it has e.g.
    manage_calendar and improvises with bash/app_api. Concatenating the recent
    user turns lets the follow-up inherit the topic so just-used tools stay
    surfaced. Newest-first, so the latest turn survives the length cap."""
    collected = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        content = (content or "").strip()
        # Skip injected tool-result envelopes — role=user but not human intent.
        if not content or content.startswith("[Tool execution results]"):
            continue
        collected.append(content)
        if len(collected) >= max_user:
            break
    return "\n".join(collected)[:max_chars]

def _build_system_prompt(
    messages: List[Dict],
    model: str,
    active_document,
    mcp_mgr,
    disabled_tools: Optional[Set[str]] = None,
    needs_admin: bool = False,
    relevant_tools: Optional[Set[str]] = None,
    mcp_disabled_map: Optional[Dict[str, set]] = None,
    compact: bool = False,
    owner: Optional[str] = None,
) -> List[Dict]:
    """Build agent system prompt, inject MCP/document context, merge consecutive system msgs."""
    global _cached_base_prompt, _cached_base_prompt_key

    # With RAG tools, cache key includes the selected tools
    _rt_key = frozenset(relevant_tools) if relevant_tools else None
    # Include a signature of the built-in overrides so editing one in the
    # Skills UI takes effect without a restart (busts the prompt cache).
    # Hash the full dict so content edits (not just key add/remove) bust it.
    try:
        import hashlib as _hl, json as _json
        _ov_sig = _hl.sha256(_json.dumps(get_builtin_overrides() or {}, sort_keys=True).encode()).hexdigest()
    except Exception:
        _ov_sig = ""
    cache_key = (frozenset(disabled_tools or []), bool(mcp_mgr), needs_admin, _rt_key, compact, _ov_sig)
    if _cached_base_prompt and _cached_base_prompt_key == cache_key and not active_document:
        agent_prompt = _cached_base_prompt
    else:
        agent_prompt = _build_base_prompt(
            disabled_tools,
            mcp_mgr,
            needs_admin,
            relevant_tools,
            mcp_disabled_map=mcp_disabled_map,
            compact=compact,
        )
        if not active_document:
            _cached_base_prompt = agent_prompt
            _cached_base_prompt_key = cache_key

    # Dynamic parts that change per request
    mcp_schemas = []
    if mcp_mgr:
        mcp_schemas = mcp_mgr.get_all_openai_schemas(mcp_disabled_map or {})

    set_active_model(model)

    # Current date/time — every request. Models default to their
    # training-cutoff date when "today" is asked otherwise (was
    # rendering April 2026 dates as "today" when the actual date is
    # May 19, 2026). System TZ-local so calendar/email date math
    # matches what the user sees.
    try:
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now().astimezone()
        _utc = _dt.now(_tz.utc)
        _off = _now.strftime('%z')  # e.g. +0900
        _off_fmt = (f"{_off[:3]}:{_off[3:]}" if _off else "+00:00")
        agent_prompt = (
            f"## Current date and time\n"
            f"Today is {_now.strftime('%A, %B %-d, %Y')} ({_now.strftime('%Y-%m-%d')}). "
            f"Local time is {_now.strftime('%-I:%M %p')} ({_now.strftime('%Z')}, UTC{_off_fmt}); "
            f"current UTC time is {_utc.strftime('%H:%M')}. "
            f"Use this for any 'today'/'tomorrow'/'this week' reasoning — do NOT "
            f"infer the date from training data or from event timestamps.\n"
            f"When scheduling a task (manage_tasks), scheduled_time is in UTC: "
            f"subtract the offset above from the user's local time "
            f"(local {_now.strftime('%H:%M')} = {_utc.strftime('%H:%M')} UTC right now).\n\n"
        ) + agent_prompt
    except Exception:
        pass

    # Document context is kept as a SEPARATE message (not merged into the tool
    # prompt) so the context trimmer doesn't destroy it when truncating the
    # massive tool-description system prompt.
    _doc_message = None
    if active_document:
        set_active_document(active_document.id)
        _doc_raw = active_document.current_content or ""
        _doc_title_l = (active_document.title or "").strip().lower()
        _is_email_doc = (
            active_document.language == "email"
            or _doc_title_l in {"new email", "new mail", "new message"}
            or ("To:" in _doc_raw[:400] and "Subject:" in _doc_raw[:400] and "\n---\n" in _doc_raw)
        )
        if _is_email_doc:
            doc_ctx = (
                f'ACTIVE EMAIL DRAFT (open in editor — the user is looking at this right now)\n'
                f'Title: "{active_document.title}"\n'
                f'```\n{_doc_raw}\n```\n\n'
                f'When the user asks you to write, reply to, or improve this email:\n'
                f'1. Use `update_document` to replace the ENTIRE content — keep all the header lines (To, Subject, In-Reply-To, References, X-Source-UID, X-Source-Folder, X-Attachments) and the `---` separator EXACTLY as they are.\n'
                f'2. Replace ONLY the body text (the part after `---`). If there is a quoted original email (lines starting with `>`), keep that quoted block unchanged BELOW your new reply.\n'
                f'3. Write the reply body above the quoted original. Use the saved email writing style when present.\n'
                f'4. Identity is critical: write as the logged-in user / mailbox owner only. NEVER sign as the recipient, original sender, quoted sender, spouse, assistant, company, or any third party. If adding a signature, use only the name/signature implied by the saved email writing style.\n'
                f'5. Mechanical style is critical: never use em dash/en dash; use --. Never use curly apostrophes. For English emails, use Hi/Hiya from the saved style rather than Hey unless the user explicitly asks for Hey.\n'
                f'6. Do NOT use create_document — the email is already open, you must update it.\n\n'
                f'Do NOT ask the user to paste or share the email — you already have it above.'
            )
        else:
            # Branch on whether the active doc is a form-backed PDF (via the
            # front-matter pointer). Form-backed docs get a focused FORM MODE
            # prompt; everything else gets the regular generic doc context.
            _is_form_backed = False
            try:
                from src.pdf_form_doc import find_source_upload_id
                _is_form_backed = bool(find_source_upload_id(active_document.current_content or ""))
            except Exception:
                pass

            if _is_form_backed:
                doc_ctx = (
                    f'ACTIVE PDF FORM (open in editor — the user is looking at this right now)\n'
                    f'Title: "{active_document.title}"\n'
                    f'```\n{active_document.current_content}\n```\n\n'
                    f'The ENTIRE form is in the markdown above. Every field, on every '
                    f'page, is a bullet line you can see now.\n\n'
                    f'DO NOT try to "read the file", "open the PDF", or call '
                    f'filesystem / read_file / mcp__filesystem__read_file / any '
                    f'file-reading tool. The form IS the document above. Just edit it.\n\n'
                    f'DO NOT ask the user to upload, share, or re-attach. The form is '
                    f'already loaded.\n\n'
                    f'TO EDIT: call `edit_document` with FIND/REPLACE matching whole '
                    f'bullet lines. The trailing HTML comment '
                    f'`<!-- field=NAME type=TYPE -->` is the ground truth anchor — '
                    f'match it to pick the correct bullet.\n\n'
                    f'RULES:\n'
                    f'1. FIND the WHOLE bullet line including the trailing comment. '
                    f'REPLACE keeps the bullet structure and the comment exactly; '
                    f'only the value text after the label changes.\n'
                    f'2. Text bullets — `- **label:** value <!--field=NAME-->` — '
                    f'replace `value`.\n'
                    f'3. Choice bullets — `- **label** [opt1 / opt2 / opt3]: value <!--field=NAME-->` — '
                    f'replace `value` with one of the listed options verbatim.\n'
                    f'4. Checkbox bullets — `- [ ] **label** <!--field=NAME-->` — '
                    f'toggle `[ ]` ↔ `[x]`.\n'
                    f'5. NEVER invent values. If the user gives no value, ASK. Never '
                    f'write fake names, addresses, emails, or "NaN"/"N/A"/"TBD".\n'
                    f'6. NEVER edit the front-matter `<!-- pdf_form_source ... -->` '
                    f'or the `## Page N` section headers.\n'
                    f'7. NEVER touch signature fields (type=signature) — the user '
                    f'signs those by clicking on the rendered PDF.\n'
                    f'8. Bulk requests are scoped by field type. "All included" means '
                    f'every choice field with that option. Do NOT touch text fields.\n'
                    f'9. The user has an Export button — do NOT try to export.'
                )
            else:
                _doc_raw = active_document.current_content or ""
                _doc_numbered = "\n".join(
                    f"{_i}\t{_ln}" for _i, _ln in enumerate(_doc_raw.split("\n"), 1)
                )
                doc_ctx = (
                    f'ACTIVE DOCUMENT (open in the editor — the user is looking at it right now)\n'
                    f'Title: "{active_document.title}" | Language: {active_document.language or "text"}\n'
                    f'Below is the full text. Each line is prefixed with its line number and a TAB, '
                    f'purely so you can locate references like "[Doc edit: L25]" — the number and tab '
                    f'are NOT part of the document.\n'
                    f'```\n{_doc_numbered}\n```\n'
                    f'You ALREADY HAVE this document — it is right above. Do NOT ask the user to paste '
                    f'it, and do NOT use read_file, bash, cat, or any tool to fetch it: it lives in the '
                    f'editor, NOT on disk, so those attempts will fail. Every request is about THIS '
                    f'document unless the user clearly says otherwise.\n'
                    f'A "[Doc edit: L25]" prefix means the user is pointing at that line — use the '
                    f'numbers above to find the text they mean.\n'
                    f'To edit: use edit_document with <<<FIND>>>...<<<REPLACE>>>...<<<END>>>. The FIND '
                    f'text must match the document EXACTLY and must NOT include the leading line-number '
                    f'or tab (those are reference-only). To rewrite entirely: update_document.'
                )
        _doc_message = untrusted_context_message("active editor document", doc_ctx)
        _doc_message["_protected"] = True

        # Auto-detect suggestion mode
        _last_user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                _content = msg.get("content", "")
                if isinstance(_content, list):
                    _content = " ".join(b.get("text", "") for b in _content if isinstance(b, dict))
                _last_user_msg = _content.lower()
                break
        _suggest_keywords = ["suggest", "review", "improve", "feedback", "critique", "proofread", "check my", "look over"]
        if any(kw in _last_user_msg for kw in _suggest_keywords):
            _doc_message["content"] += (
                "\n\nTrusted instruction for this turn: the user appears to want "
                "suggestions for the active editor document. Use suggest_document "
                "with <<<FIND>>>...<<<SUGGEST>>>...<<<REASON>>>...<<<END>>> blocks."
            )
    else:
        set_active_document(None)

    # Inject writing style for any email writing path. This is deliberately
    # broader than read/list: models may compose via send_email, reply_to_email,
    # or ui_control open_email_reply after the first tool round.
    _inject_style = False
    _EMAIL_TOOL_HINTS = {
        "list_email_accounts", "send_email", "reply_to_email", "list_emails", "read_email",
        "bulk_email", "archive_email", "delete_email", "mark_email_read",
        "resolve_contact", "ui_control",
        "mcp__email__list_email_accounts",
        "mcp__email__send_email", "mcp__email__reply_to_email",
        "mcp__email__list_emails", "mcp__email__read_email",
        "mcp__email__bulk_email", "mcp__email__archive_email",
        "mcp__email__delete_email", "mcp__email__mark_email_read",
    }
    if active_document and active_document.language == "email":
        _inject_style = True
    elif relevant_tools and (_EMAIL_TOOL_HINTS & set(relevant_tools)):
        # Avoid adding email style for unrelated UI-only requests unless the
        # user's words are email-ish.
        _last_user_text = ""
        for _msg in reversed(messages):
            if _msg.get("role") == "user":
                _c = _msg.get("content", "")
                if isinstance(_c, list):
                    _c = " ".join(b.get("text", "") for b in _c if isinstance(b, dict))
                _last_user_text = str(_c).lower()
                break
        _inject_style = any(tok in _last_user_text for tok in ("email", "mail", "reply", "send", "inbox"))
    if _inject_style:
        try:
            from src.settings import load_settings as _load_settings
            _style = (_load_settings().get("email_writing_style", "") or "").strip()
            if _style:
                agent_prompt += (
                    "\n\n📧 EMAIL WRITING STYLE AND IDENTITY — FOLLOW FOR ANY EMAIL DRAFT OR SEND:\n"
                    f"{_style}\n\n"
                    "Hard identity rule: write as the user/mailbox owner only. Do not sign as, speak as, "
                    "or imply you are the recipient, original sender, quoted sender, spouse, assistant, "
                    "company, or any other third party. If a signature is needed, use only the name/signature "
                    "from the saved writing style. Never copy a name from the quoted thread into the sign-off.\n"
                    "Mechanical style rules: never use em dash/en dash; use --. Never use curly apostrophes. "
                    "For English emails, default to Hi [Name] or Hiya from the saved style rather than Hey. "
                    "If the saved style specifies Best/newline/name, use that sign-off when a sign-off is natural."
                )
        except Exception:
            pass

    # When creating email documents, instruct the AI on the format
    if relevant_tools and (_EMAIL_TOOL_HINTS & set(relevant_tools)):
        agent_prompt += (
            '\n\n📧 EMAIL DOCUMENT FORMAT: When drafting email replies, use create_document with language="email". '
            'The content format is:\n'
            'To: recipient@example.com\n'
            'Subject: Re: Original subject\n'
            'In-Reply-To: <original-message-id>\n'
            'References: <original-message-id>\n'
            '---\n'
            'Body text here...\n\n'
            'The user can then edit and click Send or Draft in the editor. For an already-open email draft, '
            'edit the current document instead of creating another one.'
        )

    # Inject relevant skills based on the user's last message. The
    # SkillsManager does a Jaccard token-match over published skills'
    # name + description + when_to_use + procedure, returning the top
    # few. If the teacher wrote a procedure for "open my X chat" last
    # time the student failed, this is where the student finds it
    # before deciding which tool to call.
    try:
        last_user = _extract_last_user_message(messages)
        # Respect the user's skills-enabled toggle (mirrors memory_enabled).
        # When off, don't inject relevant skills into the prompt.
        _skills_on = True
        _prefs = {}
        try:
            from routes.prefs_routes import _load_for_user as _load_prefs
            _prefs = _load_prefs(owner) or {}
            _skills_on = _prefs.get("skills_enabled", True)
        except Exception:
            pass
        if last_user and _skills_on:
            from services.memory.skills import SkillsManager
            from src.constants import DATA_DIR
            sm = SkillsManager(DATA_DIR)
            # Brain → Skills settings → "Auto-approve skills" toggle +
            # confidence threshold. Approve OFF → published-only (no draft
            # passes). Approve ON → drafts at/above the chosen confidence
            # (0 = "All"). Falls back to the global default setting.
            if not _prefs.get("auto_approve_skills", True):
                _skill_min_conf = 2.0  # nothing draft clears it → published only
            else:
                try:
                    _skill_min_conf = float(_prefs.get(
                        "skill_min_confidence",
                        get_setting("skill_autosave_min_confidence", 0.85)))
                except (TypeError, ValueError):
                    _skill_min_conf = 0.85
            try:
                _skill_max_injected = int(_prefs.get(
                    "skill_max_injected",
                    get_setting("skill_max_injected", 3)))
            except (TypeError, ValueError):
                _skill_max_injected = 3
            _skill_max_injected = max(0, min(12, _skill_max_injected))
            relevant_skills = sm.get_relevant_skills(
                last_user,
                skills=sm.load(owner=owner),
                threshold=0.25,
                max_items=_skill_max_injected,
                min_confidence=_skill_min_conf,
            ) if _skill_max_injected > 0 else []
            if relevant_skills:
                # Bump the "uses" counter on every skill we actually surface
                # to the agent — otherwise every skill shows "0 times" no
                # matter how often it's been matched and applied.
                for _sk in relevant_skills:
                    try:
                        sm.record_use(_sk.get('name', ''))
                    except Exception:
                        pass
                lines = ["", "## Relevant skills for this request",
                         "These skills are matched to your current request. Each is a "
                         "procedure proven to work. Follow them step by step. To see "
                         "the full SKILL.md (more detail, pitfalls, verification "
                         "steps), call `manage_skills` with action='view' and the "
                         "skill name."]
                for sk in relevant_skills:
                    src_tag = ""
                    if sk.get("source") == "teacher-escalation":
                        tm = sk.get("teacher_model") or "teacher"
                        src_tag = f" _(learned from {tm})_"
                    lines.append(f"\n### {sk.get('name','?')}{src_tag}")
                    if sk.get("description"):
                        lines.append(sk["description"])
                    if sk.get("when_to_use"):
                        lines.append(f"_When to use:_ {sk['when_to_use']}")
                    proc = sk.get("procedure") or []
                    if proc:
                        lines.append("Procedure:")
                        for i, step in enumerate(proc, 1):
                            lines.append(f"  {i}. {step}")
                    pitfalls = sk.get("pitfalls") or []
                    if pitfalls:
                        lines.append("Pitfalls: " + "; ".join(pitfalls))
                agent_prompt += "\n".join(lines)
    except Exception as _sk_err:
        logger.debug(f"skill injection failed (non-fatal): {_sk_err}")

    agent_msg = {"role": "system", "content": agent_prompt}
    insert_idx = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            insert_idx = i + 1
        else:
            break

    messages = messages[:insert_idx] + [agent_msg] + messages[insert_idx:]

    # Merge consecutive system messages — but skip _protected doc messages
    merged = []
    for msg in messages:
        if (msg.get("role") == "system"
            and not msg.get("_protected")
            and merged and merged[-1].get("role") == "system"
            and not merged[-1].get("_protected")):
            merged[-1] = {
                "role": "system",
                "content": merged[-1]["content"] + "\n\n" + msg["content"],
            }
        else:
            merged.append(msg)

    # Insert the document message right before the last user message so it's
    # close to the user's request and survives context trimming independently.
    if _doc_message:
        last_user_idx = len(merged) - 1
        for i in range(len(merged) - 1, -1, -1):
            if merged[i].get("role") == "user":
                last_user_idx = i
                break
        merged.insert(last_user_idx, _doc_message)

    return merged, mcp_schemas


_ADMIN_TOOLS = {
    "manage_session", "manage_skills", "manage_tasks",
    "manage_endpoints", "manage_mcp", "manage_webhooks", "manage_tokens",
    "manage_documents", "manage_settings", "create_session", "list_sessions",
    "send_to_session", "pipeline", "ask_teacher", "list_models",
}

def _build_base_prompt(
    disabled_tools,
    mcp_mgr,
    needs_admin,
    relevant_tools=None,
    mcp_disabled_map=None,
    compact: bool = False,
):
    """Build the agent prompt with only relevant tools included.

    If relevant_tools is provided (from RAG retrieval), only those tools
    are shown with full descriptions. Otherwise falls back to full prompt.
    """
    from src.tool_index import ALWAYS_AVAILABLE

    disabled = set(disabled_tools or [])
    if not get_setting("image_gen_enabled", True):
        disabled.add("generate_image")

    if relevant_tools is not None:
        # RAG mode: include always-available + retrieved + admin (if needed)
        tool_names = set(ALWAYS_AVAILABLE) | set(relevant_tools)
        if needs_admin:
            tool_names |= _ADMIN_TOOLS
        agent_prompt = _assemble_prompt(tool_names, disabled, compact=compact)
    else:
        # Fallback: full prompt (RAG unavailable)
        agent_prompt = AGENT_SYSTEM_PROMPT
        if not needs_admin:
            # At least strip the management section
            mgmt_tools = set(TOOL_SECTIONS.keys()) - set(ALWAYS_AVAILABLE) - {
                "generate_image", "suggest_document",
                "chat_with_model", "ask_teacher", "list_models",
            }
            agent_prompt = _assemble_prompt(
                set(TOOL_SECTIONS.keys()) - mgmt_tools, disabled, compact=compact
            )
        elif compact:
            agent_prompt = _assemble_prompt(set(TOOL_SECTIONS.keys()), disabled, compact=True)

    # Inject the Level-0 skill index — one line per skill so the agent
    # knows what canonical procedures exist. Includes published skills
    # plus teacher-escalation drafts (auto-written when the student
    # fails a task; appear here on the very next turn so the student
    # can apply them immediately). Full SKILL.md fetched on demand via
    # `manage_skills view name=...`. Gating mirrors index_for: platform
    # + requires_toolsets + fallback_for_toolsets.
    try:
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR
        _sm = SkillsManager(DATA_DIR)
        active_tools = list(set(TOOL_SECTIONS.keys()) - set(disabled or []))
        skill_idx = _sm.index_for(owner=None, active_toolsets=active_tools)
        if skill_idx:
            lines = ["## Available skills",
                     "Procedures the assistant should consult before doing domain work. "
                     "Fetch the full procedure with `manage_skills` action=view name=<name> "
                     "when one looks relevant. Entries tagged `(draft)` were written by the "
                     "teacher-escalation loop after a prior failure — treat them as authoritative "
                     "guidance; if you follow one and it works, that's a good signal the procedure "
                     "is correct."]
            by_cat: dict[str, list] = {}
            for s in skill_idx:
                by_cat.setdefault(s["category"], []).append(s)
            for cat in sorted(by_cat):
                lines.append(f"\n**{cat}**")
                for s in by_cat[cat]:
                    badge = " *(draft)*" if s.get("status") == "draft" else ""
                    lines.append(f"- `{s['name']}` — {s['description']}{badge}")
            agent_prompt += "\n\n" + "\n".join(lines)
    except Exception as _e:
        # Skill index is a soft enhancement — never fail prompt assembly on it.
        logger.debug(f"Skill-index injection skipped: {_e}")

    # Inject integration descriptions
    from src.integrations import get_integrations_prompt
    integ_prompt = get_integrations_prompt()
    if integ_prompt:
        agent_prompt += "\n\n" + integ_prompt

    # Inject MCP tool descriptions
    if mcp_mgr:
        mcp_desc = mcp_mgr.get_tool_descriptions_for_prompt(mcp_disabled_map or {})
        if mcp_desc:
            agent_prompt += mcp_desc

    return agent_prompt
