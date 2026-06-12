"""Phase 2.2 (ADR-036): characterize + verify the email_routes helper extraction.

email_routes.py's top-level helper functions (IMAP/folder/flag/move utilities,
SMTP-config resolution, and email-HTML rendering/sanitizing) were carved out of
routes/email_routes.py into routes/email_route_helpers.py and re-imported so the
route handlers (and the external _resolve_send_config import in task_scheduler)
keep working. The pure helpers asserted here take no I/O.
"""

from routes import email_routes as er

_PUBLIC = [
    "_email_tag_owner_aliases",
    "_record_email_received_events",
    "_folder_name_from_list_line",
    "_list_imap_folders",
    "_resolve_mail_folder",
    "_folder_role_from_name",
    "_uid_bytes",
    "_uid_exists",
    "_imap_uid_search",
    "_imap_uid_fetch",
    "_uid_from_fetch_meta",
    "_smtp_ready",
    "_resolve_send_config",
    "_store_email_flag",
    "_move_email_message",
    "_apply_odysseus_headers",
    "_md_to_email_html",
    "_sanitize_email_html",
]


# --- behavior preservation (pure helpers, hermetic) ------------------------

def test_md_to_email_html_returns_str():
    assert isinstance(er._md_to_email_html("**hi** there"), str)


def test_sanitize_email_html_returns_str():
    assert isinstance(er._sanitize_email_html("<p>ok</p><script>bad()</script>"), str)


def test_smtp_ready_returns_bool():
    assert isinstance(er._smtp_ready({}), bool)


def test_folder_role_from_name_returns_str():
    assert isinstance(er._folder_role_from_name("INBOX"), str)


def test_uid_bytes_returns_bytes():
    assert isinstance(er._uid_bytes("123"), bytes)


# --- extraction target (RED until routes/email_route_helpers.py exists) -----

def test_email_route_helpers_module_exists_and_exports():
    from routes import email_route_helpers

    for name in _PUBLIC:
        assert hasattr(email_route_helpers, name), f"email_route_helpers missing {name}"


def test_email_route_helpers_reexported_with_identity():
    from routes import email_route_helpers

    for name in _PUBLIC:
        assert getattr(er, name) is getattr(email_route_helpers, name), f"{name} not re-exported with identity"
