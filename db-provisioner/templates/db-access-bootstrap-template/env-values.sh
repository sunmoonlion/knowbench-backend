#!/usr/bin/env bash

# Supply literal values over stdin, keeping credentials out of process arguments.
db_uri() {
  printf '%s\0' "$@" | python3 -c '
import sys
from urllib.parse import quote, urlencode
scheme, user, password, host, port, database, option = sys.stdin.buffer.read().decode().split("\0")[:-1]
host = "[" + host + "]" if ":" in host and not host.startswith("[") else host
def component(value): return quote(value, safe="")
uri = f"{scheme}://{component(user)}:{component(password)}@{host}:{port}/{component(database)}"
query = {"authSource": database} if scheme == "mongodb" else {"sslmode": option} if option else {}
print(uri + ("?" + urlencode(query) if query else ""), end="")
'
}

replace_env_value() {
  local file="$1" key="$2" value="$3"
  printf '%s' "$value" | python3 -c '
import sys
from pathlib import Path
path, key = Path(sys.argv[1]), sys.argv[2]
value = sys.stdin.read()
if "\r" in value or "\n" in value: raise ValueError("expected a single-line environment value")
lines = path.read_text().splitlines()
entry = f"{key}={value}"
kept = [line for line in lines if not line.startswith(key + "=")]
path.write_text("\n".join([*kept,entry])+"\n")
' "$file" "$key"
}
