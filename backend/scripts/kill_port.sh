#!/bin/bash
# lsof isn't available in this Git Bash environment; use netstat+taskkill instead.
port="${1:-8000}"
pid=$(netstat -ano | grep ":$port " | grep LISTENING | awk '{print $5}' | head -1)
if [ -n "$pid" ]; then
  taskkill //F //PID "$pid" 2>&1
else
  echo "nothing listening on $port"
fi
