set -euo pipefail
.venv-3.13/bin/python -m pytest -vv \
  tests/test_state.py::test_inbox_is_owner_only \
  tests/test_state.py::test_preexisting_permissive_state_directory_is_forced_to_owner_only \
  tests/test_state.py::test_state_directory_symlink_is_refused_without_chmodding_target \
  tests/test_state.py::test_replacement_during_directory_chmod_is_detected_without_touching_replacement \
  tests/test_state.py::test_replacement_after_open_blocks_state_access
