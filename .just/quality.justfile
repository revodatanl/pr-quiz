# --- Quality recipes ---

# Run the unit test suite
[group: 'quality']
test *args:
  python -m pytest tests/ -q {{args}}

alias t := test

# Check justfile formatting
[group: 'quality']
fmt-check:
  just --fmt --check --unstable
