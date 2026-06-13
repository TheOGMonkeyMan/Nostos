"""Phase 2.2 (ADR-052): verify the email MCP server's folder/parse helper split.

The 5 IMAP folder helpers + 2 message-parsing helpers moved verbatim out of
mcp_servers/email_server.py into mcp_servers/email_server_helpers.py, re-imported
so the tool handlers keep calling them. (email_server.py was previously untouched
by the test suite - this also adds first import coverage of that module.)
"""

import mcp_servers.email_server as es

_NAMES = [
    "_detect_sent_folder",
    "_folder_name_from_list_line",
    "_list_folder_lines",
    "_resolve_folder",
    "_folder_role_from_name",
    "_decode_header",
    "_extract_text",
]


def test_helpers_reexported_from_email_server():
    for n in _NAMES:
        assert hasattr(es, n), f"{n} missing from email_server namespace"
        assert getattr(es, n).__module__ == "mcp_servers.email_server_helpers"


def test_email_server_module_imports_and_exposes_server():
    # Importing the module must not raise and must still build its MCP Server.
    assert hasattr(es, "server")
