"""Phase 2.2 (ADR-042): verify the exec/shell scheduler-action split.

action_ssh_command / action_run_script / action_run_local and their shared
_run_subprocess runner moved verbatim out of src/builtin_actions.py into
src/actions/shell.py, re-imported there so the BUILTIN_ACTIONS registry and
direct callers are unchanged. These checks pin the re-import contract and the
hermetic empty-input branches (no subprocess spawned).
"""

import asyncio

import src.builtin_actions as ba


def test_registry_resolves_to_new_module():
    for name in ("ssh_command", "run_script", "run_local"):
        assert name in ba.BUILTIN_ACTIONS, f"{name} missing from BUILTIN_ACTIONS"
        assert ba.BUILTIN_ACTIONS[name].__module__ == "src.actions.shell"
    # direct-import surface preserved on builtin_actions
    assert ba.action_ssh_command.__module__ == "src.actions.shell"
    assert ba._run_subprocess.__module__ == "src.actions.shell"


def test_empty_input_branches_are_hermetic():
    # No command/script -> early error tuple, no subprocess spawned.
    assert asyncio.run(ba.action_ssh_command(owner="u", command="")) == ("No command specified", False)
    assert asyncio.run(ba.action_run_script(owner="u", script="")) == ("No script specified", False)
    assert asyncio.run(ba.action_run_local(owner="u", script="")) == ("No script specified", False)
