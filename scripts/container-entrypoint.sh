#!/bin/bash
# https://google.github.io/styleguide/shellguide.html#s7-naming-conventions
# set -x
set -e

RESET_TEXT=$'\e[25;0m';GREEN_TEXT=$'\e[92m';YELLOW_TEXT=$'\e[5;33m';


if [[ "$1" == "hello" ]]; then
    echo "[Task][container-entrypoint] Running Hello world"
    exec python3 main.py hello
fi

exec "$@"
