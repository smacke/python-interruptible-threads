#!/usr/bin/env bash

# ref: https://vaneyckt.io/posts/safer_bash_scripts_with_set_euxo_pipefail/
set -euxo pipefail

FILES="interruptible_threading.py examples.py ./tests"
black $FILES $@
