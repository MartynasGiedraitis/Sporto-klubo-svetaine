#!/usr/bin/env bash
set -euo pipefail
mongosh --host mongo:27017 --quiet --eval '
try { rs.status(); } catch (e) {
  if (e.codeName !== "NotYetInitialized") throw e;
  rs.initiate({_id:"rs0", members:[{_id:0,host:"mongo:27017"}]});
}'
for attempt in $(seq 1 60); do
  if mongosh --host mongo:27017 --quiet --eval 'quit(db.hello().isWritablePrimary ? 0 : 1)'; then
    exit 0
  fi
  sleep 1
done
echo 'MongoDB replica set did not become primary' >&2
exit 1
