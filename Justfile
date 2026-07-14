set quiet
set positional-arguments

import? ".just/shell.justfile"
import ".just/setup.justfile"
import ".just/databricks.justfile"
import ".just/github.justfile"
import ".just/quality.justfile"

# List available recipes
default:
    just --list
