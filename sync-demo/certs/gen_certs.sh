#!/usr/bin/env bash
# Generate a throwaway CA and one server certificate valid for both compose
# hostnames (primary-server, secondary-server) and localhost. For the DEMO
# ONLY -- the keys are written next to this script, gitignored, and must
# never be reused anywhere else.
#
# Run:  ./certs/gen_certs.sh
# Then: uncomment the "TLS" blocks in docker-compose.yml and restart.
set -euo pipefail
cd "$(dirname "$0")"

openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt \
  -subj "/CN=sync-demo-ca" -days 365 2>/dev/null

cat > san.cnf <<'CNF'
[req]
distinguished_name = dn
req_extensions = ext
prompt = no
[dn]
CN = server
[ext]
subjectAltName = @alt
[alt]
DNS.1 = server
DNS.2 = localhost
DNS.3 = innocent-front.example.com
DNS.4 = primary-server
DNS.5 = secondary-server
DNS.6 = front-proxy
IP.1  = 127.0.0.1
CNF

openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -config san.cnf 2>/dev/null
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 365 -extensions ext -extfile san.cnf 2>/dev/null
rm -f server.csr san.cnf ca.srl
echo "wrote certs/ca.crt, certs/server.crt, certs/server.key"
echo "now uncomment the TLS lines in docker-compose.yml and restart."
