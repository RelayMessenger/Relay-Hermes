set -euo pipefail
cd /home/daytona/relay-hermes
HERMES_AGENT_SRC=/home/daytona/hermes-agent \
  .venv-3.13/bin/python -m pytest -vv \
  tests/test_staging_wrapper.py \
  tests/test_adapter.py::test_env_enablement_keeps_chat_and_operator_allowlists_separate \
  tests/test_adapter.py::test_ordinary_contacts_can_chat_but_are_not_gateway_operators \
  tests/test_adapter.py::test_privileged_commands_default_deny_without_an_operator_decision \
  tests/test_adapter.py::test_multiplexed_profiles_never_fall_through_to_process_relay_settings \
  tests/test_state.py::test_state_directory_symlink_is_refused_without_chmodding_target \
  tests/test_state.py::test_symlinked_state_ancestor_is_refused_without_creating_below_it \
  tests/test_state.py::test_dangling_database_symlink_is_refused_without_creating_its_target \
  tests/test_state.py::test_database_symlink_never_mutates_an_existing_target \
  tests/test_state.py::test_directory_replacement_before_sqlite_connect_cannot_redirect_or_mutate \
  tests/test_state.py::test_database_replacement_before_sqlite_connect_cannot_reach_dangling_target \
  tests/test_state.py::test_replacement_during_directory_chmod_is_detected_without_touching_replacement \
  tests/test_state.py::test_replacement_after_open_blocks_state_access \
  tests/test_state.py::test_token_switch_refuses_before_requeue_or_snapshot_processing \
  tests/test_state.py::test_origin_switch_refuses_the_same_state_directory
